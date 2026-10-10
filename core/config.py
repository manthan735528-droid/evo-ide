#!/usr/bin/env python3
"""
Evo IDE V5.2 - Configuration & Sandbox/venv Bootstrap
=====================================================
Single source of truth for paths, constants, and feature flags.

V5.2 Critical Fixes:
- Added RUN_MODE: 'system' uses your Mac Python, 'sandbox' uses venv
- Added AUTO_RUN_ENABLED toggle (default False to prevent accidental runs)
- Added SYSTEM_PYTHON path detection

Mac M5 16GB RAM / 9B Parameter Optimization:
Token economy maximized for 16GB RAM.
"""
import os
import sys
import json
import logging
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

# ── Core Paths ─────────────────────────────────────────────
if getattr(sys, "frozen", False):
    # Running as a PyInstaller .exe: bundled read-only files (static/, etc.)
    # live under sys._MEIPASS, not next to this .py file.
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
CORE_DIR = BASE_DIR / "core"
CONFIG_FILE = Path(os.environ.get("EVO_CONFIG_FILE", BASE_DIR / "config.json"))


def load_engine_config() -> dict:
    """Load config.json. Missing file is non-fatal: env/defaults take over."""
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[config] config.json unreadable ({exc}); using defaults")
        return {}


ENGINE_CONFIG = load_engine_config()


def cfg(path: str, default=None):
    """Dotted lookup into config.json: cfg('llm.options.num_ctx', 8192)."""
    node = ENGINE_CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


PORT = int(os.environ.get("EVO_IDE_PORT", cfg("engine.port", 8000)))

SANDBOX_DIR = Path(
    os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])) or ".", "sandbox")
).resolve()
VENV_DIR = SANDBOX_DIR / ".venv"
MEMORY_FILE = SANDBOX_DIR / ".ide_memory.json"
INDEX_DIR = SANDBOX_DIR / ".index"
LOGS_DIR = SANDBOX_DIR / ".logs"
PLUGINS_DIR = SANDBOX_DIR / ".plugins"
TRANSACTIONS_DIR = SANDBOX_DIR / ".transactions"

# ── Execution Mode ─────────────────────────────────────────
# 'system' = Use the host Python/py launcher (recommended for frozen builds)
# 'sandbox' = Use an isolated venv when a host Python is available
RUN_MODE = os.environ.get("EVO_RUN_MODE", cfg("execution.mode", "system")).lower()

IS_FROZEN = bool(getattr(sys, "frozen", False))

# V6: Direct execution is ON by default — the system does the work.
AUTO_RUN_ENABLED = os.environ.get(
    "EVO_AUTO_RUN",
    "true" if cfg("agent.auto_run", True) else "false"
).lower() == "true"

# Detect a REAL host Python interpreter. A PyInstaller executable is NOT a
# Python interpreter and must never be used to execute workspace .py files.
if os.name == "nt":
    SYSTEM_PYTHON = (
        shutil.which("python")
        or shutil.which("py")
        or shutil.which("python3")
        or (sys.executable if not IS_FROZEN else "")
    )
else:
    SYSTEM_PYTHON = (
        shutil.which("python3")
        or shutil.which("python")
        or (sys.executable if not IS_FROZEN else "")
    )

# ── Model Configuration (V6: bound to Qwen3.5-9B abliterated) ──
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", cfg("llm.host", "http://localhost:11434"))
DEFAULT_MODEL = os.environ.get(
    "EVO_IDE_MODEL",
    cfg("llm.model", "huihui_ai/qwen3.5-abliterated:9b")
)
FALLBACK_MODELS = cfg("llm.fallback_models", [])
MODEL_KEEP_ALIVE = os.environ.get("EVO_KEEP_ALIVE", cfg("llm.keep_alive", "30m"))
MODEL_OPTIONS = dict(cfg("llm.options", {}) or {})
REASONING_CONFIG = dict(cfg("llm.reasoning", {}) or {})
LLM_REQUEST_TIMEOUT = int(cfg("llm.request_timeout", 600))
LLM_CONNECT_TIMEOUT = int(cfg("llm.connect_timeout", 10))
LLM_MAX_RETRIES = int(cfg("llm.max_retries", 3))
LLM_RETRY_BACKOFF = float(cfg("llm.retry_backoff", 1.6))
LLM_AUTO_PULL = bool(cfg("llm.auto_pull", False))

