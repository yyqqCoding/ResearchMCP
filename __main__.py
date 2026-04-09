"""
允许通过 python -m research_mcp 直接启动 MCP 服务器

默认 SSE 模式 (Java 集成):
    python -m research_mcp
    python -m research_mcp --port 8765

stdio 模式 (fastmcp dev 测试):
    python -m research_mcp --transport stdio
"""
from research_mcp.server import main

if __name__ == "__main__":
    main()
