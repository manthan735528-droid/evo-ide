from .ssl_fix import apply_ssl_fix  # noqa: F401  Auto-fix SSL on import
#!/usr/bin/env python3
"""
Evo IDE V6.1 - Core Package
===========================
Backend engine modules. Entry points:

    from core.ollama_client import ollama_client
    from core.agent_orchestrator import orchestrator
    from core.stream_handler import StreamSession, ChunkedWriter

Model host is bound to huihui_ai/deepseek-r1-abliterated:14b via config.json.
"""

__version__ = "6.1.0"
__all__ = [
    "config",
    "state",
    "parsing",
    "indexing",
    "lsp",
    "diff_engine",
    "streaming",
    "stream_handler",
    "backend_utils",
    "context_manager",
    "ollama_client",
    "agent_orchestrator",
    "server_handler",
    "prompts",
    "templates",
    "web_fetch",
]