# ── Token Economy (V6) MAXIMIZED FOR 16GB RAM ────────────────
# 9B model in 4-bit (Q4) uses ~5.5GB. macOS uses ~3.5GB. 
# 28K num_ctx allows a massive context window without hitting swap.
MODEL_NUM_CTX = int(os.environ.get("EVO_NUM_CTX", MODEL_OPTIONS.get("num_ctx", 28672)))
MODEL_MAX_OUTPUT = int(os.environ.get("EVO_MAX_OUTPUT", MODEL_OPTIONS.get("num_predict", -1)))
# V7.1 FIX: MODEL_MAX_OUTPUT <= 0 means "unlimited output" BUT we now explicitly
# send a large num_predict to Ollama instead of omitting it. Ollama's default
# num_predict=-1 triggers an internal 10*num_ctx cap that causes finish_reason='length'
# on large files. Explicitly setting num_predict=512K prevents this while still
# being effectively unlimited. You can still cap output via EVO_MAX_OUTPUT env var.
CLOUD_MAX_OUTPUT_FALLBACK = int(os.environ.get("EVO_CLOUD_MAX_OUTPUT", 131072))  # 128K — effectively unlimited
# V7.1 FIX: When num_predict is -1/0 (unlimited), Ollama internally caps generation at
# 10 * num_ctx tokens. For a 28K context window that's ~286K tokens, but the model still
# reports finish_reason='length'. We explicitly set num_predict to a very large value
# to avoid this confusing truncation. This is ONLY used when MODEL_MAX_OUTPUT <= 0.
LOCAL_MAX_OUTPUT_FALLBACK = int(os.environ.get("EVO_LOCAL_MAX_OUTPUT", 524288))  # 512K tokens
# Context window = num_ctx - output reserve - safety margin
CHARS_PER_TOKEN = float(cfg("context.chars_per_token", 3.6))
HISTORY_TURNS = int(cfg("context.history_turns", 16))

AGENT_AUTO_APPLY = bool(cfg("agent.auto_apply", True))
AGENT_AUTO_RUN = bool(cfg("agent.auto_run", True))
AGENT_VALIDATE_BEFORE_APPLY = bool(cfg("agent.validate_before_apply", True))
AGENT_STRIP_RUN_DIRECTIVE = bool(cfg("agent.strip_run_directive", True))
AGENT_MAX_HEAL = int(cfg("agent.max_heal_attempts", 3))
AGENT_HEAL_BACKOFF = float(cfg("agent.heal_backoff_seconds", 0.5))
AGENT_AUTO_INSTALL = bool(cfg("agent.auto_install_missing_modules", True))
AGENT_MAX_FILES_PER_TURN = int(cfg("agent.max_files_per_turn", 12))

# ── Diff / Transaction Review (V7) ───────────────────────────
# When on, proposed writes are staged into a Transaction and shown to the
# user as a diff before anything touches disk. Off restores the old
# blind-apply behaviour for scripted/headless callers.
TRANSACTIONAL_APPLY = os.environ.get(
    "EVO_TRANSACTIONAL_APPLY",
    "true" if cfg("agent.transactional_apply", True) else "false"
).lower() == "true"
TRANSACTION_APPROVAL_TIMEOUT = int(
    os.environ.get("EVO_TX_APPROVAL_TIMEOUT", cfg("agent.transaction_approval_timeout", 600))
)
MAX_TRANSACTIONS_KEPT = int(cfg("agent.max_transactions_kept", 50))

# ── Deployment Mode (V7) ──────────────────────────────────────
# CLOUD_MODE marks a pay-per-token / hosted deployment. Autonomous Sprint is
# a token-cost liability there, so it defaults OFF and must be explicitly
# opted back in with EVO_SPRINT_ENABLED=true.
CLOUD_MODE = os.environ.get(
    "EVO_CLOUD_MODE", "true" if cfg("deployment.cloud_mode", False) else "false"
).lower() == "true"
_sprint_default = not CLOUD_MODE
SPRINT_ENABLED = os.environ.get(
    "EVO_SPRINT_ENABLED",
    "true" if cfg("agent.sprint_enabled", _sprint_default) else "false"
).lower() == "true"

