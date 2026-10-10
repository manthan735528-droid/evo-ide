#!/usr/bin/env python3
"""
Evo IDE V5.1 - Shared In-Memory State

V5.1 Changes:
- Added web_sources list for scraped/fetched URL tracking
- Added thinking_enabled flag for session
"""
import threading
import queue
import uuid
from typing import Dict, Any, List
from dataclasses import dataclass, field


@dataclass
class SessionState:
    """Per-session state container for multi-user support."""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    input_tokens: int = 0
    output_tokens: int = 0
    money_saved: float = 0.0
    files_created: int = 0
    executions: int = 0
    fixes_applied: int = 0
    context_chunks: int = 0
    web_requests: int = 0
    web_cache_hits: int = 0
    web_sources: List[Dict[str, Any]] = field(default_factory=list)
    thinking_enabled: bool = False


# ── Global Session State ─────────────────────────────────────
session_stats = SessionState()

# ── Active Streaming Control ─────────────────────────────────
active_streams: Dict[str, Dict[str, Any]] = {}
stream_counter = 0
stream_lock = threading.Lock()

# ── Web Fetch Cache ────────────────────────────────────────
web_cache: Dict[str, Dict[str, Any]] = {}
web_cache_lock = threading.Lock()
WEB_CACHE_MAX_AGE = 3600  # seconds

# ── Task Queue (for async operations) ──────────────────────
task_queue = queue.Queue()
task_results: Dict[str, Any] = {}
task_lock = threading.Lock()

# ── Terminal Command History ───────────────────────────────
terminal_history: list = []
history_index = -1

# ── File Operation Lock ────────────────────────────────────
file_lock = threading.Lock()

# ── Approval Registry ──────────────────────────────────────
approval_registry: Dict[str, Any] = {}
approval_lock = threading.Lock()
