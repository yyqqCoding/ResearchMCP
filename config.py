"""
ResearchMCP 配置管理
从环境变量读取 Grok / Tavily / Firecrawl 配置，单例模式。
"""

import os
from pathlib import Path

# 尝试加载当前目录下的 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class Config:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── Grok ──────────────────────────────────────────────
    @property
    def grok_api_url(self) -> str:
        url = os.getenv("GROK_API_URL")
        if not url:
            raise ValueError(
                "GROK_API_URL 未配置！请设置环境变量 GROK_API_URL（OpenAI 兼容格式）"
            )
        return url

    @property
    def grok_api_key(self) -> str:
        key = os.getenv("GROK_API_KEY")
        if not key:
            raise ValueError("GROK_API_KEY 未配置！请设置环境变量 GROK_API_KEY")
        return key

    @property
    def grok_model(self) -> str:
        return os.getenv("GROK_MODEL", "grok-4-fast")

    # ── Tavily ────────────────────────────────────────────
    @property
    def tavily_api_url(self) -> str:
        return os.getenv("TAVILY_API_URL", "https://api.tavily.com")

    @property
    def tavily_api_key(self) -> str | None:
        return os.getenv("TAVILY_API_KEY")

    # ── Firecrawl ─────────────────────────────────────────
    @property
    def firecrawl_api_url(self) -> str:
        return os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2")

    @property
    def firecrawl_api_key(self) -> str | None:
        return os.getenv("FIRECRAWL_API_KEY")

    # ── Retry ─────────────────────────────────────────────
    @property
    def retry_max_attempts(self) -> int:
        return int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))

    @property
    def retry_multiplier(self) -> float:
        return float(os.getenv("RETRY_MULTIPLIER", "1"))

    @property
    def retry_max_wait(self) -> int:
        return int(os.getenv("RETRY_MAX_WAIT", "10"))

    # ── Debug ─────────────────────────────────────────────
    @property
    def debug_enabled(self) -> bool:
        return os.getenv("RESEARCH_MCP_DEBUG", "false").lower() in ("true", "1", "yes")

    @property
    def log_level(self) -> str:
        return os.getenv("RESEARCH_MCP_LOG_LEVEL", "INFO").upper()

    @property
    def log_dir(self) -> Path:
        log_dir_str = os.getenv("RESEARCH_MCP_LOG_DIR", "logs")
        log_dir = Path(log_dir_str)
        if log_dir.is_absolute():
            return log_dir
        home_log_dir = Path.home() / ".config" / "research-mcp" / log_dir_str
        try:
            home_log_dir.mkdir(parents=True, exist_ok=True)
            return home_log_dir
        except OSError:
            pass
        cwd_log_dir = Path.cwd() / log_dir_str
        try:
            cwd_log_dir.mkdir(parents=True, exist_ok=True)
            return cwd_log_dir
        except OSError:
            pass
        import tempfile
        tmp_log_dir = Path(tempfile.gettempdir()) / "research-mcp" / log_dir_str
        tmp_log_dir.mkdir(parents=True, exist_ok=True)
        return tmp_log_dir

    # ── Helpers ───────────────────────────────────────────
    @staticmethod
    def _mask_key(key: str | None) -> str:
        if not key or len(key) <= 8:
            return "***"
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def get_config_info(self) -> dict:
        try:
            grok_url = self.grok_api_url
            grok_key_masked = self._mask_key(self.grok_api_key)
            status = "✅ Grok 配置完整"
        except ValueError as e:
            grok_url = "未配置"
            grok_key_masked = "未配置"
            status = f"❌ {e}"

        return {
            "GROK_API_URL": grok_url,
            "GROK_API_KEY": grok_key_masked,
            "GROK_MODEL": self.grok_model,
            "TAVILY_API_URL": self.tavily_api_url,
            "TAVILY_API_KEY": self._mask_key(self.tavily_api_key) if self.tavily_api_key else "未配置",
            "FIRECRAWL_API_URL": self.firecrawl_api_url,
            "FIRECRAWL_API_KEY": self._mask_key(self.firecrawl_api_key) if self.firecrawl_api_key else "未配置",
            "config_status": status,
        }


config = Config()
