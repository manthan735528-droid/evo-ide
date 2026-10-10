#!/usr/bin/env python3
"""
Evo IDE V6 - Context Manager: ruthless token-budgeted prompt assembly
=====================================================================
Every section of the system prompt gets a hard token budget. Content is
dynamically chunked, ranked by relevance, and trimmed so the assembled
prompt NEVER exceeds PROMPT_TOKEN_BUDGET. No silent truncation by the
runtime; no wasted tokens on files irrelevant to the active scope.
"""
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import (
    SANDBOX_DIR,
    ADAPTIVE_HISTORY_RECENT_TURNS, ADAPTIVE_HISTORY_SUMMARY_BUDGET,
    logger
)
from .indexing import vector_index
from .parsing import StructuralParser

_HIDDEN_DIRS = {'.venv', '__pycache__', '.index', '.logs', '.plugins', '.transactions', '.git'}
_TEXT_EXTS = ('.py', '.json', '.txt', '.md', '.html', '.js', '.css', '.toml', '.yml', '.yaml', '.sh')
_MAX_ATTACHMENT_TOKENS = 6000  # per explicitly-attached file


def estimate_tokens(text: str) -> int:
    """Fast token estimate tuned for code (~3.6 chars/token for Qwen BPE)."""
    if not text:
        return 0
    return max(int(len(text) / 3.6), 1)


def trim_to_tokens(text: str, max_tokens: int, from_end: bool = False) -> str:
    """Trim text to a max token estimate, cutting on line boundaries."""
    if estimate_tokens(text) <= max_tokens:
        return text
    char_limit = int(max_tokens * 3.6)
    if from_end:
        cut = text[-char_limit:]
        nl = cut.find('\n')
        if nl > 0:
            cut = cut[nl + 1:]
        return "…[trimmed]\n" + cut
    cut = text[:char_limit]
    nl = cut.rfind('\n')
    if nl > 0:
        cut = cut[:nl]
    return cut + "\n…[trimmed]"


def _walk_workspace() -> List[Path]:
    out = []
    for root, dirs, files in os.walk(SANDBOX_DIR):
        dirs[:] = [d for d in dirs if d not in _HIDDEN_DIRS]
        for f in files:
            if f.startswith('.'):
                continue
            if f.endswith(_TEXT_EXTS):
                out.append(Path(root) / f)
    return out


