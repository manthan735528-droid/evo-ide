#!/usr/bin/env python3
"""
Evo IDE V6 - Agent Orchestrator
===============================
The core execution engine. Owns the full agent turn:

  budgeted prompt -> Ollama stream -> artifact parsing -> static validation
  -> filesystem apply -> execution -> self-heal loop -> metrics

Model host: huihui_ai/deepseek-r1-abliterated:14b via core.ollama_client.

Design rules:
- Reasoning tokens are streamed separately and NEVER written to disk.
- Nothing is applied to the workspace until it parses (Python files).
- Self-heal targets the file that actually failed, bounded by max_heal.
- Every emitted event is a flat JSON-serialisable dict for transport reuse.
"""
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

from . import config as runtime_config
from .backend_utils import (
    append_memory, detect_missing_module, extract_file_blocks,
    extract_fix_blocks, get_terminal_debug_context, install_module, run_code,
    update_metrics, write_file, add_web_source,
)
from .config import (
    AGENT_AUTO_APPLY, AGENT_AUTO_INSTALL, AGENT_MAX_FILES_PER_TURN,
    AGENT_MAX_HEAL, AGENT_HEAL_BACKOFF,
    AGENT_VALIDATE_BEFORE_APPLY, FEATURE_WEB_FETCH, HISTORY_TURNS,
    SANDBOX_DIR, SPRINT_MAX_ITERATIONS, SPRINT_MAX_SECONDS, logger,
)
from .context_manager import context_manager
from .diff_engine import DiffEngine
from .indexing import vector_index
from .lsp import LSPValidator
from .ollama_client import OllamaClient, ollama_client
from . import patch_transaction as tx_engine
from .prompts import (
    CHAT_SYSTEM_PROMPT, SELF_HEAL_PROMPT, THINKING_OFF_PROMPT,
    THINKING_ON_PROMPT, WEB_FETCH_PROMPT,
)
from .state import session_stats
from .web_fetch import get_web_context

URL_RE = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')
FILE_HEADER_RE = re.compile(r'File:\s*([^\n]+)\n```[a-zA-Z0-9+#.-]*\n')
FENCE = "```"
Emit = Callable[[Dict[str, Any]], None]


@dataclass
class AppliedFile:
    path: str
    lines: int
    mode: str
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class TurnResult:
    ok: bool = False
    response: str = ""
    reasoning: str = ""
    files: List[AppliedFile] = field(default_factory=list)
    executions: List[Dict[str, Any]] = field(default_factory=list)
    heals: int = 0
    stopped: bool = False
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    token_report: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "response": self.response,
            "reasoning": self.reasoning,
            "files": [f.__dict__ for f in self.files],
            "executions": self.executions,
            "heals": self.heals,
            "stopped": self.stopped,
            "error": self.error,
            "usage": self.usage,
            "token_report": self.token_report,
        }


class ArtifactTracker:
    """Detects `File: path` + fenced blocks inside a live token stream so the
    UI can render code as it is generated, before the turn completes."""

    def __init__(self):
        self.buffer = ""
        self.cursor = 0
        self.active: Optional[str] = None
        self.body_start = 0
        self.emitted = 0

    def feed(self, delta: str) -> List[Dict[str, Any]]:
        self.buffer += delta
        events: List[Dict[str, Any]] = []
        while True:
            if self.active is None:
                match = FILE_HEADER_RE.search(self.buffer, self.cursor)
                if not match:
                    break
                self.active = match.group(1).strip()
                self.body_start = match.end()
                self.emitted = self.body_start
                events.append({"event": "artifact_start", "file": self.active})
                continue

            close = self.buffer.find(FENCE, self.emitted)
            if close >= 0:
                tail = self.buffer[self.emitted:close]
                if tail:
                    events.append({"event": "artifact_delta", "file": self.active, "text": tail})
                events.append({
                    "event": "artifact_end",
                    "file": self.active,
                    "text": self.buffer[self.body_start:close],
                })
                self.cursor = close + len(FENCE)
                self.active = None
                continue

            safe_end = max(self.emitted, len(self.buffer) - len(FENCE))
            if safe_end > self.emitted:
                events.append({
                    "event": "artifact_delta",
                    "file": self.active,
                    "text": self.buffer[self.emitted:safe_end],
                })
                self.emitted = safe_end
            break
        return events


