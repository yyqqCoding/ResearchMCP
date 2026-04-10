"""
ResearchMCP 服务器核心入口
实现 `search_web`, `fetch_page`, `map_docs_site` 三大核心工具。
"""

import asyncio
from typing import Annotated

from fastmcp import FastMCP, Context

from research_mcp.config import config
from research_mcp.logger import log_info, logger as mcp_logger
from research_mcp.sources import merge_sources, split_answer_and_sources
from research_mcp.providers.grok import GrokSearchProvider

mcp = FastMCP("ResearchMCP")


async def _debug_log(ctx: Context, message: str) -> None:
    if config.debug_enabled:
        mcp_logger.info(message)


# ── 并发搜索引擎内部函数 ────────────────────────────────────────

def _extra_results_to_sources(tavily_res: list[dict] | None, firecrawl_res: list[dict] | None) -> list[dict]:
    sources = []
    if firecrawl_res:
        for r in firecrawl_res:
            sources.append({"title": r.get("title", ""), "url": r.get("url", ""), "provider": "Firecrawl"})
    if tavily_res:
        for r in tavily_res:
            sources.append({"title": r.get("title", ""), "url": r.get("url", ""), "provider": "Tavily"})
    return sources


async def _call_tavily_search(query: str, max_results: int = 5) -> list[dict] | None:
    import httpx
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{config.tavily_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_raw_content": False,
        "include_answer": False,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
    except Exception:
        return None


async def _call_firecrawl_search(query: str, limit: int = 5) -> list[dict] | None:
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return data.get("data", {}).get("web", [])
    except Exception:
        return None


def _build_sources_preview(sources: list[dict], limit: int = 3) -> list[dict]:
    preview = []
    for item in sources[:limit]:
        if not isinstance(item, dict):
            continue
        preview_item = {}
        for key in ("title", "url", "provider"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                preview_item[key] = value.strip()
        if "url" in preview_item:
            preview.append(preview_item)
    return preview


def _classify_search_result(answer: str, sources: list[dict]) -> tuple[str, str]:
    normalized_answer = answer.strip()
    if not sources:
        if not normalized_answer:
            return "empty", "fallback"
        return "partial", "refine_query"
    if not normalized_answer:
        return "partial", "refine_query"
    return "sufficient", "answer"


_SPEC_HINTS = (
    "rfc",
    "spec",
    "standard",
    "规范",
    "标准",
    "api reference",
    "official docs",
    "官方文档",
    "api文档",
    "接口文档",
    "参考文档",
)
_MAP_HINTS = (
    "docs site",
    "documentation site",
    "site structure",
    "sitemap",
    "all pages",
    "目录",
    "站点结构",
    "文档站",
    "文档站点",
    "页面结构",
    "站点地图",
    "文档结构",
)
_AUTHORITY_HOST_HINTS = ("ietf.org", "rfc-editor.org", "w3.org", "whatwg.org", "docs.", "developer.")
_AUTHORITY_TITLE_HINTS = ("rfc", "spec", "standard", "reference")
_FOLLOW_UP_FAILURE_PREFIXES = (
    "Extraction failed:",
    "Extraction completely failed.",
    "Failed:",
    "Web Mapping Error:",
)


def _normalize_query(query: str) -> str:
    return query.casefold().strip()


def _should_map_follow_up(query: str) -> bool:
    normalized = _normalize_query(query)
    return any(token in normalized for token in _MAP_HINTS)


def _should_fetch_follow_up(query: str) -> bool:
    normalized = _normalize_query(query)
    return any(token in normalized for token in _SPEC_HINTS)


def _select_follow_up_route(query: str) -> str:
    if _should_map_follow_up(query):
        return "map"
    if _should_fetch_follow_up(query):
        return "fetch"
    return "search"


def _is_authoritative_url(url: str) -> bool:
    lowered = url.casefold()
    return any(token in lowered for token in _AUTHORITY_HOST_HINTS)


def _source_looks_authoritative(item: dict) -> bool:
    url = item.get("url", "")
    title = item.get("title", "")
    lowered_url = url.casefold() if isinstance(url, str) else ""
    lowered_title = title.casefold() if isinstance(title, str) else ""
    return (
        (isinstance(url, str) and url and _is_authoritative_url(url))
        or (
            isinstance(url, str)
            and url
            and any(token in lowered_title for token in _AUTHORITY_TITLE_HINTS)
            and any(token in lowered_url for token in ("reference", "spec", "docs.", "developer."))
        )
    )


def _is_usable_follow_up_content(content: str) -> bool:
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    return not any(stripped.startswith(prefix) for prefix in _FOLLOW_UP_FAILURE_PREFIXES)


def _select_fetch_urls(query: str, sources: list[dict], limit: int = 2) -> list[str]:
    if not _should_fetch_follow_up(query):
        return []
    selected: list[str] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if (
            isinstance(url, str)
            and url
            and url not in selected
            and _source_looks_authoritative(item)
        ):
            selected.append(url)
        if len(selected) >= limit:
            break
    return selected


def _select_map_url(query: str, sources: list[dict]) -> str:
    if not _should_map_follow_up(query):
        return ""
    for item in sources:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        title = item.get("title", "")
        if not isinstance(url, str) or not url:
            continue
        lowered_url = url.casefold()
        lowered_title = title.casefold() if isinstance(title, str) else ""
        if "docs." in lowered_url or "/reference" in lowered_url or "docs" in lowered_title:
            return url
    return ""


async def _run_follow_up(query: str, sources: list[dict], ctx: Context = None) -> tuple[list[str], list[str], list[str], str]:
    orchestration_path = ["search"]
    fetched_urls: list[str] = []
    evidence_sections: list[str] = []
    mapped_url = ""
    route = _select_follow_up_route(query)

    if route == "map":
        map_url = _select_map_url(query, sources)
        await _debug_log(ctx, f"search_web route=map selected_map_url={map_url!r}")
        if map_url:
            map_result = await map_docs_site(map_url, max_depth=1, limit=10, ctx=ctx)
            if _is_usable_follow_up_content(map_result):
                orchestration_path.append("map")
                mapped_url = map_url
                evidence_sections.append(f"[Mapped site]\nURL: {map_url}\n{map_result}")
            else:
                await _debug_log(
                    ctx,
                    f"search_web ignored_map_url={map_url} reason=unusable_follow_up_output",
                )
    elif route == "fetch":
        selected_fetch_urls = _select_fetch_urls(query, sources, limit=2)
        await _debug_log(ctx, f"search_web route=fetch selected_fetch_urls={selected_fetch_urls!r}")
        for url in selected_fetch_urls:
            markdown = await fetch_page(url, ctx=ctx)
            if _is_usable_follow_up_content(markdown):
                fetched_urls.append(url)
                if "fetch" not in orchestration_path:
                    orchestration_path.append("fetch")
                evidence_sections.append(f"[Fetched page]\nURL: {url}\n{markdown[:2000]}")
                if len(fetched_urls) >= 2:
                    break
            else:
                await _debug_log(
                    ctx,
                    f"search_web ignored_fetch_url={url} reason=unusable_follow_up_output",
                )
    else:
        await _debug_log(ctx, "search_web route=search selected_fetch_urls=[] selected_map_url=''")

    return orchestration_path, evidence_sections, fetched_urls, mapped_url


# ── MCP 工具 1：深度网络总览搜素 ─────────────────────────────────

@mcp.tool(
    name="search_web",
    description=(
        "针对一个明确问题进行网页搜索与归纳，返回答案、来源预览和下一步建议。"
        "不要对近似问题反复调用。"
    ),
)
async def search_web(
    query: Annotated[str, "The search query in natural language."],
    platform: Annotated[str, "Optional target platform (e.g., 'GitHub', 'Reddit')."] = "",
    extra_sources: Annotated[int, "Number of extra web sources to fetch concurrently."] = 0,
    ctx: Context = None,
) -> dict:
    """
    执行深度人工智能检索，聚合答案与轻量信源预览。
    """
    await log_info(ctx, f"Starting search_web for: {query}", config.debug_enabled)

    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        return {
            "content": f"配置错误: {str(e)}",
            "sources_count": 0,
            "sources": [],
            "evidence_status": "empty",
            "recommended_action": "fallback",
            "orchestration_path": [],
            "fetched_urls": [],
            "mapped_url": "",
        }

    grok_provider = GrokSearchProvider(api_url, api_key, config.grok_model)

    firecrawl_count = 0
    tavily_count = 0
    if extra_sources > 0:
        has_tavily = bool(config.tavily_api_key)
        has_firecrawl = bool(config.firecrawl_api_key)
        if has_firecrawl and has_tavily:
            firecrawl_count = extra_sources // 2
            tavily_count = extra_sources - firecrawl_count
        elif has_firecrawl:
            firecrawl_count = extra_sources
        elif has_tavily:
            tavily_count = extra_sources

    async def _safe_grok() -> str:
        try:
            return await grok_provider.search(query, platform, ctx, prompt_mode="embedded")
        except Exception as e:
            await log_info(ctx, f"Grok Error: {e}", config.debug_enabled)
            return ""

    async def _safe_tavily() -> list[dict] | None:
        if tavily_count:
            return await _call_tavily_search(query, tavily_count)
        return None

    async def _safe_firecrawl() -> list[dict] | None:
        if firecrawl_count:
            return await _call_firecrawl_search(query, firecrawl_count)
        return None

    coros = [_safe_grok()]
    if tavily_count > 0:
        coros.append(_safe_tavily())
    if firecrawl_count > 0:
        coros.append(_safe_firecrawl())

    gathered = await asyncio.gather(*coros)

    grok_result: str = gathered[0] or ""
    tavily_results = gathered[1] if tavily_count > 0 else None
    firecrawl_results = gathered[-1] if firecrawl_count > 0 else None

    # 分离答案与内置来源
    answer, grok_sources = split_answer_and_sources(grok_result)
    extra = _extra_results_to_sources(tavily_results, firecrawl_results)

    # 彻底排重合并
    all_sources = merge_sources(grok_sources, extra)
    await log_info(ctx, f"Search done. Extracted {len(all_sources)} sources.", config.debug_enabled)
    orchestration_path, evidence_sections, fetched_urls, mapped_url = await _run_follow_up(query, all_sources, ctx)
    await _debug_log(
        ctx,
        f"search_web follow_up_summary orchestration_path={orchestration_path!r} "
        f"fetched_urls={fetched_urls!r} mapped_url={mapped_url!r}",
    )

    synthesize_with_evidence = getattr(grok_provider, "synthesize_with_evidence", None)
    if evidence_sections and callable(synthesize_with_evidence):
        orchestration_path.append("synthesize")
        try:
            refined_answer = await synthesize_with_evidence(query, answer, evidence_sections, ctx)
            if isinstance(refined_answer, str) and refined_answer.strip():
                answer = refined_answer
        except Exception as e:
            await log_info(ctx, f"Grok synthesis error: {e}", config.debug_enabled)

    evidence_status, recommended_action = _classify_search_result(answer, all_sources)
    await _debug_log(
        ctx,
        f"search_web result_summary orchestration_path={orchestration_path!r} "
        f"fetched_urls={fetched_urls!r} mapped_url={mapped_url!r} "
        f"evidence_status={evidence_status} recommended_action={recommended_action}",
    )

    return {
        "content": answer,
        "sources_count": len(all_sources),
        "sources": _build_sources_preview(all_sources),
        "evidence_status": evidence_status,
        "recommended_action": recommended_action,
        "orchestration_path": orchestration_path,
        "fetched_urls": fetched_urls,
        "mapped_url": mapped_url,
    }


# ── 强韧性抓取工具内构 ──────────────────────────────────────────

async def _call_tavily_extract(url: str) -> str | None:
    import httpx
    api_key = config.tavily_api_key
    if not api_key:
        return None
    endpoint = f"{config.tavily_api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            if data.get("results") and len(data["results"]) > 0:
                content = data["results"][0].get("raw_content", "")
                return content if content and content.strip() else None
            return None
    except Exception:
        return None


async def _call_firecrawl_scrape(url: str, ctx=None) -> str | None:
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    max_retries = config.retry_max_attempts
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
        }
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
                markdown = data.get("data", {}).get("markdown", "")
                if markdown and markdown.strip():
                    return markdown
                await log_info(ctx, f"Firecrawl scraping empty markdown, retrying {attempt+1}/{max_retries}...", config.debug_enabled)
        except Exception as e:
            await log_info(ctx, f"Firecrawl extract error: {e}", config.debug_enabled)
            return None
    return None


# ── MCP 工具 3：强降级提取网页 ─────────────────────────────────

@mcp.tool(
    name="fetch_page",
    description=(
        "读取你已经选定的单个网页正文，返回高保真 Markdown。"
        "不要用它做大范围搜索。"
    ),
)
async def fetch_page(
    url: Annotated[str, "HTTP/HTTPS target URL point to the article/webpage."],
    ctx: Context = None,
) -> str:
    """
    高抗毁的网页全保真提取器，获得100%无删减结构化的原始 Markdown。
    首发 Tavily 进行高速解析；失败后会自动切换至 Firecrawl 并启用指数级防反爬虫等待时间自动重试。
    """
    await log_info(ctx, f"Starting fetch: {url}", config.debug_enabled)

    tavily_result = await _call_tavily_extract(url)
    if tavily_result:
        await log_info(ctx, "Fetch success using Tavily Extraction API.", config.debug_enabled)
        return tavily_result

    await log_info(ctx, "Tavily failed or unavailable. Falling back to Firecrawl scrape...", config.debug_enabled)
    firecrawl_result = await _call_firecrawl_scrape(url, ctx)
    if firecrawl_result:
        await log_info(ctx, "Fetch success using Firecrawl Resilient Scraper.", config.debug_enabled)
        return firecrawl_result

    await log_info(ctx, "Total Fetch Failed.", config.debug_enabled)
    if not config.tavily_api_key and not config.firecrawl_api_key:
        return "Extraction failed: TAVILY_API_KEY and FIRECRAWL_API_KEY both missing."
    return "Extraction completely failed. Page might resist scraping or requires CAPTCHA."


# ── MCP 工具 4：大范围网络拓扑 ─────────────────────────────────

@mcp.tool(
    name="map_docs_site",
    description=(
        "分析文档站点的页面结构与链接分布，适合梳理目录、导航和相关页面。"
        "不适合普通事实问答。"
    ),
)
async def map_docs_site(
    url: Annotated[str, "The starting URL to analyze."],
    max_depth: Annotated[int, "Max depth to crawl (1-5)."] = 1,
    limit: Annotated[int, "Max number of total links to process."] = 50,
    ctx: Context = None,
) -> str:
    """
    利用 Tavily 的站点映射分析系统对一个完整的网站进行拓扑图扫表（例如抓出一整个项目的所有文档页 URL）。
    """
    import httpx
    import json
    
    api_key = config.tavily_api_key
    if not api_key:
        return "Failed: TAVILY_API_KEY is not configured."
        
    await log_info(ctx, f"Starting topology map for: {url}", config.debug_enabled)
    endpoint = f"{config.tavily_api_url.rstrip('/')}/map"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "url": url, 
        "max_depth": max_depth, 
        "max_breadth": 20, 
        "limit": limit, 
        "timeout": 120
    }
    
    try:
        async with httpx.AsyncClient(timeout=130.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return json.dumps({
                "base_url": data.get("base_url", ""),
                "urls_found": data.get("results", []),
            }, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Web Mapping Error: {str(e)}"


# ====== Monkey Patch EventSourceResponse to fix Spring AI Client crash ======
import sse_starlette.sse
original_init = sse_starlette.sse.EventSourceResponse.__init__
def new_init(self, *args, **kwargs):
    kwargs['ping'] = 100000  # Disable the 15s keepalive ping
    original_init(self, *args, **kwargs)
sse_starlette.sse.EventSourceResponse.__init__ = new_init
# =========================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="ResearchMCP Server")
    parser.add_argument("--transport", default="sse", choices=["stdio", "sse"],
                        help="传输协议: sse(默认,用于Java集成) 或 stdio(用于fastmcp dev)")
    parser.add_argument("--host", default="0.0.0.0", help="SSE 监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="SSE 监听端口 (默认: 8765)")
    args = parser.parse_args()
    mcp.run(transport=args.transport, host=args.host, port=args.port)
