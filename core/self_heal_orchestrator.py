#!/usr/bin/env python3
"""
Evo IDE - Autonomous Self-Heal Orchestrator (standalone CLI)
==============================================================
Execute -> Detect Error -> Analyze -> Targeted Diff Patch -> Re-Execute,
looped until the target exits 0 or the configured budget runs out.

Run with:
    python3 self_heal_orchestrator.py path/to/script.py
    python3 self_heal_orchestrator.py path/to/script.py --test-cmd "pytest -q"

Requires a local Ollama host serving a chat-capable model (same host/model
your Evo IDE server talks to). Standard library only.

DESIGN NOTES
------------
- ONE file, no companion modules — the loop, the subprocess execution, the
  patch-request prompt, and the patch application all live here.
- Patches are targeted, not full-file rewrites: the model returns
  `Fix: <file>` / `Line: <n>` / a fenced code block, and only the smallest
  enclosing function/class (or, failing that, the single line) is replaced.
  Every patch is validated with ast.parse() before it's written to disk, and
  rejected (with the reason fed back into the next attempt) if it doesn't
  parse.
- The loop is bounded by --max-iterations and --max-minutes. Both default to
  generous-but-finite values and can be raised as high as you want — an
  unbounded-by-default loop that reapplies a bad patch forever isn't useful
  to anyone, so the defaults exist to catch that case, not to limit you.
- A .bak copy of the original file is made before the first patch, so a
  failed run never leaves you without the pre-heal version.
- urllib.request.urlopen() is never called with a `headers` kwarg — headers
  always go on the Request object (this is the #1 crash mode in this
  codebase per prompts.py DIRECTIVE 7, and it applies here too).
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Defaults ─────────────────────────────────────────────────────────────
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "huihui_ai/qwen3.5-abliterated:9b"
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_MINUTES = 30
DEFAULT_RUN_TIMEOUT = 30          # per-execution subprocess timeout (seconds)
DEFAULT_LLM_TIMEOUT = 180         # per-request LLM timeout (seconds)
DEFAULT_HEAL_BACKOFF = 0.5
DEFAULT_TEMPERATURE = 0.1

FIX_BLOCK_RE = re.compile(
    r"Fix:\s*([^\n]+)\nLine:\s*(\d+)\n```(?:[a-zA-Z0-9+#.\-]*)\n(.*?)```",
    re.DOTALL,
)
THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
RUN_CODE_RE = re.compile(r"\n?\[RUN_CODE\]\s*\n?")
MODULE_NOT_FOUND_PATTERNS = [
    re.compile(r"ModuleNotFoundError: No module named '([^']+)'"),
    re.compile(r"ImportError: No module named '([^']+)'"),
]

SYSTEM_PROMPT = """You are a targeted code-repair engine. You are given one
Python file that just failed to run, plus its error output. Your job is to
fix ONLY the specific bug that caused the failure, with the smallest possible
change.

RULES - all absolute:
1. Emit EXACTLY ONE `Fix:` block. Format, byte-exact:

Fix: <filename>
Line: <line number where the fix should be centered>
```python
<ONLY the replacement code - a single line, or a full replacement for the
function/class that contains that line. Never the whole file.>
```

2. Preserve the EXACT indentation of the code you are replacing. If you are
   replacing a method body, keep it at method depth; a dedented replacement
   corrupts the file.
3. Do not rewrite, refactor, or "improve" anything that isn't broken. Fix
   only what the error output points to.
4. Do not invent new files. Do not remove the `Fix:`/`Line:` header format.
5. A missing third-party module is not something you need to fix in code -
   it will be auto-installed. Address the actual traceback instead.
6. urllib.request.urlopen() takes no `headers` kwarg. Headers go on
   urllib.request.Request(url, headers=...).
7. You may reason briefly inside a single <think>...</think> block before
   the Fix: block if it helps you, but the Fix: block is the only thing that
   gets applied - everything else is discarded.
