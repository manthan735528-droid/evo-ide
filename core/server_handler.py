#!/usr/bin/env python3
"""
Evo IDE V6.1 - HTTP Server & Request Handler
============================================
Thin transport in front of AgentOrchestrator. All model work, artifact
parsing, validation and healing lives in core.agent_orchestrator; this file
only moves bytes and exposes workspace endpoints.

V6.1 fixes (the ones that were actually breaking runs):
- FIXED: `'StreamEvent' object has no attribute 'get'`. The old handler
         re-implemented the chat loop and treated StreamEvent dataclasses as
         dicts. The loop is gone — the orchestrator owns it and pushes flat
         dict events through core.stream_handler.StreamSession.
- FIXED: `Transfer-Encoding: chunked` was advertised while raw bytes were
         written, so browsers truncated the tail of every long response.
         ChunkedWriter now emits correct chunk framing and a zero-chunk
         terminator under HTTP/1.1.
- FIXED: responses had no Content-Length under keep-alive, causing hung
         requests in the file/tree endpoints.
- FIXED: duplicated prompt assembly (god-view + structural context) that
         blew past num_ctx. ContextManager assembles the prompt instead.
- ADDED: /api/health, /api/models, /api/sprint_stream, /api/validate,
         /api/unload, and static asset serving for styles.css / app.js.
"""
import json
import mimetypes
import os
import tempfile
import threading
import time
import http.server
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import core.config as config
from .agent_orchestrator import orchestrator
from .ollama_client import send_proxy_request
from .backend_utils import (
    append_memory, delete_file, extract_multipart_file, get_file_stats,
    import_project, run_code, run_terminal_command, write_file,
    get_web_sources, add_web_source,
)
from .config import (
    DEFAULT_MODEL, DEFAULT_OLLAMA_URL, FEATURE_WEB_FETCH, MODEL_MAX_OUTPUT,
    MODEL_NUM_CTX, SANDBOX_DIR, STATIC_DIR,
    SYSTEM_PYTHON, logger,
)
from .diff_engine import DiffEngine
from .indexing import vector_index
from .lsp import LSPValidator
from .ollama_client import ollama_client
from . import patch_transaction as tx_engine
from .state import active_streams, session_stats
from .stream_handler import ChunkedWriter, StreamSession
from .streaming import (
    check_stop, cleanup_stream, get_stream_id, get_stream_info, set_approval,
    set_transaction_decision, stop_stream, wait_for_transaction_decision,
)
from .templates import TEMPLATES
from .web_fetch import clear_web_cache, fetch_url

HIDDEN_DIRS = {".venv", "__pycache__", ".index", ".logs", ".plugins", ".transactions", ".git"}

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)


def session_snapshot() -> dict:
    """One canonical stats payload, shared by every endpoint."""
    return {
        "tokens": session_stats.input_tokens + session_stats.output_tokens,
        "input_tokens": session_stats.input_tokens,
        "output_tokens": session_stats.output_tokens,
        "money": round(session_stats.money_saved, 4),
        "files": get_file_stats(),
        "runs": session_stats.executions,
        "fixes": session_stats.fixes_applied,
        "context_chunks": len(vector_index.chunks),
        "web_requests": session_stats.web_requests,
        "web_cache_hits": session_stats.web_cache_hits,
        "thinking_enabled": session_stats.thinking_enabled,
        "run_mode": config.RUN_MODE,
        "auto_run": config.AUTO_RUN_ENABLED,
        "model": ollama_client.model,
        "num_ctx": MODEL_NUM_CTX,
        "max_output": MODEL_MAX_OUTPUT,
        
        "system_python": str(SYSTEM_PYTHON),
        "active_streams": len(active_streams),
        "cloud_mode": config.CLOUD_MODE,
        "sprint_enabled": config.SPRINT_ENABLED,
        "transactional_apply": config.TRANSACTIONAL_APPLY,
    }


def list_workspace_files() -> list:
    files = []
    for root, dirs, filenames in os.walk(SANDBOX_DIR):
        dirs[:] = [d for d in dirs if d not in HIDDEN_DIRS]
        for name in filenames:
            if name.startswith("."):
                continue
            rel = Path(root).relative_to(SANDBOX_DIR) / name
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append({"path": str(rel).replace("\\", "/"), "size": size})
    return sorted(files, key=lambda f: f["path"])


def safe_workspace_path(name: str):
    """Resolve a workspace-relative path, refusing traversal."""
    if not name:
        return None
    candidate = (SANDBOX_DIR / name).resolve()
    try:
        candidate.relative_to(SANDBOX_DIR.resolve())
    except ValueError:
        return None
    return candidate


class IDEHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 is required for legitimate chunked transfer encoding.
    protocol_version = "HTTP/1.1"
    server_version = "EvoIDE/6.1"

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    # ── response helpers ─────────────────────────────────────

    def _common(self):
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        self.send_header("Access-Control-Allow-Origin", "*")

    def send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._common()
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self._common()
        self.end_headers()

    # ── GET ──────────────────────────────────────────────────

    def do_GET(self):
        route = urlparse(self.path)
        path, query = route.path, parse_qs(route.query)
        if path == "/":
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if path == "/ai_model_manager.html":
            return self._serve_static("ai_model_manager.html", "text/html; charset=utf-8")
        if path in ("/styles.css", "/app.js", "/favicon.ico"):
            return self._serve_static(path.lstrip("/"))
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])

        if path == "/api/health":
            health = ollama_client.health()
            return self.send_json({
                "ok": bool(health.get("online") and health.get("model_available")),
                "ollama": health,
                "host": getattr(ollama_client, "host", DEFAULT_OLLAMA_URL),
                "configured_model": ollama_client.model,
                "session": session_snapshot(),
            })

        if path == "/api/models":
            try:
                models = ollama_client.list_models()
                return self.send_json({"models": models,
                                       "active": ollama_client.model,
                                       "host": ollama_client.host,
                                       "mode": "openai-compatible" if ollama_client.is_openai_compatible else "ollama"})
            except Exception as exc:
                return self.send_json({"models": [], "active": ollama_client.model,
                                       "host": ollama_client.host,
                                       "error": str(exc)}, 502)

        if path == "/api/files":
            return self.send_json({"files": list_workspace_files()})

        if path == "/api/file":
            name = unquote(query.get("name", [""])[0])
            target = safe_workspace_path(name)
            if target is None or not target.is_file():
                return self.send_json({"error": f"Not found: {name}"}, 404)
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return self.send_json({"error": str(exc)}, 415)
            payload = {"content": content, "path": name, "diagnostics": [], "metrics": {}}
            if name.endswith(".py"):
                payload["diagnostics"] = LSPValidator.validate_python(content, name)
                payload["metrics"] = LSPValidator.get_complexity_metrics(content)
            return self.send_json(payload)

        if path == "/api/context":
            filename = query.get("file", [""])[0]
            line = int(query.get("line", ["0"])[0] or 0)
            chunks = vector_index.search(query.get("q", [""])[0], top_k=8,
                                         current_file=filename, cursor_line=line)
            trimmed = [{k: v for k, v in c.items() if k != "tokens"} for c in chunks]
            return self.send_json({"chunks": trimmed})

        if path == "/api/templates":
            return self.send_json({"templates": {k: {"description": v["description"],
                                                     "files": list(v["files"].keys())}
                                                 for k, v in TEMPLATES.items()}})

        if path == "/api/structure":
            try:
                return self.send_json(vector_index.get_project_structure())
            except Exception as exc:
                logger.error(f"Structure endpoint error: {exc}")
                return self.send_json({"error": str(exc), "files": [],
                                       "dependencies": [], "stats": {}}, 500)

        if path == "/api/dependencies":
            try:
                return self.send_json(vector_index.get_dependency_graph())
            except Exception as exc:
                return self.send_json({"error": str(exc), "nodes": [], "edges": []}, 500)

        if path == "/api/metrics":
            try:
                structure = vector_index.get_project_structure()
                stats = structure.get("stats", {})
            except Exception:
                stats = {}
            return self.send_json({"stats": stats, "session": session_snapshot()})

        if path == "/api/web_sources":
            return self.send_json({"sources": get_web_sources()})

        if path == "/api/stream_info":
            sid = query.get("id", [""])[0]
            return self.send_json(get_stream_info(sid) or {"error": "unknown stream"})

        if path == "/api/transactions":
            return self.send_json({"transactions": tx_engine.list_transactions()})

        if path == "/api/fetch":
            if not FEATURE_WEB_FETCH:
                return self.send_json({"error": "Web fetch disabled"}, 403)
            url = query.get("url", [""])[0]
            if not url:
                return self.send_json({"error": "No URL provided"}, 400)
            try:
                result = fetch_url(url)
                session_stats.web_requests += 1
                if result.get("cached"):
                    session_stats.web_cache_hits += 1
                if result.get("success"):
                    add_web_source(url, title=url[:60], status="fetched")
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 500)

        if path == "/api/proxy":
            target_url = body.get("url", "").strip()
            method = body.get("method", "GET").upper()
            proxy_headers = body.get("headers", {})
            proxy_body = body.get("body")
            timeout = body.get("timeout", 15)

            if not target_url:
                return self.send_json({"error": "Missing url"}, 400)

            try:
                with send_proxy_request(
                    target_url, method=method,
                    headers={k: str(v) for k, v in proxy_headers.items()},
                    data=json.dumps(proxy_body).encode("utf-8") if proxy_body is not None else None,
                    timeout=timeout,
                ) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                    try:
                        resp_json = json.loads(resp_body)
                        return self.send_json({"ok": True, "status": resp.status, "data": resp_json})
                    except json.JSONDecodeError:
                        return self.send_json({"ok": True, "status": resp.status, "text": resp_body})
            except urllib.error.HTTPError as e:
                body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
                return self.send_json({"ok": False, "status": e.code, "error": body_text or e.reason}, e.code)
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)}, 502)

        return self.send_json({"error": "Not found", "path": path}, 404)

    def _serve_static(self, relative: str, content_type: str = ""):
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self.send_json({"error": "Forbidden"}, 403)
        if not target.is_file():
            return self.send_json({"error": f"Missing asset: {relative}"}, 404)
        guessed = content_type or (mimetypes.guess_type(target.name)[0]
                                   or "application/octet-stream")
        if guessed.startswith("text/") or guessed in ("application/javascript",):
            guessed += "; charset=utf-8"
        return self.send_bytes(target.read_bytes(), guessed)

    # ── POST ─────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/import":
            return self._handle_import()
        if path == "/api/chat_stream":
            return self._handle_stream(sprint=False)
        if path == "/api/sprint_stream":
            return self._handle_stream(sprint=True)

        body = self.read_json()

        if path == "/api/save":
            filename = body.get("filename", "main.py")
            ok, detail = write_file(filename, body.get("code", ""))
            return self.send_json({"success": ok, "detail": str(detail)})

        if path == "/api/newfile":
            filename = body.get("filename", "untitled.py")
            target = safe_workspace_path(filename)
            if target is None:
                return self.send_json({"success": False, "error": "Invalid path"}, 400)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"# {filename}\n", encoding="utf-8")
                session_stats.files_created += 1
                vector_index.index_file(target, filename)
            return self.send_json({"success": True, "path": filename})

        if path == "/api/delete":
            ok, message = delete_file(body.get("filename", ""))
            return self.send_json({"success": ok, "message": message})

        if path == "/api/run":
            filename = body.get("filename", "main.py")
            result = run_code(filename)
            session_stats.executions += 1
            result["stats"] = session_snapshot()
            return self.send_json(result)

        if path == "/api/terminal":
            command = body.get("command", "")
            result = run_terminal_command(command)
            if result.get("stdout"):
                append_memory(f"[TERMINAL OUTPUT] $ {command}\n{result['stdout'][:4000]}")
            if result.get("stderr"):
                append_memory(f"[TERMINAL ERROR] $ {command}\n{result['stderr'][:4000]}")
            return self.send_json(result)

        if path == "/api/validate":
            code = body.get("code", "")
            name = body.get("filename", "main.py")
            if not name.endswith(".py"):
                return self.send_json({"diagnostics": [], "metrics": {}})
            return self.send_json({
                "diagnostics": LSPValidator.validate_python(code, name),
                "metrics": LSPValidator.get_complexity_metrics(code),
            })

        if path == "/api/fix_preview":
            name = body.get("filename", "")
            target = safe_workspace_path(name)
            if target is None or not target.is_file():
                return self.send_json({"error": "Not found"}, 404)
            preview = DiffEngine.preview_fix(target.read_text(encoding="utf-8"),
                                             int(body.get("line", 1)),
                                             body.get("code", ""))
            return self.send_json(preview)

        if path == "/api/template":
            key = body.get("template", "")
            if key not in TEMPLATES:
                return self.send_json({"success": False, "error": "Template not found"}, 404)
            for rel, content in TEMPLATES[key]["files"].items():
                write_file(rel, content)
            return self.send_json({"success": True,
                                   "main_file": next(iter(TEMPLATES[key]["files"]))})

        if path == "/api/rebuild_index":
            vector_index.rebuild()
            return self.send_json({"success": True, "chunks": len(vector_index.chunks)})

        if path == "/api/clear_web_cache":
            return self.send_json({"cleared": clear_web_cache()})

        if path == "/api/unload":
            return self.send_json({"unloaded": ollama_client.unload()})

        if path == "/api/stop":
            sid = body.get("stream_id")
            targets = [sid] if sid else list(active_streams.keys())
            for target in targets:
                stop_stream(target)
            return self.send_json({"status": "stopped", "streams": targets})

        if path == "/api/approval":
            set_approval(body.get("approval_id"), bool(body.get("approved")))
            return self.send_json({"status": "received"})

        if path == "/api/tx_decision":
            stream_id = body.get("stream_id", "")
            tx_id = body.get("tx_id", "")
            decision = body.get("decision") or {"action": "reject_all"}
            if not stream_id or not tx_id:
                return self.send_json({"error": "stream_id and tx_id required"}, 400)
            set_transaction_decision(stream_id, tx_id, decision)
            return self.send_json({"status": "received"})

        if path == "/api/tx_revert":
            tx_id = body.get("tx_id", "")
            if not tx_id:
                return self.send_json({"error": "tx_id required"}, 400)
            result = tx_engine.revert(tx_id)
            if result.get("ok"):
                vector_index.rebuild()
            return self.send_json(result, 200 if result.get("ok") else 409)

        if path == "/api/thinking":
            enabled = body.get("enabled")
            if enabled is not None:
                session_stats.thinking_enabled = bool(enabled)
            return self.send_json({"thinking_enabled": session_stats.thinking_enabled})

        if path == "/api/run_mode":
            mode = body.get("mode", "system")
            if mode in ("system", "sandbox"):
                config.RUN_MODE = mode
                os.environ["EVO_RUN_MODE"] = mode
            return self.send_json({"run_mode": config.RUN_MODE,
                                   "system_python": str(SYSTEM_PYTHON)})

        if path == "/api/auto_run":
            enabled = body.get("enabled")
            if enabled is not None:
                config.AUTO_RUN_ENABLED = bool(enabled)
                os.environ["EVO_AUTO_RUN"] = "true" if config.AUTO_RUN_ENABLED else "false"
            return self.send_json({"auto_run": config.AUTO_RUN_ENABLED})
        if path == "/api/model":
            name = body.get("model", "").strip()
            host = body.get("host", "").strip()
            api_key = body.get("api_key", "").strip()
            auth_type = body.get("auth_type", "").strip()
            
            if name:
                ollama_client.model = name
            if host:
                new_host = host.rstrip("/")
                if new_host != getattr(ollama_client, "host", ""):
                    ollama_client.host = new_host
                    ollama_client._detected_format = None
            if "api_key" in body:
                ollama_client.api_key = api_key
            if "auth_type" in body:
                ollama_client.auth_type = auth_type

            return self.send_json({
                "model": ollama_client.model,
                "host": getattr(ollama_client, "host", ""),
                "auth_type": getattr(ollama_client, "auth_type", "None"),
                "api_format": ollama_client._detect_format()
            })

        if path == "/api/proxy":
            target_url = body.get("url", "").strip()
            method = body.get("method", "GET").upper()
            proxy_headers = body.get("headers", {})
            proxy_body = body.get("body")
            timeout = body.get("timeout", 15)

            if not target_url:
                return self.send_json({"error": "Missing url"}, 400)

            try:
                with send_proxy_request(
                    target_url, method=method,
                    headers={k: str(v) for k, v in proxy_headers.items()},
                    data=json.dumps(proxy_body).encode("utf-8") if proxy_body is not None else None,
                    timeout=timeout,
                ) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                    try:
                        resp_json = json.loads(resp_body)
                        return self.send_json({"ok": True, "status": resp.status, "data": resp_json})
                    except json.JSONDecodeError:
                        return self.send_json({"ok": True, "status": resp.status, "text": resp_body})
            except urllib.error.HTTPError as e:
                body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
                return self.send_json({"ok": False, "status": e.code, "error": body_text or e.reason}, e.code)
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)}, 502)

        return self.send_json({"error": "Not found", "path": path}, 404)

    def _handle_import(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        content_type = self.headers.get("Content-Type", "")
        raw = self.rfile.read(length) if length > 0 else b""
        if "multipart/form-data" not in content_type:
            return self.send_json({"success": False, "error": "Invalid content type"}, 415)
        payload = extract_multipart_file(content_type, raw, field_name="file")
        if payload is None:
            return self.send_json({"success": False, "error": "No file uploaded"}, 400)
        temp_path = tempfile.mktemp(suffix=".zip")
        try:
            Path(temp_path).write_bytes(payload)
            ok, count = import_project(temp_path)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if ok:
            vector_index.rebuild()
        return self.send_json({"success": ok, "files_imported": count})

    # ── streaming turn ───────────────────────────────────────

    def _handle_stream(self, sprint: bool):
        body = self.read_json()
        prompt = (body.get("prompt") or "").strip()
        active_file = body.get("currentFile") or "main.py"
        cursor_line = int(body.get("cursorLine") or 0)
        thinking = bool(body.get("thinking", session_stats.thinking_enabled))
        auto_apply = bool(body.get("autoApply", True))
        auto_run = body.get("autoRun")
        if auto_run is not None:
            auto_run = bool(auto_run)
        attachments = body.get("attachments") or []
        if not isinstance(attachments, list):
            attachments = []
        attachments = [str(a) for a in attachments if a][:8]

        if not prompt:
            return self.send_json({"error": "Empty prompt"}, 400)

        stream_id = get_stream_id()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, no-transform")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Stream-Id", stream_id)
        self._common()
        self.end_headers()

        writer = ChunkedWriter(self.wfile, chunked=True)
        session = StreamSession(writer, max_output=MODEL_MAX_OUTPUT)
        stop_flag = threading.Event()

        def keepalive():
            # A cold 14B model can take 30s+ to load; without traffic some
            # proxies and the browser's own idle timers give up.
            while not stop_flag.wait(5.0):
                if writer.broken:
                    return
                writer.heartbeat(idle_seconds=8.0)

        heart = threading.Thread(target=keepalive, daemon=True)
        heart.start()

        session.emit({"event": "stream_start", "stream_id": stream_id,
                      "model": ollama_client.model, "thinking": thinking,
                      "sprint": sprint, "session": session_snapshot()})

        def wait_for_decision(tx_id: str):
            return wait_for_transaction_decision(
                stream_id, tx_id, timeout=config.TRANSACTION_APPROVAL_TIMEOUT)

        started = time.monotonic()
        try:
            if sprint:
                if not config.SPRINT_ENABLED:
                    session.emit({"event": "sprint_halted",
                                 "reason": "Sprint is disabled in this deployment (cloud mode). "
                                          "Set EVO_SPRINT_ENABLED=true to opt in."})
                    summary = {"iterations": 0, "ok": False}
                else:
                    results = orchestrator.run_sprint(
                        prompt, active_file=active_file, emit=session.emit,
                        should_stop=lambda: check_stop(stream_id) or writer.broken,
                        attachments=attachments)
                    summary = {"iterations": len(results),
                               "ok": all(r.ok for r in results) if results else False}
            else:
                turn = orchestrator.run_turn(
                    prompt, active_file=active_file, cursor_line=cursor_line,
                    emit=session.emit, thinking=thinking, auto_apply=auto_apply,
                    auto_run=auto_run,
                    should_stop=lambda: check_stop(stream_id) or writer.broken,
                    # Auto-apply must not wait for the interactive diff-review
                    # endpoint. Manual review remains enabled when autoApply is off.
                    wait_for_decision=(wait_for_decision
                                       if (config.TRANSACTIONAL_APPLY and not auto_apply)
                                       else None))
                summary = {"ok": turn.ok, "heals": turn.heals,
                           "stopped": turn.stopped, "error": turn.error}
        except Exception as exc:  # never leave the socket half-written
            logger.exception("Stream turn failed")
            session.emit({"event": "error", "text": f"{type(exc).__name__}: {exc}"})
            summary = {"ok": False, "error": str(exc)}
        finally:
            stop_flag.set()
            summary["duration_ms"] = int((time.monotonic() - started) * 1000)
            summary["session"] = session_snapshot()
            try:
                session.finalize(summary)
            finally:
                writer.close()
                cleanup_stream(stream_id)
