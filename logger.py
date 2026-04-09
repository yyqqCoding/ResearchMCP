"""
ResearchMCP 日志工具
"""

import logging
from datetime import datetime
from pathlib import Path
from research_mcp.config import config

logger = logging.getLogger("research_mcp")
logger.setLevel(getattr(logging, config.log_level, logging.INFO))

_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

try:
    log_dir = config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"research_mcp_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(getattr(logging, config.log_level, logging.INFO))
    file_handler.setFormatter(_formatter)
    logger.addHandler(file_handler)
except OSError:
    logger.addHandler(logging.NullHandler())


async def log_info(ctx, message: str, is_debug: bool = False):
    """向 MCP Context 和文件日志同时输出"""
    if is_debug:
        logger.info(message)
    if ctx:
        await ctx.info(message)