"""


# ── Subprocess execution ────────────────────────────────────────────────

def run_target(cmd: List[str], cwd: Path, timeout: int) -> Dict[str, Any]:
    start = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration": time.time() - start,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\n[TIMEOUT after {timeout}s]",
            "duration": time.time() - start,
            "timed_out": True,
        }
    except FileNotFoundError as exc:
        return {
            "success": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"Could not launch command {cmd}: {exc}",
            "duration": time.time() - start,
            "timed_out": False,
        }


def detect_missing_module(stderr: str) -> Optional[str]:
    for pattern in MODULE_NOT_FOUND_PATTERNS:
        match = pattern.search(stderr)
        if match:
            return match.group(1).split(".")[0]
    return None


def install_module(module: str, python_exe: str) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            [python_exe, "-m", "pip", "install", module],
            capture_output=True, text=True, timeout=180,
        )
        return result.returncode == 0, (result.stdout + result.stderr)[-2000:]
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)


# ── Ollama transport (standalone, stdlib only) ──────────────────────────

def call_ollama(messages: List[Dict[str, str]], model: str, host: str,
                 temperature: float = DEFAULT_TEMPERATURE,
                 timeout: int = DEFAULT_LLM_TIMEOUT) -> str:
    url = host.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    data = json.dumps(payload).encode("utf-8")
    # Headers ALWAYS go on Request(), never on urlopen().
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {host}: {exc.reason}. "
            f"Is `ollama serve` running and is '{model}' pulled?"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc

    message = body.get("message") or {}
    content = message.get("content") or ""
    content = THINK_TAG_RE.sub("", content).strip()
    return content


# ── Patch parsing & application ─────────────────────────────────────────

def parse_fix_block(response: str) -> Optional[Tuple[str, int, str]]:
    match = FIX_BLOCK_RE.search(response)
    if not match:
        return None
    filename = match.group(1).strip()
    line_no = int(match.group(2))
    code = RUN_CODE_RE.sub("\n", match.group(3)).strip("\n")
    if not code:
        return None
    return filename, line_no, code


def _find_enclosing_block(code: str, line_num: int) -> Optional[Tuple[int, int]]:
    """Smallest enclosing function/class def containing line_num (1-indexed).
    Returns a 0-indexed [start, end) line slice, or None if line_num isn't
    inside one (e.g. it's module-level code)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    best: Optional[Tuple[int, int]] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", node.lineno)
            if start <= line_num <= end:
                if best is None or (end - start) < (best[1] - best[0]):
                    best = (start, end)
    if best is None:
        return None
    return best[0] - 1, best[1]


def apply_targeted_fix(code: str, line_num: int, fix_code: str) -> str:
    """Replace the smallest enclosing function/class containing line_num, or
    (if line_num isn't inside one) just that single line. Never touches the
    rest of the file."""
    lines = code.split("\n")
    total = len(lines)
    line_num = max(1, min(line_num, total)) if total else 1

    block = _find_enclosing_block(code, line_num)
    if block:
        start, end = block
        start = max(0, min(start, total))
        end = max(0, min(end, total))
        lines[start:end] = fix_code.split("\n")
    else:
        idx = line_num - 1
        if 0 <= idx < len(lines):
            lines[idx] = fix_code
        else:
            lines.append(fix_code)
    return "\n".join(lines)


def numbered(code: str) -> str:
    lines = code.split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}: {line}" for i, line in enumerate(lines))


def build_heal_messages(target_name: str, code: str, error_output: str,
                         attempt: int, last_rejection: str = "") -> List[Dict[str, str]]:
    user = (
        f"FILE: {target_name}\n"
        f"ATTEMPT: {attempt}\n\n"
        f"CURRENT FILE CONTENT (line numbers are for reference only - do not "
        f"include them in your Fix: code):\n{numbered(code)}\n\n"
        f"LATEST ERROR OUTPUT:\n{error_output[-3000:]}\n"
    )
    if last_rejection:
        user += f"\nYour previous patch was rejected: {last_rejection}\nDo not repeat that mistake.\n"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ── Logging ──────────────────────────────────────────────────────────────

def log_event(log_path: Path, event: Dict[str, Any]) -> None:
    event = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass  # logging is best-effort; never let it break the heal loop


# ── Main loop ────────────────────────────────────────────────────────────