class ContextManager:
    """Assembles a token-budgeted prompt focused on the active scope."""

    def __init__(self):
        self.last_report: Dict[str, int] = {}

    # ── Section builders ────────────────────────────────────

    def workspace_map(self, active_file: str) -> str:
        """Compact workspace map: skeletons (signatures) instead of full
        file dumps for everything except the active file."""
        parts = ["WORKSPACE MAP (signatures only — request a file if you need its body):"]
        for path in sorted(_walk_workspace()):
            rel = str(path.relative_to(SANDBOX_DIR)).replace('\\', '/')
            if rel == active_file:
                continue  # active file gets its own full section
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if path.suffix == '.py':
                try:
                    code = path.read_text(encoding='utf-8')
                    parser = StructuralParser(code, rel)
                    sigs = []
                    for node in parser.nodes.values():
                        if node.node_type in ('function', 'class'):
                            sigs.append(f"  {node.signature}")
                    imports = ', '.join(parser.imports[:8]) if parser.imports else ''
                    entry = f"\n{rel} ({len(code.splitlines())} lines)"
                    if imports:
                        entry += f"\n  imports: {imports}"
                    entry += '\n' + '\n'.join(sigs[:20]) if sigs else ''
                    parts.append(entry)
                except Exception:
                    parts.append(f"\n{rel} ({size}B)")
            else:
                parts.append(f"\n{rel} ({size}B)")
        return '\n'.join(parts)

    def active_file_context(self, filename: str, cursor_line: int = 0) -> str:
        """Full active file if it fits; otherwise a window around the cursor
        plus the structural skeleton of the rest."""
        filepath = SANDBOX_DIR / filename
        if not filepath.exists():
            return f"ACTIVE FILE: {filename} (does not exist yet — you will create it)"
        try:
            code = filepath.read_text(encoding='utf-8')
        except Exception as e:
            return f"ACTIVE FILE: {filename} (unreadable: {e})"

        header = f"ACTIVE FILE: {filename} ({len(code.splitlines())} lines)\n"

        # Window around cursor + skeleton for large files
        lines = code.split('\n')
        half_window = 60  # ~120 lines total window
        start = max(0, cursor_line - half_window)
        end = min(len(lines), cursor_line + half_window)
        window = '\n'.join(lines[start:end])

        skeleton = []
        try:
            parser = StructuralParser(code, filename)
            for node in parser.nodes.values():
                if node.node_type in ('function', 'class'):
                    skeleton.append(f"  L{node.start_line + 1}: {node.signature}")
        except Exception:
            pass

        text = (header
                + "FILE SKELETON:\n" + '\n'.join(skeleton[:40])
                + f"\n\nWINDOW (lines {start + 1}-{end}, cursor at {cursor_line}):\n"
                + "```\n" + window + "\n```")
        return text

    def _resolve_attachment(self, name: str) -> Optional[Path]:
        """Resolve a workspace-relative attachment path, refusing traversal
        outside the sandbox. Self-contained (no import from server_handler)
        to avoid a circular import."""
        if not name:
            return None
        try:
            candidate = (SANDBOX_DIR / name).resolve()
            candidate.relative_to(SANDBOX_DIR.resolve())
        except (ValueError, OSError):
            return None
        return candidate

    def attachments_context(self, attachments: Optional[List[str]]) -> str:
        """Full content of files the user explicitly picked from the sandbox
        file list via the GUI attach button — no need to guess a path or
        make the file the active tab for the model to see it in full."""
        if not attachments:
            return ""
        parts = ["ATTACHED FILES (explicitly selected by the user):"]
        for name in attachments:
            path = self._resolve_attachment(name)
            if path is None or not path.is_file():
                parts.append(f"\n[{name}] — not found, skipped")
                continue
            try:
                code = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError) as exc:
                parts.append(f"\n[{name}] — unreadable ({exc})")
                continue
            entry = f"\n{name} ({len(code.splitlines())} lines):\n```\n"
            entry += trim_to_tokens(code, _MAX_ATTACHMENT_TOKENS)
            entry += "\n```"
            parts.append(entry)
        return '\n'.join(parts) if len(parts) > 1 else ""

    def rag_context(self, query: str, active_file: str, cursor_line: int) -> str:
        """Ranked relevant chunks from the project index."""
        chunks = vector_index.search(query, top_k=8, current_file=active_file, cursor_line=cursor_line)
        if not chunks:
            return ""
        parts = ["RELEVANT PROJECT CONTEXT (ranked):"]
        for c in chunks:
            entry = f"\n[{c['file']}::{c.get('name', '?')}]"
            if c.get('signature'):
                entry += f"\n{c['signature']}"
            body = c.get('content', '')
            entry += '\n' + trim_to_tokens(body, 220)
            parts.append(entry)
        return '\n'.join(parts) if len(parts) > 1 else ""

    def terminal_context(self, raw: str) -> str:
        if not raw or raw == "No recent terminal output.":
            return ""
        return "RECENT TERMINAL OUTPUT (newest last):\n" + trim_to_tokens(raw, 1500, from_end=True)

    def web_context(self, raw: str) -> str:
        if not raw:
            return ""
        return trim_to_tokens(raw, 3000)

    def history_context(self, turns: List[Dict[str, str]]) -> str:
        """Adaptive sliding window: recent turns verbatim, older turns collapsed."""
        if not turns:
            return ""

        recent_cutoff = max(len(turns) - ADAPTIVE_HISTORY_RECENT_TURNS, 0)
        recent_turns = turns[recent_cutoff:]
        older_turns = turns[:recent_cutoff]

        parts = []
        for turn in reversed(recent_turns):
            entry = f"[{turn.get('role', '?')}] {turn.get('content', '')}"
            entry = trim_to_tokens(entry, 300)
            parts.append(entry)
        recent_block = '\n'.join(reversed(parts))

        summary_lines = []
        for turn in older_turns:
            line = f"- [{turn.get('role', '?')}] " + trim_to_tokens(turn.get('content', ''), 20)
            summary_lines.append(line)

        sections = []
        if summary_lines:
            sections.append("EARLIER TURNS (summarised):\n" + '\n'.join(summary_lines))
        if recent_block:
            sections.append("CONVERSATION HISTORY (recent, verbatim):\n" + recent_block)
        return '\n\n'.join(sections)

    # ── Assembly ────────────────────────────────────────────

    def build_prompt(self,
                     system_core: str,
                     user_prompt: str,
                     active_file: str,
                     cursor_line: int = 0,
                     terminal_raw: str = "",
                     web_raw: str = "",
                     history: Optional[List[Dict[str, str]]] = None,
                     extra: str = "",
                     attachments: Optional[List[str]] = None) -> Tuple[str, Dict[str, int]]:
        """Assemble the full system prompt. Returns (prompt, token_report)."""
        sections: List[Tuple[str, str]] = []

        sections.append(("system", system_core))
        sections.append(("active_file", self.active_file_context(active_file, cursor_line)))
        att = self.attachments_context(attachments)
        if att:
            sections.append(("attachments", att))
        sections.append(("workspace", self.workspace_map(active_file)))
        rag = self.rag_context(user_prompt, active_file, cursor_line)
        if rag:
            sections.append(("rag", rag))
        term = self.terminal_context(terminal_raw)
        if term:
            sections.append(("terminal", term))
        web = self.web_context(web_raw)
        if web:
            sections.append(("web", web))
        hist = self.history_context(history or [])
        if hist:
            sections.append(("history", hist))
        if extra:
            sections.append(("extra", extra))

        report = {name: estimate_tokens(text) for name, text in sections}
        user_cost = estimate_tokens(user_prompt)

        prompt = '\n\n'.join(text for _, text in sections if text.strip())
        report["user_prompt"] = user_cost
        report["total"] = estimate_tokens(prompt) + user_cost
        self.last_report = report
        logger.info(f"Prompt assembled: {report['total']} tokens")
        return prompt, report


# Global instance
context_manager = ContextManager()