class AgentOrchestrator:
    """Connects the LLM host to the workspace and drives the agent loop."""

    # Short-term conversation memory, persisted to disk so it survives a
    # server restart instead of vanishing with the in-RAM deque. This is
    # deliberately separate from the long-term project index (vector_index)
    # and from the append-only .ide_memory.json event log.
    _HISTORY_FILE = SANDBOX_DIR / ".chat_memory.json"

    def __init__(self, client: OllamaClient = ollama_client):
        self.client = client
        self.history: Deque[Dict[str, str]] = deque(maxlen=HISTORY_TURNS * 2)
        self._load_history()

    # ── short-term memory persistence ────────────────────────

    def _load_history(self) -> None:
        """Restore recent conversation turns from the last session."""
        try:
            if self._HISTORY_FILE.exists():
                raw = json.loads(self._HISTORY_FILE.read_text(encoding='utf-8'))
                if isinstance(raw, list):
                    for turn in raw[-(HISTORY_TURNS * 2):]:
                        if isinstance(turn, dict) and 'role' in turn and 'content' in turn:
                            self.history.append(turn)
        except Exception as exc:
            logger.warning(f"Could not restore chat memory: {exc}")

    def _persist_history(self) -> None:
        """Flush the current short-term memory window to disk immediately."""
        try:
            self._HISTORY_FILE.write_text(
                json.dumps(list(self.history), indent=2), encoding='utf-8')
        except Exception as exc:
            logger.warning(f"Could not persist chat memory: {exc}")

    # ── prompt assembly ──────────────────────────────────────

    def _system_core(self, thinking: bool) -> str:
        mode = THINKING_ON_PROMPT if thinking else THINKING_OFF_PROMPT
        return f"{CHAT_SYSTEM_PROMPT}\n{mode}"

    def _web_section(self, user_prompt: str, emit: Emit) -> str:
        if not FEATURE_WEB_FETCH:
            return ""
        urls = URL_RE.findall(user_prompt)
        if not urls:
            return ""
        emit({"event": "status", "text": f"Fetching {len(urls)} URL(s)"})
        raw = get_web_context(urls, max_chars=int(3000 * 3.6 / max(len(urls), 1)))
        if not raw.strip():
            return ""
        session_stats.web_requests += len(urls)
        for url in urls:
            add_web_source(url, title=url[:60], status="fetched")
        return WEB_FETCH_PROMPT.format(content=raw)

    def build_prompt(self, user_prompt: str, active_file: str, cursor_line: int,
                     thinking: bool, emit: Emit,
                     extra: str = "",
                     attachments: Optional[List[str]] = None
                     ) -> Tuple[str, Dict[str, int]]:
        web_block = self._web_section(user_prompt, emit)
        return context_manager.build_prompt(
            system_core=self._system_core(thinking),
            user_prompt=user_prompt,
            active_file=active_file,
            cursor_line=cursor_line,
            terminal_raw=get_terminal_debug_context(),
            web_raw=web_block,
            history=list(self.history),
            extra=extra,
            attachments=attachments,
        )

    # ── streaming ────────────────────────────────────────────

    def _stream(self, system_prompt: str, user_prompt: str, emit: Emit,
                thinking: bool,
                should_stop: Optional[Callable[[], bool]]
                ) -> Tuple[str, str, Dict[str, Any], bool, str, Optional[str]]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        update_metrics(system_prompt + user_prompt, is_input=True)

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tracker = ArtifactTracker()
        usage: Dict[str, Any] = {}
        stopped = False
        error = ""
        reasoning_open = False

        t_start = time.monotonic()
        first_token_at: Optional[float] = None
        content_tokens = 0
        reasoning_tokens = 0
        peak_tps = 0.0
        last_emit_at = t_start
        last_emit_tokens = 0
        syntax_bad = 0
        last_transport: Dict[str, Any] = {}
        finish_reason: Optional[str] = None
        TELEMETRY_INTERVAL = 0.35

        def emit_telemetry(force: bool = False) -> None:
            nonlocal last_emit_at, last_emit_tokens, peak_tps
            now = time.monotonic()
            if not force and (now - last_emit_at) < TELEMETRY_INTERVAL:
                return
            total_tokens = content_tokens + reasoning_tokens
            since = now - last_emit_at
            inst_tps = ((total_tokens - last_emit_tokens) / since) if since > 0 else 0.0
            peak_tps = max(peak_tps, inst_tps)
            avg_tps = (total_tokens / (now - first_token_at)) if first_token_at and now > first_token_at else 0.0
            ttft_ms = int((first_token_at - t_start) * 1000) if first_token_at else None
            fences_balanced = tracker.active is None
            score = 100
            if syntax_bad:
                score -= min(60, syntax_bad * 20)
            if not fences_balanced:
                score -= 10
            emit({
                "event": "telemetry",
                "metrics": {
                    "tps": round(inst_tps, 1),
                    "peak_tps": round(peak_tps, 1),
                    "avg_tps": round(avg_tps, 1),
                    "ttft_ms": ttft_ms,
                    "tokens": total_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "content_tokens": content_tokens,
                    "elapsed_ms": int((now - t_start) * 1000),
                },
                "health": {
                    "score": max(0, score),
                    "syntax_bad": syntax_bad,
                    "fences_balanced": fences_balanced,
                },
                "transport": {
                    "frames": last_transport.get("frames", 0),
                    "dropped": last_transport.get("dropped", 0),
                },
            })
            last_emit_at = now
            last_emit_tokens = total_tokens

        for event in self.client.stream_chat(messages, think=thinking, should_stop=should_stop):
            if event.kind == "reasoning":
                if first_token_at is None:
                    first_token_at = time.monotonic()
                if not reasoning_open:
                    reasoning_open = True
                    emit({"event": "thinking_start"})
                reasoning_parts.append(event.text)
                reasoning_tokens += max(len(event.text) // 4, 1)
                emit({"event": "thinking", "text": event.text})
                emit_telemetry()
            elif event.kind == "content":
                if first_token_at is None:
                    first_token_at = time.monotonic()
                if reasoning_open:
                    reasoning_open = False
                    emit({"event": "thinking_end"})
                content_parts.append(event.text)
                content_tokens += max(len(event.text) // 4, 1)
                update_metrics(event.text, is_input=False)
                emit({"event": "token", "text": event.text})
                for artifact_event in tracker.feed(event.text):
                    emit(artifact_event)
                    if artifact_event["event"] == "artifact_end" and artifact_event["file"].endswith(".py"):
                        try:
                            compile(artifact_event["text"], artifact_event["file"], "exec")
                        except SyntaxError:
                            syntax_bad += 1
                emit_telemetry()
            elif event.kind == "usage":
                usage = event.meta
                last_transport = event.meta.get("transport", {}) or last_transport
                emit({"event": "usage", **event.meta})
            elif event.kind == "error":
                error = event.text
                emit({"event": "error", "text": event.text})
            elif event.kind == "done":
                stopped = bool(event.meta.get("stopped"))
                usage = event.meta.get("usage", usage)
                last_transport = event.meta.get("transport", {}) or last_transport
                # V6.1 FIX: finish_reason was parsed out of the provider's SSE
                # payload (see ollama_client._openai_stream_parser) but never
                # looked at here, so a hard output-length cutoff on big
                # generations was indistinguishable from a clean finish.
                finish_reason = event.meta.get("reason")
                if finish_reason in ("length", "max_tokens", "max_output_tokens"):
                    emit({"event": "status",
                          "text": "Model hit its output-length limit before finishing "
                                  "— the response may be truncated"})

        if reasoning_open:
            emit({"event": "thinking_end"})
        emit_telemetry(force=True)
        return "".join(content_parts), "".join(reasoning_parts), usage, stopped, error, finish_reason

    # ── artifact application ─────────────────────────────────

    @staticmethod
    def _validate(path: str, code: str) -> Tuple[bool, List[Dict[str, Any]]]:
        if not path.endswith(".py"):
            return True, []
        diagnostics = LSPValidator.validate_python(code, path)
        fatal = [d for d in diagnostics if d["severity"] == "error"]
        return not fatal, diagnostics

    def _gather_candidates(self, response: str, active_file: str,
                           emit: Emit, finish_reason: Optional[str] = None,
                           combined_text: Optional[str] = None) -> List[tuple]:
        """Validate every proposed change and return
        (path, mode, old_content, new_content, diagnostics, extra) tuples
        for anything safe enough to offer for review. Nothing is written
        here — this is pure parsing + validation.

        V7.1: combined_text allows passing content+reasoning concatenated to catch
        file blocks that span the boundary (File: header in content, closing fence
        in reasoning, or vice versa)."""
        candidates: List[tuple] = []
        seen: set = set()

        # Try the primary text first, then combined if provided and primary failed
        texts_to_try = [response]
        if combined_text and combined_text != response:
            texts_to_try.append(combined_text)

        for text in texts_to_try:
            file_blocks = extract_file_blocks(text)[:AGENT_MAX_FILES_PER_TURN]
            fix_blocks = extract_fix_blocks(text)

            if file_blocks or fix_blocks:
                # Found blocks in this text, use them
                break
        else:
            # Neither text had blocks — try native fallback
            file_blocks = []
            fix_blocks = []

        if not file_blocks and not fix_blocks:
            # Try combined text for fallback too
            fallback_text = combined_text if combined_text else response
            fallback = DiffEngine.native_fallback_patch(
                fallback_text, active_file,
                (SANDBOX_DIR / active_file).read_text(encoding="utf-8")
                if (SANDBOX_DIR / active_file).exists() else None)
            if fallback:
                file_blocks = [fallback]
                emit({"event": "status",
                      "text": f"No File:/Fix: block found — native diff engine "
                              f"inferred a full-file patch for {fallback[0]}"})

        for filename, code, truncated in file_blocks:
            if filename in seen:
                continue
            seen.add(filename)
            new_content = code if code.endswith("\n") else code + "\n"
            ok, diagnostics = self._validate(filename, new_content)
            if truncated:
                # V6.1 FIX: distinguish *why* it's truncated instead of always
                # guessing "output/token limit" — the provider's own
                # finish_reason tells us whether it actually hit a token cap,
                # versus the stream simply being cut off mid-flight (dropped
                # connection, Cloudflare block, timeout), which needs a very
                # different fix.
                if finish_reason in ("length", "max_tokens", "max_output_tokens"):
                    cause = ("the provider reported finish_reason="
                             f"'{finish_reason}' — it hit its own output-token cap")
                    suggestion = ("Raise the model's output limit (num_predict / "
                                  "max_tokens), or ask for the file in smaller "
                                  "pieces and retry.")
                else:
                    cause = ("no output-limit was reported by the provider, so this "
                             "looks like the connection was cut mid-stream rather "
                             "than a token limit (network drop, timeout, or a "
                             "Cloudflare block on the cloud host)")
                    suggestion = ("If this is a Cerebras/cloud host, install "
                                  "curl_cffi (`pip install curl_cffi`) — without it "
                                  "requests fall back to plain urllib, which "
                                  "Cloudflare-protected APIs can silently kill on "
                                  "longer streaming responses. Otherwise retry, or "
                                  "check EVO_IDE_TIMEOUT / request_timeout.")
                diagnostics = diagnostics + [{
                    "severity": "warning",
                    "line": len(new_content.splitlines()),
                    "column": 0,
                    "message": f"Response was cut off before a closing code fence — {cause}. "
                               "This file may be incomplete.",
                    "source": "truncation",
                    "suggestion": suggestion,
                }]
                emit({"event": "status",
                      "text": f"{filename} looks truncated ({cause}) "
                              f"— offering the partial content for review"})
            if AGENT_VALIDATE_BEFORE_APPLY and not ok:
                emit({"event": "rejected", "file": filename, "diagnostics": diagnostics,
                      "text": f"{filename} rejected: syntax error, not offered"})
                continue
            target = SANDBOX_DIR / filename
            old_content = target.read_text(encoding="utf-8") if target.exists() else None
            candidates.append((filename, "write", old_content, new_content, diagnostics, None))

        for filename, line_no, fix in fix_blocks:
            target = SANDBOX_DIR / filename
            if not target.exists():
                emit({"event": "error", "text": f"Fix target missing: {filename}"})
                continue
            original = target.read_text(encoding="utf-8")
            patched = DiffEngine.fix_specific_line(original, line_no, fix)
            ok, diagnostics = self._validate(filename, patched)
            if AGENT_VALIDATE_BEFORE_APPLY and not ok:
                emit({"event": "rejected", "file": filename, "diagnostics": diagnostics,
                      "text": f"Fix for {filename}:{line_no} rejected: syntax error"})
                continue
            candidates.append((filename, f"fix@{line_no}", original, patched, diagnostics, line_no))

        return candidates

    def apply_response(self, response: str, emit: Emit, active_file: str = "main.py",
                       tx_id: Optional[str] = None,
                       wait_for_decision: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                       finish_reason: Optional[str] = None,
                       reasoning: Optional[str] = None
                       ) -> List[AppliedFile]:
        candidates = self._gather_candidates(response, active_file, emit, finish_reason)

        # V7.1 FIX: DeepSeek-R1 and similar models often emit File: blocks inside
        # <thinking> tags, which get routed to reasoning instead of content. If no
        # files were found in content AND we have reasoning text, scan reasoning too.
        # This must happen BEFORE we give up on finding candidates, especially when
        # the content was truncated (finish_reason='length') — the file header might
        # be in content but the closing fence in reasoning, or vice versa.
        if not candidates and reasoning:
            emit({"event": "status", "text": "No files in content — scanning reasoning stream"})
            candidates = self._gather_candidates(reasoning, active_file, emit, finish_reason)

        if not candidates:
            return []

        if not runtime_config.TRANSACTIONAL_APPLY:
            return self._apply_direct(candidates, emit)

        patched_files = [
            tx_engine.build_patched_file(path, mode, old, new, diagnostics)
            for path, mode, old, new, diagnostics, _ in candidates
        ]
        tx = tx_engine.create_transaction(patched_files)
        emit({"event": "diff_preview", "tx_id": tx.tx_id, **tx.as_dict()})

        if wait_for_decision is not None:
            decision = wait_for_decision(tx.tx_id)
            if decision is None:
                emit({"event": "diff_timeout", "tx_id": tx.tx_id})
                decision = {"action": "reject_all"}
        else:
            decision = {"action": "apply_all"}

        result = tx_engine.commit(tx, decision)
        return self._emit_commit_result(tx, result, candidates, emit)

    def _apply_direct(self, candidates: List[tuple], emit: Emit) -> List[AppliedFile]:
        """Legacy immediate-write path, kept for EVO_TRANSACTIONAL_APPLY=false."""
        applied: List[AppliedFile] = []
        for path, mode, old, new, diagnostics, extra in candidates:
            success, detail = write_file(path, new)
            if not success:
                emit({"event": "error", "text": f"Write failed for {path}: {detail}"})
                continue
            record = AppliedFile(path, len(new.splitlines()), mode, diagnostics)
            applied.append(record)
            if mode == "write":
                emit({"event": "file_written", "file": path, "lines": record.lines,
                      "diagnostics": diagnostics})
                append_memory(f"[WRITE] {path} ({record.lines} lines)")
            else:
                session_stats.fixes_applied += 1
                emit({"event": "file_patched", "file": path, "line": extra,
                      "diagnostics": diagnostics})
                append_memory(f"[FIX] {path}:{extra}")
        return applied

    def _emit_commit_result(self, tx, result: Dict[str, Any], candidates: List[tuple],
                            emit: Emit) -> List[AppliedFile]:
        applied: List[AppliedFile] = []
        by_path = {c[0]: c for c in candidates}
        if not result.get("ok"):
            emit({"event": "error", "text": f"Transaction {tx.tx_id} failed: "
                                            f"{result.get('error')} — workspace unchanged"})
            return applied

        for item in result.get("applied", []):
            path = item["path"]
            mode, extra = by_path[path][1], by_path[path][5]
            record = AppliedFile(path, item["lines"], mode, item["diagnostics"])
            applied.append(record)
            if mode == "write":
                emit({"event": "file_written", "file": path, "lines": record.lines,
                      "diagnostics": record.diagnostics, "tx_id": tx.tx_id})
                append_memory(f"[WRITE] {path} ({record.lines} lines) tx={tx.tx_id}")
            else:
                session_stats.fixes_applied += 1
                emit({"event": "file_patched", "file": path, "line": extra,
                      "diagnostics": record.diagnostics, "tx_id": tx.tx_id})
                append_memory(f"[FIX] {path}:{extra} tx={tx.tx_id}")

        for path in result.get("rejected", []):
            emit({"event": "file_rejected_by_user", "file": path, "tx_id": tx.tx_id})

        return applied

    # ── execution + healing ──────────────────────────────────

    def _execute(self, filename: str, emit: Emit) -> Dict[str, Any]:
        emit({"event": "status", "text": f"Executing {filename}"})
        result = run_code(filename)
        session_stats.executions += 1
        if result.get("stdout"):
            emit({"event": "stdout", "text": result["stdout"]})
            append_memory(f"[RUN OUTPUT] {filename}\n{result['stdout'][:4000]}")
        if result.get("stderr"):
            emit({"event": "stderr", "text": result["stderr"]})
            append_memory(f"[RUN ERROR] {filename}\n{result['stderr'][:4000]}")
        if not result.get("auto_run", True):
            emit({"event": "terminal_hint", "text": result.get("terminal_command", "")})
        emit({"event": "execution", "file": filename, "success": result.get("success", False),
              "duration": result.get("execution_time", 0)})
        return result

    def _heal_target(self, applied: List[AppliedFile], fallback: str) -> str:
        runnable = [f.path for f in applied if f.path.endswith(".py")]
        for candidate in ("main.py", "app.py"):
            if candidate in runnable:
                return candidate
        return runnable[0] if runnable else fallback

    # ── public API ───────────────────────────────────────────

    def run_turn(self,
                 user_prompt: str,
                 active_file: str = "main.py",
                 cursor_line: int = 0,
                 emit: Optional[Emit] = None,
                 thinking: bool = True,
                 auto_apply: bool = AGENT_AUTO_APPLY,
                 auto_run: Optional[bool] = None,
                 max_heal: int = AGENT_MAX_HEAL,
                 should_stop: Optional[Callable[[], bool]] = None,
                 wait_for_decision: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                 attachments: Optional[List[str]] = None
                 ) -> TurnResult:
        emit = emit or (lambda payload: logger.info(f"event: {payload.get('event')}"))
        result = TurnResult()
        run_enabled = runtime_config.AUTO_RUN_ENABLED if auto_run is None else auto_run

        try:
            self.client.resolve_model(lambda status: emit({"event": "status", "text": status}))
        except Exception as exc:
            result.error = str(exc)
            emit({"event": "error", "text": result.error})
            return result

        emit({"event": "status", "text": f"Model {self.client.model} · assembling context"})
        system_prompt, report = self.build_prompt(user_prompt, active_file, cursor_line,
                                                  thinking, emit, attachments=attachments)
        result.token_report = report
        emit({"event": "context", **report})

        content, reasoning, usage, stopped, error, finish_reason = self._stream(
            system_prompt, user_prompt, emit, thinking, should_stop)
        result.response, result.reasoning, result.usage = content, reasoning, usage
        result.stopped, result.error = stopped, error
        if error or stopped:
            return result

        self.history.append({"role": "user", "content": user_prompt})
        self.history.append({"role": "assistant", "content": content[:2000]})
        self._persist_history()

        if not auto_apply:
            result.ok = True
            return result

        applied = self.apply_response(content, emit, active_file=active_file,
                                      wait_for_decision=wait_for_decision,
                                      finish_reason=finish_reason,
                                      reasoning=reasoning)
        # V7.1: The reasoning fallback is now handled inside apply_response above,
        # so we don't need a second apply_response call here. This prevents double-
        # application when both content and reasoning contain (different) file blocks.
        result.files = applied
        if not applied:
            result.ok = True
            emit({"event": "status", "text": "No file artifacts in response"})
            return result

        vector_index.rebuild()

        if not run_enabled:
            result.ok = True
            emit({"event": "status", "text": "Auto-run disabled; workspace updated"})
            return result

        target = self._heal_target(applied, active_file)
        heals = 0
        while True:
            run_result = self._execute(target, emit)
            result.executions.append({
                "file": target,
                "success": run_result.get("success", False),
                "stdout": run_result.get("stdout", ""),
                "stderr": run_result.get("stderr", ""),
            })
            if run_result.get("success") or not run_result.get("auto_run", True):
                result.ok = True
                break

            stderr = run_result.get("stderr", "")
            module = detect_missing_module(stderr)
            if module and AGENT_AUTO_INSTALL:
                emit({"event": "status", "text": f"Installing missing module: {module}"})
                installed, output = install_module(module)
                emit({"event": "stdout" if installed else "stderr", "text": output[-2000:]})
                if installed:
                    continue

            if heals >= max_heal:
                result.error = f"Unresolved after {heals} heal attempt(s)"
                emit({"event": "error", "text": result.error})
                break

            heals += 1
            result.heals = heals
            emit({"event": "status", "text": f"Self-heal {heals}/{max_heal} on {target}"})
            heal_prompt = SELF_HEAL_PROMPT.format(
                error_msg=stderr[-3000:], run_file=target, original_file=active_file)
            heal_system, heal_report = self.build_prompt(
                heal_prompt, target, cursor_line, thinking, emit,
                extra="Self-heal mode: emit ONE Fix: block for the failing file only.")
            heal_content, heal_reasoning, heal_usage, heal_stopped, heal_error, heal_reason = self._stream(
                heal_system, heal_prompt, emit, thinking, should_stop)
            result.usage = heal_usage or result.usage
            result.token_report = heal_report
            if heal_error or heal_stopped:
                result.error = heal_error
                result.stopped = heal_stopped
                break
            healed = self.apply_response(heal_content, emit, active_file=target,
                                         wait_for_decision=wait_for_decision,
                                         finish_reason=heal_reason)
            if not healed:
                result.error = "Self-heal produced no applicable patch"
                emit({"event": "error", "text": result.error})
                break
            result.files.extend(healed)
            target = self._heal_target(healed, target)
            time.sleep(AGENT_HEAL_BACKOFF)

        emit({"event": "complete", **result.as_dict()})
        return result

    def run_sprint(self,
                   goal: str,
                   active_file: str = "main.py",
                   emit: Optional[Emit] = None,
                   max_iterations: int = SPRINT_MAX_ITERATIONS,
                   max_seconds: int = SPRINT_MAX_SECONDS,
                   should_stop: Optional[Callable[[], bool]] = None,
                   attachments: Optional[List[str]] = None) -> List[TurnResult]:
        """Autonomous multi-turn loop: keep iterating until the goal reports
        DONE, limits are hit, or a turn fails irrecoverably."""
        emit = emit or (lambda payload: logger.info(f"event: {payload.get('event')}"))
        if not runtime_config.SPRINT_ENABLED:
            emit({"event": "sprint_halted",
                  "reason": "Sprint is disabled (cloud mode). Set EVO_SPRINT_ENABLED=true to opt in."})
            return []
        started = time.time()
        results: List[TurnResult] = []
        prompt = (f"{goal}\n\nWork autonomously. When the goal is fully met and the code "
                  f"runs clean, reply with the single line: SPRINT_COMPLETE")

        for iteration in range(1, max_iterations + 1):
            if should_stop and should_stop():
                break
            if time.time() - started > max_seconds:
                emit({"event": "status", "text": "Sprint time budget exhausted"})
                break
            emit({"event": "sprint_iteration", "iteration": iteration})
            turn = self.run_turn(prompt, active_file=active_file, emit=emit,
                                 should_stop=should_stop, attachments=attachments)
            results.append(turn)
            if "SPRINT_COMPLETE" in turn.response:
                emit({"event": "sprint_complete", "iterations": iteration})
                break
            if turn.stopped or (turn.error and not turn.files):
                emit({"event": "sprint_halted", "reason": turn.error or "stopped"})
                break
            failures = [e for e in turn.executions if not e["success"]]
            prompt = (f"Continue toward the goal: {goal}\n"
                      f"Last run {'failed' if failures else 'passed'}. "
                      f"{'Fix: ' + failures[-1]['stderr'][-1500:] if failures else 'Advance the next unfinished piece.'}")
        return results

    def stream_turn(self, *args, **kwargs) -> Iterator[Dict[str, Any]]:
        """Buffered generator wrapper for callers that prefer iteration over a
        callback. Events are replayed after the turn finishes — pass your own
        `emit` to run_turn when you need true live streaming (e.g. HTTP)."""
        queue: List[Dict[str, Any]] = []
        kwargs["emit"] = queue.append
        result = self.run_turn(*args, **kwargs)
        for payload in queue:
            yield payload
        yield {"event": "result", **result.as_dict()}


# Global orchestrator instance
orchestrator = AgentOrchestrator()
