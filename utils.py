"""
ResearchMCP 工具函数
"""

import re
from typing import List

_URL_PATTERN = re.compile(r'https?://[^\s<>"\'`，。、；：！？》）】\)]+')


def extract_unique_urls(text: str) -> list[str]:
    """从文本中提取所有唯一 URL，按首次出现顺序排列"""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_PATTERN.finditer(text):
        url = m.group().rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls
