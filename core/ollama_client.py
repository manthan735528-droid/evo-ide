#!/usr/bin/env python3
"""
Evo IDE V6.2 - Universal LLM Transport Client
==============================================
Supports: Ollama native API, OpenAI-compatible APIs (OpenRouter, OpenAI, LM Studio, etc.)

Auto-detects API format. Handles auth for all providers.
"""
import io
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from core.config import (
    CLOUD_MAX_OUTPUT_FALLBACK, DEFAULT_MODEL, DEFAULT_OLLAMA_URL,
    FALLBACK_MODELS, LLM_AUTO_PULL,
    LLM_CONNECT_TIMEOUT, LLM_MAX_RETRIES, LLM_REQUEST_TIMEOUT,
    LLM_RETRY_BACKOFF, LOCAL_MAX_OUTPUT_FALLBACK, MODEL_KEEP_ALIVE,
    MODEL_MAX_OUTPUT, MODEL_NUM_CTX, MODEL_OPTIONS, REASONING_CONFIG, logger,
)
from core.stream_handler import NdjsonReader

# CLOUDFLARE FIX: Cloud hosts (Cerebras, etc.) sit behind Cloudflare's bot
# WAF, which fingerprints the TLS/JA3 handshake — not just the User-Agent
# header. Plain urllib gets banned ("Error 1010: browser_signature_banned")
# no matter what headers we send. curl_cffi impersonates a real Chrome TLS
# fingerprint and gets through. It's optional (stdlib-only stays the
# default for local Ollama); install with `pip install curl_cffi`.
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}


class OllamaError(RuntimeError):
    """Transport or protocol failure talking to the LLM host."""


class _CurlResponseAdapter:
    """Wraps a curl_cffi streaming response so it quacks like the
    http.client.HTTPResponse object urllib.request.urlopen() returns:
    .status, .read(size=-1), and context-manager support. This lets every
    existing call site (which was written against urllib) work unchanged
    regardless of which transport actually sent the request."""

    def __init__(self, resp):
        self._resp = resp
        self.status = resp.status_code
        self.headers = resp.headers
        self._iter = resp.iter_content(chunk_size=1024)
        self._buf = b""
        self._exhausted = False

    def _fill(self, at_least: int) -> None:
        while not self._exhausted and len(self._buf) < at_least:
            try:
                chunk = next(self._iter)
            except StopIteration:
                self._exhausted = True
                break
            if chunk:
                self._buf += chunk

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._exhausted:
                self._fill(1 << 20)
            out, self._buf = self._buf, b""
            return out
        self._fill(size)
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


@dataclass
class StreamEvent:
    """One unit of model output."""
    kind: str
    text: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return self.kind

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "type": self.kind,
                "text": self.text, "meta": dict(self.meta)}

    def keys(self):
        return self.as_dict().keys()

    def items(self):
        return self.as_dict().items()

    def values(self):
        return self.as_dict().values()

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("kind", "type"):
            return self.kind
        if key == "text":
            return self.text
        if key == "meta":
            return self.meta
        return self.meta.get(key, default)

    def __getitem__(self, key: str) -> Any:
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            raise KeyError(key)
        return value

    def __contains__(self, key: str) -> bool:
        sentinel = object()
        return self.get(key, sentinel) is not sentinel


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_duration_ms: int = 0
    load_duration_ms: int = 0
    eval_rate: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.prompt_tokens + self.output_tokens,
            "total_duration_ms": self.total_duration_ms,
            "load_duration_ms": self.load_duration_ms,
            "eval_rate": round(self.eval_rate, 2),
        }


class ReasoningSplitter:
    """Incrementally split a token stream into reasoning vs. answer text."""

    def __init__(self, open_tag: str = "<think>", close_tag: str = "</think>"):
        self.open_tag = open_tag
        self.close_tag = close_tag
        self.buf = ""
        self.inside = False

    @staticmethod
    def _partial_tail(buf: str, tag: str) -> int:
        for k in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:k]):
                return k
        return 0

    def feed(self, chunk: str) -> List[Tuple[str, str]]:
        if not chunk:
            return []
        self.buf += chunk
        out: List[Tuple[str, str]] = []
        while True:
            target = self.close_tag if self.inside else self.open_tag
            idx = self.buf.find(target)
            if idx >= 0:
                head = self.buf[:idx]
                if head:
                    out.append(("reasoning" if self.inside else "content", head))
                self.buf = self.buf[idx + len(target):]
                self.inside = not self.inside
                continue
            hold = self._partial_tail(self.buf, target)
            emit_len = len(self.buf) - hold
            if emit_len > 0:
                out.append(("reasoning" if self.inside else "content", self.buf[:emit_len]))
                self.buf = self.buf[emit_len:]
            break
        return out

    def flush(self) -> List[Tuple[str, str]]:
        if not self.buf:
            return []
        out = [("reasoning" if self.inside else "content", self.buf)]
        self.buf = ""
        return out


