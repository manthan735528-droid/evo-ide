#!/usr/bin/env python3
"""
Evo IDE V5.1 - Diff Engine: targeted, line-scoped code fixes

V5.1 Fixes:
- FIXED: Removed unused error_msg parameter from fix_specific_line
- FIXED: Self-heal loop falling back to wrong file
- Added: Safe line number validation
- Added: Multi-line fix support with boundary checks
- Added: Diff preview before applying
"""
import re
import difflib
from typing import Dict, List, Optional, Tuple

from .parsing import StructuralParser


class DiffEngine:
    """Generate and apply targeted code changes with safety checks."""

    @staticmethod
    def _validate_line_number(code: str, line_num: int) -> Tuple[bool, int, str]:
        """
        Validate a line number is within bounds.
        Returns (is_valid, clamped_line, message).
        """
        lines = code.split('\n')
        total_lines = len(lines)

        if total_lines == 0:
            return False, 0, "Code is empty"

        if line_num < 1:
            return False, 1, f"Line {line_num} < 1, clamped to 1"

        if line_num > total_lines:
            return False, total_lines, f"Line {line_num} > {total_lines}, clamped to {total_lines}"

        return True, line_num, ""

    @staticmethod
    def generate_inline_diff(old_code: str, new_code: str) -> List[Dict]:
        """Generate a list of change operations between old and new code."""
        old_lines = old_code.split('\n')
        new_lines = new_code.split('\n')

        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=''))

        changes = []
        i = 2
        while i < len(diff):
            line = diff[i]
            if line.startswith('@@'):
                match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                if match:
                    old_start = int(match.group(1))
                    old_count = int(match.group(2)) if match.group(2) else 1
                    new_start = int(match.group(3))
                    new_count = int(match.group(4)) if match.group(4) else 1

                    old_content = []
                    new_content = []
                    i += 1
                    while i < len(diff) and not diff[i].startswith('@@'):
                        if diff[i].startswith('-'):
                            old_content.append(diff[i][1:])
                        elif diff[i].startswith('+'):
                            new_content.append(diff[i][1:])
                        elif diff[i].startswith(' '):
                            old_content.append(diff[i][1:])
                            new_content.append(diff[i][1:])
                        i += 1

                    changes.append({
                        "type": "replace",
                        "old_start": old_start,
                        "old_end": old_start + old_count - 1,
                        "new_start": new_start,
                        "new_content": '\n'.join(new_content),
                        "old_content": '\n'.join(old_content)
                    })
                    continue
            i += 1

        return changes

    @staticmethod
    def apply_diff(original: str, changes: List[Dict]) -> str:
        """Apply a list of changes to original code."""
        lines = original.split('\n')
        sorted_changes = sorted(changes, key=lambda c: c["old_start"], reverse=True)

        for change in sorted_changes:
            if change["type"] == "replace":
                start = change["old_start"] - 1
                end = change["old_end"]
                new_lines = change["new_content"].split('\n')

                # Safety: clamp to valid range
                start = max(0, min(start, len(lines)))
                end = max(0, min(end, len(lines)))

                lines[start:end] = new_lines

        return '\n'.join(lines)

    @staticmethod
    def fix_specific_line(code: str, line_num: int, fix_code: str) -> str:
        """
        Apply a fix at a specific line with comprehensive safety checks.
        V5.1: Removed unused error_msg parameter.
        """
        lines = code.split('\n')
        total_lines = len(lines)

        # Validate line number
        is_valid, clamped_line, msg = DiffEngine._validate_line_number(code, line_num)

        if not is_valid:
            # If line is out of range, append the fix at the end
            if line_num > total_lines:
                lines.append(fix_code)
                return '\n'.join(lines)
            else:
                line_num = clamped_line

        parser = StructuralParser(code)
        node = parser.get_node_at_line(line_num - 1)

        if node and node.node_type in ("function", "class"):
            start = node.start_line
            end = node.end_line

            # Safety: ensure start/end are within bounds
            start = max(0, min(start, total_lines))
            end = max(0, min(end, total_lines))

            fix_lines = fix_code.split('\n')
            lines[start:end] = fix_lines
        else:
            # Single line replacement with bounds check
            idx = line_num - 1
            if 0 <= idx < len(lines):
                lines[idx] = fix_code
            else:
                # Fallback: append
                lines.append(fix_code)

        return '\n'.join(lines)

    @staticmethod
    def preview_fix(code: str, line_num: int, fix_code: str) -> Dict[str, str]:
        """Preview what a fix would look like without applying it."""
        is_valid, clamped_line, msg = DiffEngine._validate_line_number(code, line_num)

        parser = StructuralParser(code)
        node = parser.get_node_at_line(line_num - 1)

        preview = {
            "valid": is_valid,
            "line_requested": line_num,
            "line_actual": clamped_line,
            "message": msg,
            "target": "function" if node and node.node_type == "function" else "line",
            "target_name": node.name if node else "N/A",
            "original": "",
            "replacement": fix_code,
            "total_lines": len(code.split('\n'))
        }

        if node:
            preview["original"] = '\n'.join(code.split('\n')[node.start_line:node.end_line])
        elif 0 <= line_num - 1 < len(code.split('\n')):
            preview["original"] = code.split('\n')[line_num - 1]

        return preview

    @staticmethod
    def insert_after_line(code: str, line_num: int, new_code: str) -> str:
        """Insert code after a specific line."""
        lines = code.split('\n')
        is_valid, clamped_line, _ = DiffEngine._validate_line_number(code, line_num)

        idx = clamped_line  # Insert AFTER this line
        new_lines = new_code.split('\n')

        if idx >= len(lines):
            lines.extend(new_lines)
        else:
            for i, nl in enumerate(new_lines):
                lines.insert(idx + i, nl)

        return '\n'.join(lines)

    @staticmethod
    def native_fallback_patch(response: str, active_file: str,
                              current_content: Optional[str]) -> Optional[Tuple[str, str, bool]]:
        """Last-resort local parser for when the model didn't emit a
        `File:`/`Fix:` block the regex extractors understand. If the
        response is a single fenced code block with no file header, treat
        it as a full replacement of the active file — difflib (not the LLM)
        is what produces the actual diff shown to the user, so a malformed
        response still ends up as a safe, reviewable patch instead of a
        failed turn.

        V6.1 FIX: same unclosed-fence tolerance as extract_file_blocks — a
        response cut off before emitting the closing ``` used to match zero
        fences here too, so this fallback silently gave up right when it
        was needed most (large single-block responses that hit the output
        limit). Returns (filename, new_content, truncated) or None.
        """
        if current_content is None or not active_file:
            return None
        fences = list(re.finditer(r'```[a-zA-Z0-9+#.-]*\n(.*?)(```|\Z)', response, re.DOTALL))
        if len(fences) != 1:
            return None
        candidate = fences[0].group(1)
        truncated = fences[0].group(2) != '```'
        if not candidate.strip() or candidate.strip() == current_content.strip():
            return None
        return active_file, candidate, truncated

    @staticmethod
    def delete_lines(code: str, start_line: int, end_line: int) -> str:
        """Delete a range of lines safely."""
        lines = code.split('\n')
        total = len(lines)

        start = max(0, min(start_line - 1, total))
        end = max(0, min(end_line, total))

        if start < end:
            del lines[start:end]

        return '\n'.join(lines)
