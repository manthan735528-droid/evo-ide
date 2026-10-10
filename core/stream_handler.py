#!/usr/bin/env python3
"""
Evo IDE V6.1 - Stream Handler
=============================
NdjsonReader: parses newline-delimited JSON from a chunked HTTP response.
ChunkedWriter: emits correct HTTP/1.1 chunked transfer encoding.
StreamSession: buffers events and coordinates the writer.

This module was missing from the uploaded codebase but is required by
ollama_client.py (NdjsonReader) and server_handler.py (ChunkedWriter,
StreamSession).
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, TextIO


# ── NdjsonReader ─────────────────────────────────────────────────────────

@dataclass
class _ReaderStats:
    """Internal telemetry for the reader."""
    objects_parsed: int = 0
    resyncs: int = 0
    dropped_lines: int = 0
    stopped: bool = False


class NdjsonReader:
    """Incremental newline-delimited JSON reader.

    Reads from a file-like object (typically an HTTP response) that yields
    bytes in arbitrary chunk sizes. Reassembles partial lines across chunk
    boundaries and yields parsed JSON objects.

    Args:
        response: A file-like object with a `.read(chunk_size)` method.
        should_stop: Optional callable; returning True aborts the stream.
        on_raw: Optional callback invoked with every raw bytes block.
        chunk_size: Number of bytes to read per iteration.
    """

    def __init__(self,
                 response,
                 should_stop: Optional[Callable[[], bool]] = None,
                 on_raw: Optional[Callable[[bytes], None]] = None,
                 chunk_size: int = 8192):
        self.response = response
        self.should_stop = should_stop
        self.on_raw = on_raw
        self.chunk_size = chunk_size
        self.stats = _ReaderStats()
        self._buffer = ""

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        """Yield parsed JSON objects until the stream ends."""
        try:
            while True:
                if self.should_stop and self.should_stop():
                    self.stats.stopped = True
                    return

                raw = self.response.read(self.chunk_size)
                if not raw:
                    break

                if self.on_raw:
                    self.on_raw(raw)

                self._buffer += raw.decode("utf-8", errors="replace")

                while "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        self.stats.objects_parsed += 1
                        yield obj
                    except json.JSONDecodeError:
                        self.stats.dropped_lines += 1
                        # Attempt a simple resync: if the buffer now starts
                        # with a `{`, try to find the next newline and parse.
                        if self._buffer.lstrip().startswith("{"):
                            self.stats.resyncs += 1

            # Drain any trailing content (no final newline)
            tail = self._buffer.strip()
            if tail:
                try:
                    obj = json.loads(tail)
                    self.stats.objects_parsed += 1
                    yield obj
                except json.JSONDecodeError:
                    self.stats.dropped_lines += 1
        finally:
            # Ensure the response is closed if it supports it
            close = getattr(self.response, "close", None)
            if close:
                close()


# ── ChunkedWriter ────────────────────────────────────────────────────────

class ChunkedWriter:
    """HTTP/1.1 chunked-transfer writer with heartbeat support.

    Emits properly framed chunks:
        <hex-length>\r\n
        <data>\r\n
    and a zero-length terminator at close.

    Args:
        wfile: The writable stream (e.g. `self.wfile` from a handler).
        chunked: If True, emit chunk framing; otherwise write raw bytes.
    """

    def __init__(self, wfile: TextIO, chunked: bool = True):
        self.wfile = wfile
        self.chunked = chunked
        self.broken = False
        self._last_write = time.time()

    def _write(self, data: bytes) -> bool:
        """Write bytes, catching BrokenPipe / ConnectionReset."""
        if self.broken:
            return False
        try:
            self.wfile.write(data)
            self.wfile.flush()
            self._last_write = time.time()
            return True
        except (BrokenPipeError, ConnectionResetError):
            self.broken = True
            return False

    def write(self, data: bytes) -> bool:
        """Write a data payload. If chunked, wrap it in chunk framing."""
        if self.chunked:
            chunk = f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n"
            return self._write(chunk)
        return self._write(data)

    def write_line(self, text: str) -> bool:
        """Encode text as UTF-8 and write it as one chunk/line."""
        return self.write(text.encode("utf-8") + b"\n")

    def heartbeat(self, idle_seconds: float = 8.0) -> bool:
        """Emit a comment heartbeat if the stream has been idle.

        Browsers and some proxies drop connections that are silent for too
        long. A comment line (starts with `:`) is ignored by NDJSON parsers
        but keeps the TCP socket alive.
        """
        if time.time() - self._last_write < idle_seconds:
            return True
        # Comment line — ignored by NDJSON consumers
        return self.write(b": \n")

    def close(self) -> None:
        """Emit the zero-length chunk terminator and flush."""
        if self.chunked and not self.broken:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.broken = True


# ── StreamSession ────────────────────────────────────────────────────────

class StreamSession:
    """Buffers events for a single streaming turn and coordinates the writer.

    This is a thin adapter: the orchestrator calls `emit()` with flat dicts,
    and the session serialises them as NDJSON lines through the ChunkedWriter.
    """

    def __init__(self, writer: ChunkedWriter, max_output: int = 4096):
        self.writer = writer
        self.max_output = max_output
        self.events: List[Dict[str, Any]] = []
        self._started = time.time()

    def emit(self, payload: Dict[str, Any]) -> bool:
        """Serialise *payload* as one NDJSON line and write it."""
        self.events.append(payload)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        return self.writer.write_line(line)

    def finalize(self, summary: Dict[str, Any]) -> bool:
        """Emit a final `result` event with the turn summary."""
        return self.emit({"event": "result", **summary})


__all__ = ["NdjsonReader", "ChunkedWriter", "StreamSession"]