def strip_reasoning(text: str,
                    open_tag: str = "<think>",
                    close_tag: str = "</think>") -> str:
    """Remove all reasoning blocks from a finished response body."""
    out, cursor = [], 0
    while True:
        start = text.find(open_tag, cursor)
        if start < 0:
            out.append(text[cursor:])
            break
        out.append(text[cursor:start])
        end = text.find(close_tag, start)
        if end < 0:
            break
        cursor = end + len(close_tag)
    return "".join(out).strip()


def _is_local_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        host = ""
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def send_proxy_request(url: str, method: str = "GET",
                        headers: Optional[Dict[str, str]] = None,
                        data: Optional[bytes] = None,
                        timeout: int = 15):
    """Shared HTTP transport for anything that proxies an external LLM
    host's REST API on the server's behalf — e.g. the Settings UI's
    "Test Connection" / model-list fetch, which the browser can't call
    directly (CORS) and routes through /api/proxy instead.

    Cloud hosts go through curl_cffi's Chrome TLS impersonation for the
    same reason OllamaClient's transport does: Cloudflare-protected APIs
    (Cerebras and others) fingerprint the raw urllib TLS handshake and
    block it as bot traffic regardless of what headers are sent.

    Returns an object with .status / .read() (same shape urllib.request
    .urlopen() returns) and raises urllib.error.HTTPError / URLError on
    failure, so callers written against urllib don't need to change.
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", JSON_HEADERS["User-Agent"])

    if not _is_local_url(url) and CURL_CFFI_AVAILABLE:
        try:
            session = curl_requests.Session(impersonate="chrome")
            resp = session.request(method, url, data=data, headers=headers,
                                   timeout=timeout, stream=True)
        except Exception as exc:
            raise urllib.error.URLError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.content
            try:
                resp.close()
            except Exception:
                pass
            raise urllib.error.HTTPError(
                url, resp.status_code, resp.reason or "",
                dict(resp.headers), io.BytesIO(body),
            )
        return _CurlResponseAdapter(resp)

    if not _is_local_url(url) and not CURL_CFFI_AVAILABLE:
        logger.warning(
            f"Proxying to cloud host {url} without curl_cffi installed — "
            f"Cloudflare-protected APIs may block this as bot traffic. "
            f"Run: pip install curl_cffi"
        )

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


class OllamaClient:
    """Universal LLM client supporting Ollama native and OpenAI-compatible APIs."""

    def __init__(self,
                 host: str = DEFAULT_OLLAMA_URL,
                 model: str = DEFAULT_MODEL,
                 options: Optional[Dict[str, Any]] = None,
                 keep_alive: str = MODEL_KEEP_ALIVE,
                 request_timeout: int = LLM_REQUEST_TIMEOUT,
                 connect_timeout: int = LLM_CONNECT_TIMEOUT,
                 max_retries: int = LLM_MAX_RETRIES,
                 retry_backoff: float = LLM_RETRY_BACKOFF):
        self._host = host.rstrip("/")
        self.model = model
        self.api_key = ""
        self.auth_type = "None"
        self.keep_alive = keep_alive
        self.request_timeout = request_timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max(1, max_retries)
        self.retry_backoff = retry_backoff
        self.api_format = "auto"  # "ollama" | "openai" | "auto"
        self._detected_format = None
        self._curl_session = None
        self._warned_no_curl = False

        base_options = {
            "num_ctx": MODEL_NUM_CTX,
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "repeat_penalty": 1.05,
        }
        # V7.1 FIX: Always set num_predict explicitly. When MODEL_MAX_OUTPUT > 0 we
        # use the user's cap. When <= 0 (unlimited), we use LOCAL_MAX_OUTPUT_FALLBACK
        # (512K by default) instead of omitting num_predict — Ollama's default -1
        # triggers an internal 10*num_ctx cap that causes finish_reason='length' on
        # large files. 512K is effectively unlimited while avoiding that trap.
        if MODEL_MAX_OUTPUT > 0:
            base_options["num_predict"] = MODEL_MAX_OUTPUT
        else:
            base_options["num_predict"] = LOCAL_MAX_OUTPUT_FALLBACK
        base_options.update(MODEL_OPTIONS or {})
        base_options.update(options or {})
        if not base_options.get("stop"):
            base_options.pop("stop", None)
        if base_options.get("seed", 0) in (0, None):
            base_options.pop("seed", None)
        self.options = base_options

        rc = REASONING_CONFIG or {}
        self.reasoning_enabled = bool(rc.get("enabled", True))
        self.native_think = bool(rc.get("native_think_field", True))
        self.open_tag = rc.get("tag_open", "<think>")
        self.close_tag = rc.get("tag_close", "</think>")
        self.max_reasoning_chars = int(rc.get("max_reasoning_chars", 12000))
        self.last_usage = Usage()

    @property
    def host(self) -> str:
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        value = (value or "").rstrip("/")
        if value != getattr(self, "_host", None):
            self._detected_format = None
            self._curl_session = None
        self._host = value

    # ── API Format Detection ──────────────────────────────────

    def _detect_format(self) -> str:
        """Auto-detect if the endpoint is Ollama or OpenAI-compatible."""
        if self._detected_format:
            return self._detected_format

        # CEREBRAS FIX: Check for Cerebras first - it only supports /v1/chat/completions
        # and does NOT have /v1/models or /api/tags
        if self._is_cerebras():
            self._detected_format = "openai"
            logger.info(f"Detected Cerebras API at {self.host}")
            return "openai"

        # Try Ollama first
        try:
            self._request("/api/tags", method="GET", timeout=self.connect_timeout)
            self._detected_format = "ollama"
            logger.info(f"Detected Ollama API at {self.host}")
            return "ollama"
        except Exception:
            pass

        # Try OpenAI-compatible
        try:
            headers = self._build_headers()
            req = urllib.request.Request(
                f"{self.host}/v1/models", 
                headers=headers, 
                method="GET"
            )
            with self._urlopen(req, timeout=self.connect_timeout) as resp:
                if resp.status == 200:
                    self._detected_format = "openai"
                    logger.info(f"Detected OpenAI-compatible API at {self.host}")
                    return "openai"
        except Exception:
            pass

        # Default to OpenAI if host contains typical OpenAI indicators
        if any(x in self.host.lower() for x in ['openai', 'openrouter', 'api.groq', 'api.together', 'api.deepseek', 'api.mistral', 'api.z.ai', 'zhipu', 'fireworks.ai', 'cerebras']):
            self._detected_format = "openai"
            return "openai"

        # Default to Ollama for localhost/127.0.0.1
        if any(x in self.host for x in ['localhost', '127.0.0.1', '0.0.0.0']):
            self._detected_format = "ollama"
            return "ollama"

        self._detected_format = "openai"
        return "openai"

    def _is_cerebras(self) -> bool:
        """Check if the host is Cerebras."""
        return "cerebras" in self.host.lower()

    def _is_local_host(self) -> bool:
        return any(x in self.host for x in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))

    def _use_curl_cffi(self) -> bool:
        """Cloud hosts get routed through curl_cffi's Chrome TLS
        impersonation to get past Cloudflare-style bot walls; local Ollama
        has no WAF in front of it, so it stays on plain urllib."""
        if self._is_local_host():
            return False
        if CURL_CFFI_AVAILABLE:
            return True
        if not self._warned_no_curl:
            self._warned_no_curl = True
            logger.warning(
                f"Cloud host {self.host} detected but curl_cffi isn't installed — "
                f"falling back to urllib, which Cloudflare-protected APIs (e.g. Cerebras) "
                f"may block as bot traffic. Run: pip install curl_cffi"
            )
        return False

    def _get_curl_session(self):
        if self._curl_session is None:
            self._curl_session = curl_requests.Session(impersonate="chrome")
            logger.info(f"Using curl_cffi (Chrome TLS impersonation) transport for {self.host}")
        return self._curl_session

    def _is_openai_format(self) -> bool:
        fmt = self.api_format
        if fmt == "auto":
            fmt = self._detect_format()
        return fmt == "openai"

    # ── low-level HTTP ────────────────────────────────────────

    def _build_headers(self) -> Dict[str, str]:
        """Build headers with API key authentication for any provider."""
        headers = dict(JSON_HEADERS)
        # Cloudflare and some APIs block Python-urllib user-agent
        headers.setdefault("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

        # Always include Authorization header if api_key is set
        if self.api_key and self.api_key.strip():
            if self.auth_type == "Bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            elif self.auth_type == "Basic":
                import base64
                headers["Authorization"] = f"Basic {base64.b64encode(self.api_key.encode()).decode()}"
            elif self.auth_type == "X-API-Key":
                headers["X-API-Key"] = self.api_key
            else:
                # Default to Bearer for most OpenAI-compatible APIs including Cerebras
                headers["Authorization"] = f"Bearer {self.api_key}"

        return headers

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context. Allow insecure HTTPS when configured."""
        ctx = ssl.create_default_context()
        if os.environ.get("EVO_INSECURE_HTTPS", "").lower() == "true":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _urlopen(self, req, timeout: Optional[int] = None):
        """Open URL with automatic SSL fallback on certificate verification failure.
        Cloud hosts are routed through curl_cffi (see _use_curl_cffi) so
        Cloudflare-style bot walls don't block them."""
        if self._use_curl_cffi():
            return self._curl_urlopen(req, timeout)

        ctx = self._create_ssl_context()
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.request_timeout, context=ctx)
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "CERTIFICATE_VERIFY_FAILED" in reason or ("SSL" in reason and "verify" in reason.lower()):
                logger.warning(
                    f"SSL verification failed for {self.host}. "
                    f"Retrying with unverified context. "
                    f"Set EVO_INSECURE_HTTPS=true to skip verification by default."
                )
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return urllib.request.urlopen(req, timeout=timeout or self.request_timeout, context=ctx)
            raise

    def _curl_urlopen(self, req, timeout: Optional[int] = None):
        """Send a urllib.request.Request through curl_cffi's Chrome-
        impersonating transport, translating the result back into
        urllib-compatible objects (HTTPError / a response with
        .status/.read()) so every existing caller works unmodified."""
        session = self._get_curl_session()
        method = req.get_method()
        url = req.full_url
        headers = dict(req.header_items())
        data = req.data

        try:
            resp = session.request(
                method, url, data=data, headers=headers,
                timeout=timeout or self.request_timeout, stream=True,
            )
        except Exception as exc:
            raise urllib.error.URLError(f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            body = resp.content
            try:
                resp.close()
            except Exception:
                pass
            raise urllib.error.HTTPError(
                url, resp.status_code, resp.reason or "",
                dict(resp.headers), io.BytesIO(body),
            )

        return _CurlResponseAdapter(resp)

    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None,
                 method: str = "POST", timeout: Optional[int] = None):
        url = f"{self.host}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = self._build_headers()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        return self._urlopen(req, timeout)

    def _request_json(self, path: str, payload: Optional[Dict[str, Any]] = None,
                      method: str = "POST", timeout: Optional[int] = None) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                with self._request(path, payload, method, timeout) as response:
                    body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:500]
                raise OllamaError(f"HTTP {exc.code} on {path}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    delay = self.retry_backoff ** attempt
                    logger.warning(f"Request {path} attempt {attempt + 1} failed ({exc}); retry in {delay:.1f}s")
                    time.sleep(delay)
            except json.JSONDecodeError as exc:
                raise OllamaError(f"Malformed JSON from {path}: {exc}") from exc
        raise OllamaError(f"Host unreachable at {self.host}{path}: {last_error}")

    # ── OpenAI-compatible helpers ─────────────────────────────

    def _openai_chat_payload(self, messages: List[Dict[str, str]], stream: bool,
                            options: Optional[Dict[str, Any]], think: Optional[bool]) -> Dict[str, Any]:
        """Build OpenAI-compatible chat payload."""
        merged = dict(self.options)
        merged.update(options or {})

        # Convert messages to OpenAI format
        openai_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                openai_messages.append({"role": "system", "content": content})
            elif role == "user":
                openai_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                openai_messages.append({"role": "assistant", "content": content})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "stream": stream,
        }

        # Map options
        if "temperature" in merged:
            payload["temperature"] = merged["temperature"]
        if "top_p" in merged:
            payload["top_p"] = merged["top_p"]
        # V7: Unlimited output by default. For OpenAI-compatible hosts we must
        # send an explicit max_tokens because omitting it triggers the provider's
        # own tiny default (often 2-4K). We default to 128K which is effectively
        # unlimited for all current models. Override with EVO_CLOUD_MAX_OUTPUT.
        max_out = merged.get("num_predict", merged.get("max_tokens", -1))
        if max_out is not None and max_out > 0:
            payload["max_tokens"] = max_out
        else:
            payload["max_tokens"] = CLOUD_MAX_OUTPUT_FALLBACK  # 128K default — no truncation


        # CEREBRAS FIX: Cerebras uses max_completion_tokens instead of max_tokens
        if self._is_cerebras() and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        # OpenRouter and some providers support reasoning
        if think and self.reasoning_enabled:
            # Some OpenAI-compatible APIs support reasoning_effort
            if "openrouter" in self.host.lower() or "deepseek" in self.host.lower():
                payload["reasoning"] = {"effort": "high"}

        return payload

    def _openai_stream_parser(self, line: str) -> Optional[StreamEvent]:
        """Parse a single SSE line from OpenAI-compatible streaming."""
        """CEREBRAS FIX: Handle Cerebras SSE format variations."""
        line = line.strip()
        if not line or line.startswith(":"):
            return None
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                return StreamEvent("done", "", {"stopped": False, "reason": "stop"})
            try:
                chunk = json.loads(data)
                
                # CEREBRAS FIX: Handle Cerebras SSE format which may differ
                choices = chunk.get("choices", [{}])
                if not choices:
                    # Check for finish_reason at top level
                    if chunk.get("finish_reason"):
                        usage = chunk.get("usage", {})
                        return StreamEvent("done", "", {
                            "stopped": False,
                            "reason": chunk["finish_reason"],
                            "usage": usage
                        })
                    return None
                    
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                reasoning = delta.get("reasoning_content", "") or delta.get("reasoning", "")

                if reasoning:
                    return StreamEvent("reasoning", reasoning)
                if content:
                    return StreamEvent("content", content)

                # Check for finish_reason
                finish = choices[0].get("finish_reason")
                if finish:
                    usage = chunk.get("usage", {})
                    return StreamEvent("done", "", {
                        "stopped": False,
                        "reason": finish,
                        "usage": usage
                    })
            except json.JSONDecodeError:
                pass
        return None


    def _openai_extract_usage(self, response: Dict[str, Any]) -> Usage:
        """Extract usage from OpenAI-compatible response."""
        usage_data = response.get("usage", {})
        return Usage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            total_duration_ms=0,
            load_duration_ms=0,
            eval_rate=0.0,
        )

    # ── introspection ─────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        is_openai = self._is_openai_format()

        if is_openai:
            try:
                headers = self._build_headers()
                req = urllib.request.Request(
                    f"{self.host}/v1/models", 
                    headers=headers, 
                    method="GET"
                )
                with self._urlopen(req, timeout=self.connect_timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                models = [m.get("id", "") for m in payload.get("data", [])]
                return {
                    "online": True,
                    "host": self.host,
                    "model": self.model,
                    "model_available": self.model in models or any(self.model.split(":")[0] in m for m in models),
                    "models": models,
                    "format": "openai",
                }
            except Exception as exc:
                return {"online": False, "host": self.host, "model": self.model,
                        "model_available": False, "models": [], "error": str(exc), "format": "openai"}
        else:
            # Ollama path
            try:
                with self._request("/api/tags", method="GET", timeout=self.connect_timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                models = [m.get("name", "") for m in payload.get("models", [])]
                return {
                    "online": True,
                    "host": self.host,
                    "model": self.model,
                    "model_available": self._match_model(self.model, models) is not None,
                    "models": models,
                    "format": "ollama",
                }
            except Exception as exc:
                return {"online": False, "host": self.host, "model": self.model,
                        "model_available": False, "models": [], "error": str(exc), "format": "ollama"}

    def list_models(self) -> List[str]:
        is_openai = self._is_openai_format()

        if is_openai:
            try:
                headers = self._build_headers()
                req = urllib.request.Request(
                    f"{self.host}/v1/models", 
                    headers=headers, 
                    method="GET"
                )
                with self._urlopen(req, timeout=self.connect_timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return [m.get("id", "") for m in payload.get("data", [])]
            except Exception as exc:
                logger.warning(f"OpenAI list_models failed: {exc}")
                return []
        else:
            try:
                payload = self._request_json("/api/tags", method="GET", timeout=self.connect_timeout)
                return [m.get("name", "") for m in payload.get("models", [])]
            except Exception as exc:
                logger.warning(f"Ollama list_models failed: {exc}")
                return []

    @staticmethod
    def _match_model(wanted: str, available: List[str]) -> Optional[str]:
        if wanted in available:
            return wanted
        base = wanted.split(":")[0]
        for name in available:
            if name == base or name.split(":")[0] == base:
                return name
        return None

    def pull(self, model: Optional[str] = None,
             on_progress: Optional[Callable[[str], None]] = None) -> bool:
        if self._is_openai_format():
            logger.info("Pull not supported for OpenAI-compatible APIs")
            return True  # OpenAI APIs don't need pull

        target = model or self.model
        payload = {"name": target, "stream": True}
        try:
            with self._request("/api/pull", payload, timeout=3600) as response:
                for event in NdjsonReader(response):
                    status = event.get("status", "")
                    if on_progress and status:
                        on_progress(status)
                    if event.get("error"):
                        logger.error(f"Pull failed for {target}: {event['error']}")
                        return False
            logger.info(f"Model ready: {target}")
            return True
        except Exception as exc:
            logger.error(f"Pull error for {target}: {exc}")
            return False

    def resolve_model(self, on_progress: Optional[Callable[[str], None]] = None) -> str:
        """Pick a usable model. For OpenAI APIs, just return the configured model."""
        if self._is_openai_format():
            # For OpenAI-compatible APIs, the model string is just passed through
            logger.info(f"Using OpenAI-compatible model: {self.model}")
            return self.model

        # Ollama path
        try:
            available = self.list_models()
        except OllamaError as exc:
            logger.warning(f"Model discovery failed: {exc}")
            return self.model

        match = self._match_model(self.model, available)
        if match:
            self.model = match
            return match
        for candidate in FALLBACK_MODELS or []:
            match = self._match_model(candidate, available)
            if match:
                logger.warning(f"Configured model missing; using fallback {match}")
                self.model = match
                return match
        if LLM_AUTO_PULL and self.pull(self.model, on_progress):
            return self.model
        raise OllamaError(
            f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
        )

    # ── chat ──────────────────────────────────────────────────

    def _ollama_payload(self, messages: List[Dict[str, str]], stream: bool,
                        options: Optional[Dict[str, Any]], think: Optional[bool]) -> Dict[str, Any]:
        merged = dict(self.options)
        merged.update(options or {})
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": merged,
        }
        if self.native_think:
            payload["think"] = self.reasoning_enabled if think is None else bool(think)
        return payload

    def _record_usage(self, event: Dict[str, Any]) -> Usage:
        total_ns = int(event.get("total_duration", 0) or 0)
        eval_ns = int(event.get("eval_duration", 0) or 0)
        output_tokens = int(event.get("eval_count", 0) or 0)
        usage = Usage(
            prompt_tokens=int(event.get("prompt_eval_count", 0) or 0),
            output_tokens=output_tokens,
            total_duration_ms=total_ns // 1_000_000,
            load_duration_ms=int(event.get("load_duration", 0) or 0) // 1_000_000,
            eval_rate=(output_tokens / (eval_ns / 1_000_000_000)) if eval_ns else 0.0,
        )
        self.last_usage = usage
        return usage

    def stream_chat(self,
                    messages: List[Dict[str, str]],
                    options: Optional[Dict[str, Any]] = None,
                    think: Optional[bool] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> Iterator[StreamEvent]:
        """Yield StreamEvents as the model produces them."""

        is_openai = self._is_openai_format()

        if is_openai:
            yield from self._stream_chat_openai(messages, options, think, should_stop)
        else:
            yield from self._stream_chat_ollama(messages, options, think, should_stop)

    def _stream_chat_openai(self, messages, options, think, should_stop):
        """OpenAI-compatible streaming."""
        payload = self._openai_chat_payload(messages, True, options, think)
        headers = self._build_headers()
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            f"{self.host}/v1/chat/completions",
            data=data,
            headers=headers,
            method="POST"
        )

        attempt = 0
        while True:
            try:
                response = self._urlopen(req, timeout=self.request_timeout)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:500]
                yield StreamEvent("error", f"HTTP {exc.code}: {detail}")
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                attempt += 1
                if attempt >= self.max_retries:
                    yield StreamEvent("error", f"API unreachable at {self.host}: {exc}")
                    return
                time.sleep(self.retry_backoff ** attempt)

        splitter = ReasoningSplitter(self.open_tag, self.close_tag)
        reasoning_chars = 0
        content_parts: List[str] = []
        bytes_seen = 0

        try:
            with response:
                # Read SSE stream
                buffer = b""
                while True:
                    if should_stop and should_stop():
                        yield StreamEvent("done", "", {"stopped": True, "usage": self.last_usage.as_dict()})
                        return

                    chunk = response.read(1024)
                    if not chunk:
                        break
                    bytes_seen += len(chunk)
                    buffer += chunk

                    # Process complete lines
                    while b"\n" in buffer:
                        idx = buffer.index(b"\n")
                        line = buffer[:idx].decode("utf-8", errors="replace")
                        buffer = buffer[idx + 1:]

                        ev = self._openai_stream_parser(line)
                        if ev:
                            if ev.kind == "reasoning":
                                if reasoning_chars < self.max_reasoning_chars:
                                    reasoning_chars += len(ev.text)
                                    yield ev
                            elif ev.kind == "content":
                                content_parts.append(ev.text)
                                yield ev
                            elif ev.kind == "done":
                                # Build usage estimate
                                total_tokens = len("".join(content_parts)) // 4
                                self.last_usage = Usage(
                                    prompt_tokens=len(json.dumps(messages)) // 4,
                                    output_tokens=total_tokens,
                                )
                                yield StreamEvent("usage", "", self.last_usage.as_dict())
                                yield StreamEvent("done", "", {
                                    "stopped": False,
                                    "reason": ev.meta.get("reason", "stop"),
                                    "usage": self.last_usage.as_dict(),
                                    "transport": {"bytes": bytes_seen, "frames": 0, "resyncs": 0, "dropped": 0},
                                })
                                return

                # Flush any remaining reasoning
                for kind, text in splitter.flush():
                    if kind == "reasoning" and reasoning_chars < self.max_reasoning_chars:
                        yield StreamEvent(kind, text)
                    elif kind == "content":
                        yield StreamEvent(kind, text)

                # Final done event
                total_tokens = len("".join(content_parts)) // 4
                self.last_usage = Usage(
                    prompt_tokens=len(json.dumps(messages)) // 4,
                    output_tokens=total_tokens,
                )
                yield StreamEvent("usage", "", self.last_usage.as_dict())
                yield StreamEvent("done", "", {
                    "stopped": False,
                    "reason": "stop",
                    "usage": self.last_usage.as_dict(),
                    "transport": {"bytes": bytes_seen, "frames": 0, "resyncs": 0, "dropped": 0},
                })

        except Exception as exc:
            yield StreamEvent("error", f"Stream interrupted: {exc}")

    def _stream_chat_ollama(self, messages, options, think, should_stop):
        """Ollama native streaming."""
        payload = self._ollama_payload(messages, True, options, think)
        splitter = ReasoningSplitter(self.open_tag, self.close_tag)
        reasoning_chars = 0
        attempt = 0

        while True:
            try:
                response = self._request("/api/chat", payload)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:500]
                yield StreamEvent("error", f"HTTP {exc.code}: {detail}")
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                attempt += 1
                if attempt >= self.max_retries:
                    yield StreamEvent("error", f"Ollama unreachable at {self.host}: {exc}")
                    return
                time.sleep(self.retry_backoff ** attempt)

        bytes_seen = 0

        def count(block: bytes) -> None:
            nonlocal bytes_seen
            bytes_seen += len(block)

        try:
            with response:
                reader = NdjsonReader(response, should_stop=should_stop, on_raw=count)
                for event in reader:
                    if should_stop and should_stop():
                        yield StreamEvent("done", "", {"stopped": True,
                                                       "usage": self.last_usage.as_dict()})
                        return
                    if event.get("error"):
                        yield StreamEvent("error", str(event["error"]))
                        return

                    message = event.get("message") or {}
                    native = message.get("thinking") or ""
                    if native and reasoning_chars < self.max_reasoning_chars:
                        reasoning_chars += len(native)
                        yield StreamEvent("reasoning", native)

                    content = message.get("content") or ""
                    for kind, text in splitter.feed(content):
                        if kind == "reasoning":
                            if reasoning_chars >= self.max_reasoning_chars:
                                continue
                            reasoning_chars += len(text)
                        yield StreamEvent(kind, text)

                    if event.get("done"):
                        for kind, text in splitter.flush():
                            yield StreamEvent(kind, text)
                        usage = self._record_usage(event)
                        transport = {
                            "bytes": bytes_seen,
                            "frames": reader.stats.objects_parsed,
                            "resyncs": reader.stats.resyncs,
                            "dropped": reader.stats.dropped_lines,
                        }
                        yield StreamEvent("usage", "", {**usage.as_dict(),
                                                        "transport": transport})
                        yield StreamEvent("done", "", {
                            "stopped": False,
                            "reason": event.get("done_reason", "stop"),
                            "usage": usage.as_dict(),
                            "transport": transport,
                        })
                        return

                for kind, text in splitter.flush():
                    yield StreamEvent(kind, text)
                yield StreamEvent("done", "", {
                    "stopped": reader.stats.stopped,
                    "reason": "eof",
                    "usage": self.last_usage.as_dict(),
                    "transport": {"bytes": bytes_seen,
                                  "frames": reader.stats.objects_parsed,
                                  "resyncs": reader.stats.resyncs,
                                  "dropped": reader.stats.dropped_lines},
                })
                return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            for kind, text in splitter.flush():
                yield StreamEvent(kind, text)
            yield StreamEvent("error", f"Stream interrupted after {bytes_seen}B: {exc}")
            return

    def chat(self,
             messages: List[Dict[str, str]],
             options: Optional[Dict[str, Any]] = None,
             think: Optional[bool] = None) -> Dict[str, Any]:
        """Blocking chat."""
        is_openai = self._is_openai_format()

        if is_openai:
            payload = self._openai_chat_payload(messages, False, options, think)
            event = self._request_json("/v1/chat/completions", payload)
            choice = event.get("choices", [{}])[0]
            message = choice.get("message", {})
            raw = message.get("content", "")
            reasoning = message.get("reasoning_content", "") or message.get("reasoning", "")
            usage = self._openai_extract_usage(event)
            return {"content": raw.strip(), "reasoning": reasoning.strip(),
                    "usage": usage.as_dict(), "model": self.model}
        else:
            payload = self._ollama_payload(messages, False, options, think)
            event = self._request_json("/api/chat", payload)
            message = event.get("message") or {}
            raw = message.get("content") or ""
            reasoning = message.get("thinking") or ""
            if not reasoning and self.open_tag in raw:
                splitter = ReasoningSplitter(self.open_tag, self.close_tag)
                parts = splitter.feed(raw) + splitter.flush()
                reasoning = "".join(t for k, t in parts if k == "reasoning")
                content = "".join(t for k, t in parts if k == "content")
            else:
                content = raw
            usage = self._record_usage(event)
            return {"content": content.strip(), "reasoning": reasoning.strip(),
                    "usage": usage.as_dict(), "model": self.model}

    def generate(self, prompt: str, system: str = "",
                 options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        is_openai = self._is_openai_format()

        if is_openai:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            return self.chat(messages, options)
        else:
            merged = dict(self.options)
            merged.update(options or {})
            payload = {"model": self.model, "prompt": prompt, "stream": False,
                       "keep_alive": self.keep_alive, "options": merged}
            if system:
                payload["system"] = system
            event = self._request_json("/api/generate", payload)
            usage = self._record_usage(event)
            raw = event.get("response", "")
            return {"content": strip_reasoning(raw, self.open_tag, self.close_tag),
                    "raw": raw, "usage": usage.as_dict(), "model": self.model}

    def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        is_openai = self._is_openai_format()

        if is_openai:
            payload = {
                "model": model or self.model,
                "input": text
            }
            event = self._request_json("/v1/embeddings", payload)
            data = event.get("data", [])
            return data[0].get("embedding", []) if data else []
        else:
            payload = {"model": model or self.model, "input": text}
            event = self._request_json("/api/embed", payload)
            vectors = event.get("embeddings") or []
            return vectors[0] if vectors else []

    def unload(self) -> bool:
        if self._is_openai_format():
            # OpenAI APIs don't support unload
            return True
        try:
            self._request_json("/api/generate", {"model": self.model, "prompt": "",
                                                 "keep_alive": 0, "stream": False},
                               timeout=self.connect_timeout)
            return True
        except OllamaError as exc:
            logger.warning(f"Unload failed: {exc}")
            return False


# Global client bound to the configured model
ollama_client = OllamaClient()