# ── Adaptive Context (V7) ─────────────────────────────────────
# Recent turns are kept verbatim; anything older is collapsed into a
# one-line-per-turn summary instead of being silently dropped.
ADAPTIVE_HISTORY_RECENT_TURNS = int(cfg("context.adaptive_history_recent_turns", 6))
ADAPTIVE_HISTORY_SUMMARY_BUDGET = int(cfg("context.adaptive_history_summary_budget", 600))

# ── Execution Configuration ─────────────────────────────────
DEFAULT_TIMEOUT = int(os.environ.get("EVO_IDE_TIMEOUT", cfg("execution.timeout", 60)))
MAX_RETRIES = int(os.environ.get("EVO_IDE_MAX_RETRIES", 4))
RETRY_DELAY = float(os.environ.get("EVO_IDE_RETRY_DELAY", 1.0))

# ── Web Search (V6) ──────────────────────────────────────────
WEB_SEARCH_ENABLED = os.environ.get("EVO_WEB_SEARCH", "true").lower() == "true"
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("EVO_SEARCH_RESULTS", 4))
WEB_SEARCH_MAX_ROUNDS = int(os.environ.get("EVO_SEARCH_ROUNDS", 3))

# ── Autonomous Sprint (V6) ───────────────────────────────────
SPRINT_MAX_SECONDS = int(os.environ.get("EVO_SPRINT_SECONDS", 7200))   # 2h cap
SPRINT_MAX_ITERATIONS = int(os.environ.get("EVO_SPRINT_ITERATIONS", 200))
SPRINT_HEAL_LIMIT = int(os.environ.get("EVO_SPRINT_HEAL_LIMIT", 5))    # per iteration

# ── Pricing constants ("money saved vs. Claude" estimate) ────
CLAUDE_IN_PRICE_PER_M = 3.00
CLAUDE_OUT_PRICE_PER_M = 15.00

# ── Feature Flags ────────────────────────────────────────────
FEATURE_WEB_FETCH = os.environ.get("EVO_IDE_WEB_FETCH", "true").lower() == "true"
FEATURE_CANVAS_PANEL = os.environ.get("EVO_IDE_CANVAS_PANEL", "true").lower() == "true"
FEATURE_AUTO_FIX = os.environ.get("EVO_IDE_AUTO_FIX", "true").lower() == "true"
FEATURE_AUTO_APPROVE = os.environ.get("EVO_IDE_AUTO_APPROVE", "false").lower() == "true"

# ── Logging Setup ──────────────────────────────────────────
def setup_logging() -> None:
    """Configure application logging with rotation."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"evo_ide_{datetime.now():%Y%m%d}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

# ── Bootstrap ──────────────────────────────────────────────
def bootstrap_sandbox() -> None:
    """Create sandbox dir, memory file, starter file, and isolated venv."""
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSACTIONS_DIR.mkdir(parents=True, exist_ok=True)

    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(
            json.dumps(
                {"events": [], "projects": {}, "templates": {}, "web_cache": {}},
                indent=2
            ),
            encoding="utf-8",
        )

    if not (SANDBOX_DIR / "main.py").exists():
        (SANDBOX_DIR / "main.py").write_text(
            "# Evo IDE V5.2\n# File: main.py\nprint('System Online.')\n",
            encoding="utf-8"
        )

    # Only create a venv when sandbox mode is explicitly selected.
    # IMPORTANT: a PyInstaller .exe cannot act as `python -m venv`.
    if RUN_MODE == "sandbox" and not VENV_DIR.exists():
        if not SYSTEM_PYTHON:
            raise RuntimeError(
                "Sandbox mode requires a host Python installation. "
                "Install Python or switch EVO_RUN_MODE to system."
            )
        print("Creating sandbox virtual environment (for sandbox mode)...")
        subprocess.run([SYSTEM_PYTHON, "-m", "venv", str(VENV_DIR)], check=True)
        pip_path = VENV_DIR / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        subprocess.run(
            [str(pip_path), "install", "--upgrade", "pip"],
            capture_output=True
        )

# ── Venv Paths ─────────────────────────────────────────────
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
VENV_PIP = VENV_DIR / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")

# ── Logger Instance ────────────────────────────────────────
logger = logging.getLogger("evo_ide")