def heal(args: argparse.Namespace) -> int:
    target = Path(args.file).resolve()
    if not target.exists():
        print(f"[error] file not found: {target}")
        return 2

    backup = target.with_suffix(target.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(target, backup)
        print(f"[backup] original preserved at {backup}")
    else:
        print(f"[backup] existing backup found at {backup} (not overwritten)")

    python_exe = sys.executable
    cmd = shlex.split(args.test_cmd) if args.test_cmd else [python_exe, str(target)]

    log_path = target.with_suffix(target.suffix + ".heal_log.jsonl")
    print(f"[log] iteration log: {log_path}")
    print(
        f"[budget] up to {args.max_iterations} iterations, "
        f"{args.max_minutes} min wall-clock, {args.run_timeout}s per run "
        f"(raise with --max-iterations / --max-minutes / --run-timeout)"
    )

    start_time = time.time()
    last_rejection = ""

    for iteration in range(1, args.max_iterations + 1):
        elapsed_min = (time.time() - start_time) / 60
        if elapsed_min > args.max_minutes:
            print(f"\n[stop] wall-clock budget of {args.max_minutes} min exhausted")
            break

        print(f"\n=== Iteration {iteration}/{args.max_iterations} — executing {' '.join(cmd)} ===")
        result = run_target(cmd, cwd=target.parent, timeout=args.run_timeout)
        log_event(log_path, {
            "iteration": iteration, "phase": "execute",
            "success": result["success"], "returncode": result["returncode"],
            "timed_out": result["timed_out"], "duration": round(result["duration"], 2),
        })

        if result["success"]:
            print(f"[success] exit 0 after {iteration} iteration(s), {elapsed_min:.1f} min elapsed")
            if result["stdout"].strip():
                print("--- stdout ---")
                print(result["stdout"][-2000:])
            return 0

        stderr = result["stderr"]
        print(f"[fail] returncode={result['returncode']} timed_out={result['timed_out']}")
        print(stderr[-1500:] if stderr else "(no stderr captured)")

        module = detect_missing_module(stderr)
        if module and args.auto_install:
            print(f"[heal] missing module detected: '{module}' — installing")
            installed, output = install_module(module, python_exe)
            log_event(log_path, {"iteration": iteration, "phase": "install",
                                  "module": module, "ok": installed})
            if installed:
                print(f"[heal] installed '{module}', re-running without an LLM call")
                continue
            print(f"[heal] install of '{module}' failed:\n{output}")

        try:
            code = target.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[error] could not read {target}: {exc}")
            break

        print("[heal] requesting a targeted patch from the model...")
        messages = build_heal_messages(target.name, code, stderr, iteration, last_rejection)
        try:
            response = call_ollama(messages, model=args.model, host=args.ollama_url,
                                    timeout=args.llm_timeout)
        except RuntimeError as exc:
            print(f"[error] {exc}")
            log_event(log_path, {"iteration": iteration, "phase": "llm_error", "error": str(exc)})
            break

        parsed = parse_fix_block(response)
        if not parsed:
            last_rejection = "response contained no parsable `Fix:` block"
            print(f"[heal] {last_rejection}; retrying")
            log_event(log_path, {"iteration": iteration, "phase": "parse_failed"})
            time.sleep(args.heal_backoff)
            continue

        fix_file, line_no, fix_code = parsed
        if fix_file not in (target.name, str(target)):
            print(f"[heal] model targeted '{fix_file}' but only '{target.name}' is in "
                  f"scope for this run — applying to {target.name} anyway")

        patched = apply_targeted_fix(code, line_no, fix_code)
        try:
            ast.parse(patched)
        except SyntaxError as exc:
            last_rejection = f"patch produced a SyntaxError at line {exc.lineno}: {exc.msg}"
            print(f"[heal] rejected patch — {last_rejection}")
            log_event(log_path, {"iteration": iteration, "phase": "patch_rejected",
                                  "reason": last_rejection})
            time.sleep(args.heal_backoff)
            continue

        diff = "\n".join(difflib.unified_diff(
            code.splitlines(), patched.splitlines(),
            fromfile=f"{target.name} (before)", tofile=f"{target.name} (after)",
            lineterm="",
        ))
        print("[heal] applying patch:")
        print(diff[:3000] if diff else "(no textual diff — identical content)")

        target.write_text(patched, encoding="utf-8")
        log_event(log_path, {"iteration": iteration, "phase": "patch_applied",
                              "line": line_no, "diff": diff[:4000]})
        last_rejection = ""
        time.sleep(args.heal_backoff)

    print(f"\n[stopped] no clean run within the configured budget. "
          f"Original file is safe at {backup}; current (possibly still broken) "
          f"state is at {target}.")
    return 1


# ── CLI ──────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Autonomous self-heal loop: run a file, patch the exact "
                    "failure, re-run, repeat until it exits clean or the "
                    "budget runs out.",
    )
    p.add_argument("file", help="Target Python file to run and heal.")
    p.add_argument("--test-cmd", default=None,
                   help="Run this shell command instead of `python3 <file>` "
                        "(e.g. \"pytest -q\"). Patches still apply to <file>.")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    p.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES)
    p.add_argument("--run-timeout", type=int, default=DEFAULT_RUN_TIMEOUT,
                   help="Per-execution subprocess timeout in seconds.")
    p.add_argument("--llm-timeout", type=int, default=DEFAULT_LLM_TIMEOUT)
    p.add_argument("--heal-backoff", type=float, default=DEFAULT_HEAL_BACKOFF,
                   help="Seconds to sleep between iterations.")
    p.add_argument("--auto-install", dest="auto_install", action="store_true", default=True,
                   help="Auto pip-install missing modules instead of asking the LLM (default: on).")
    p.add_argument("--no-auto-install", dest="auto_install", action="store_false")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        return heal(args)
    except KeyboardInterrupt:
        print("\n[interrupted] stopped by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
