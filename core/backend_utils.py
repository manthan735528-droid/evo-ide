#!/usr/bin/env python3
"""
Evo IDE V5.2 - Backend Utilities

V6.2 Critical Fixes:
- FIXED: run_code now uses SYSTEM_PYTHON (your Mac Python) by default
- FIXED: run_code respects AUTO_RUN_ENABLED — returns terminal command if disabled
- FIXED: extract_file_blocks strips [RUN_CODE] more aggressively
- FIXED: Self-heal never auto-runs, only suggests terminal commands
- ADDED: get_terminal_debug_context() for LLM debugging via terminal output
- FIXED: install_module now targets the SAME interpreter that runs the code.
  When RUN_MODE="system" (default), uses `python3 -m pip install --break-system-packages`
  to bypass externally-managed-environment errors (PEP 668).
  When RUN_MODE="sandbox", uses the venv pip as before.
"""
import os
import re
import json
import shutil
import zipfile
import subprocess
import sys
from pathlib import Path
from datetime import datetime

from .config import (
    SANDBOX_DIR, MEMORY_FILE, VENV_PYTHON, VENV_PIP,
    SYSTEM_PYTHON, RUN_MODE, AUTO_RUN_ENABLED,
    CLAUDE_IN_PRICE_PER_M, CLAUDE_OUT_PRICE_PER_M,
    DEFAULT_TIMEOUT, logger
)
from .parsing import StructuralParser
from .indexing import vector_index
from .state import session_stats, file_lock
from .web_fetch import fetch_url, get_web_context


def extract_multipart_file(content_type: str, body: bytes, field_name: str = "file"):
    """Pull a named file field out of a multipart/form-data POST body."""
    from email.parser import BytesParser
    from email.policy import default as email_default_policy

    header_bytes = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser(policy=email_default_policy).parsebytes(header_bytes + body)
    if msg.is_multipart():
        for part in msg.iter_parts():
            name = part.get_param("name", header="Content-Disposition")
            if name == field_name:
                return part.get_payload(decode=True)
    return None


def append_memory(text):
    try:
        with file_lock:
            data = json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
            data["events"].append({"time": datetime.now().isoformat(), "text": text})
            MEMORY_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception as e:
        logger.warning(f"Memory error: {e}")


def get_god_view_context():
    context = "WORKSPACE FILES:\n"
    file_count = 0
    for root, dirs, files in os.walk(SANDBOX_DIR):
        if '.venv' in dirs:
            dirs.remove('.venv')
        if '__pycache__' in dirs:
            dirs.remove('__pycache__')
        if '.index' in dirs:
            dirs.remove('.index')
        if '.logs' in dirs:
            dirs.remove('.logs')
        if '.plugins' in dirs:
            dirs.remove('.plugins')
        if '.transactions' in dirs:
            dirs.remove('.transactions')
        for file in files:
            if file.endswith(('.py', '.json', '.txt', '.md', '.html', '.js', '.css')) and not file.startswith('.'):
                path = Path(root) / file
                rel_path = path.relative_to(SANDBOX_DIR)
                try:
                    content = path.read_text(encoding='utf-8')[:1200]
                    context += f"\n--- {rel_path} ---\n{content}\n"
                    file_count += 1
                except Exception:
                    pass
    context += f"\n[Total files: {file_count}]\n"
    return context


def get_structural_context(filename: str, cursor_line: int = 0) -> str:
    """Get structural context with RAG for a specific file and cursor position."""
    filepath = SANDBOX_DIR / filename
    if not filepath.exists():
        return ""

    try:
        code = filepath.read_text(encoding='utf-8')
        parser = StructuralParser(code, filename)

        node = parser.get_node_at_line(cursor_line)
        context = "CURRENT FILE STRUCTURE:\n"

        if node:
            context += f"\nCurrent position: {node.node_type} '{node.name}' (lines {node.start_line+1}-{node.end_line+1})\n"
            context += f"Signature: {node.signature}\n"
            if node.docstring:
                context += f"Docstring: {node.docstring[:200]}\n"
            if node.dependencies:
                context += f"Calls: {', '.join(node.dependencies)}\n"
            if node.is_async:
                context += "Type: async function\n"
            if node.decorators:
                context += f"Decorators: {', '.join(node.decorators)}\n"

        # Get RAG context
        rag_chunks = vector_index.search("", top_k=5, current_file=filename, cursor_line=cursor_line)
        if rag_chunks:
            context += "\nRELEVANT CONTEXT FROM PROJECT:\n"
            for chunk in rag_chunks:
                context += f"\n[{chunk['file']}::{chunk['name']}]\n"
                if chunk.get('signature'):
                    context += f"{chunk['signature']}\n"
                context += f"{chunk['content'][:500]}\n"

        return context
    except Exception as e:
        logger.error(f"Context error: {e}")
        return f"Context error: {e}"


def update_metrics(text_chunk, is_input=False):
    tokens = max(len(text_chunk) // 4, 1)
    if is_input:
        session_stats.input_tokens += tokens
    else:
        session_stats.output_tokens += tokens

    in_cost = (session_stats.input_tokens / 1_000_000) * CLAUDE_IN_PRICE_PER_M
    out_cost = (session_stats.output_tokens / 1_000_000) * CLAUDE_OUT_PRICE_PER_M
    session_stats.money_saved = in_cost + out_cost


def get_file_stats():
    count = 0
    for root, dirs, files in os.walk(SANDBOX_DIR):
        if '.venv' in dirs:
            dirs.remove('.venv')
        if '__pycache__' in dirs:
            dirs.remove('__pycache__')
        if '.index' in dirs:
            dirs.remove('.index')
        if '.logs' in dirs:
            dirs.remove('.logs')
        if '.plugins' in dirs:
            dirs.remove('.plugins')
        if '.transactions' in dirs:
            dirs.remove('.transactions')
        for f in files:
            if not f.startswith('.'):
                count += 1
    return count


def _strip_run_command(text: str) -> str:
    """Strip [RUN_CODE] meta-command from code content."""
    # Remove [RUN_CODE] on its own line or at end of file
    text = re.sub(r'\n?\[RUN_CODE\]\s*\n?', '\n', text)
    # Remove any trailing [RUN_CODE] without newlines
    text = re.sub(r'\[RUN_CODE\]\s*$', '', text)
    return text.strip()


def extract_file_blocks(full_response):
    """Extract file blocks from LLM response. Strips [RUN_CODE] from code.

    V6.1 FIX: previously the closing fence (```) was mandatory, so any
    response that got cut off mid-file (model hit its output/token limit
    before finishing) matched nothing at all and the whole file silently
    vanished instead of being offered for review — this is why large
    generations (roughly 500+ lines) would appear to "not save" while
    small ones worked fine. The terminator is now a closing ``` OR the
    end of the string, and each returned tuple carries a `truncated` flag
    so the caller can still recover the partial content but clearly warn
    that it may be incomplete instead of pretending it's a finished file.
    """
    files = []
    pattern = r'File:\s*([^\n]+)\n```(?:[a-zA-Z0-9+]*)\n(.*?)(```|\Z)'
    matches = re.finditer(pattern, full_response, re.DOTALL)
    for match in matches:
        filename = match.group(1).strip()
        code = match.group(2)
        truncated = match.group(3) != '```'
        # V5.2 FIX: Strip [RUN_CODE] from inside code blocks
        code = _strip_run_command(code)
        if filename and code:
            files.append((filename, code, truncated))
    return files


def extract_fix_blocks(full_response):
    """Extract targeted fix blocks. Strips [RUN_CODE] from fixes."""
    fixes = []
    pattern = r'Fix:\s*([^\n]+)\nLine:\s*(\d+)\n```(?:[a-zA-Z0-9+]*)\n(.*?)```'
    matches = re.finditer(pattern, full_response, re.DOTALL)
    for match in matches:
        filename = match.group(1).strip()
        line = int(match.group(2))
        code = match.group(3)
        # V5.2 FIX: Strip [RUN_CODE] from fix code
        code = _strip_run_command(code)
        if filename and code:
            fixes.append((filename, line, code))
    return fixes


def write_file(filename, content):
    safe_path = (SANDBOX_DIR / filename).resolve()
    try:
        safe_path.relative_to(SANDBOX_DIR.resolve())
    except ValueError:
        return False, "Invalid path"

    safe_path.parent.mkdir(parents=True, exist_ok=True)

    with file_lock:
        safe_path.write_text(content, encoding='utf-8')

    # Index the file
    rel = str(safe_path.relative_to(SANDBOX_DIR))
    vector_index.index_file(safe_path, rel)

    return True, safe_path


def delete_file(filename):
    safe_path = (SANDBOX_DIR / filename).resolve()
    try:
        safe_path.relative_to(SANDBOX_DIR.resolve())
    except ValueError:
        return False, "Invalid path"

    if safe_path.exists():
        if safe_path.is_file():
            with file_lock:
                safe_path.unlink()
        elif safe_path.is_dir():
            with file_lock:
                shutil.rmtree(safe_path)

        # Remove from index
        rel = str(safe_path.relative_to(SANDBOX_DIR))
        vector_index.remove_file(rel)
        return True, "Deleted"
    return False, "Not found"


def detect_missing_module(stderr):
    """Detect missing module from error output."""
    patterns = [
        r"ModuleNotFoundError: No module named '([^']+)'",
        r"ImportError: No module named '([^']+)'",
        r"ImportError: cannot import name '([^']+)' from '([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, stderr)
        if match:
            return match.group(1)
    return None


def _is_externally_managed(python_exe: str) -> bool:
    """Check if a Python interpreter is externally managed (PEP 668)."""
    try:
        result = subprocess.run(
            [python_exe, "-c", 
             "import sys, os; print(os.path.exists(os.path.join(sys.prefix, 'EXTERNALLY-MANAGED')))"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip() == "True"
    except Exception:
        pass
    # Fallback: check if pip install fails with externally-managed error
    try:
        result = subprocess.run(
            [python_exe, "-m", "pip", "install", "--dry-run", "nonexistent-package-12345"],
            capture_output=True, text=True, timeout=15
        )
        return "externally-managed-environment" in result.stderr
    except Exception:
        return False


def install_module(module_name):
    """Install a Python module into the interpreter that actually runs code.

    V6.2 FIX: When RUN_MODE="system" (default), installs into the system Python
    using `python -m pip install --break-system-packages` to bypass PEP 668
    externally-managed-environment errors. When RUN_MODE="sandbox", installs
    into the venv as before.
    """
    # Determine which Python interpreter we need to install for
    if RUN_MODE == "sandbox":
        python_cmd = str(VENV_PYTHON)
        pip_cmd = str(VENV_PIP)
        use_break_system = False
    else:
        # System mode: install into the same Python that runs the code
        python_cmd = str(SYSTEM_PYTHON)
        pip_cmd = None  # We'll use python -m pip
        # Check if system Python is externally managed (PEP 668)
        use_break_system = _is_externally_managed(python_cmd)
        if use_break_system:
            logger.info(f"System Python at {python_cmd} is externally managed; "
                       f"using --break-system-packages for {module_name}")

    try:
        if pip_cmd and not use_break_system:
            # Venv path: use the venv pip directly
            result = subprocess.run(
                [str(pip_cmd), "install", module_name],
                capture_output=True, text=True, timeout=120
            )
        else:
            # System path: use python -m pip with break-system-packages if needed
            cmd = [python_cmd, "-m", "pip", "install"]
            if use_break_system:
                cmd.append("--break-system-packages")
            cmd.append(module_name)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=120
            )

        success = result.returncode == 0
        output = result.stdout + result.stderr

        if success:
            logger.info(f"Installed module: {module_name}")
        else:
            logger.error(f"Failed to install {module_name}: {output}")
        return success, output
    except Exception as e:
        logger.error(f"Install exception for {module_name}: {e}")
        return False, str(e)


def run_code(filename, timeout=None):
    """
    Run code using SYSTEM_PYTHON (your Mac Python) by default.
    V5.2: Returns terminal command if AUTO_RUN is disabled.
    """
    timeout = timeout or DEFAULT_TIMEOUT
    file_path = SANDBOX_DIR / filename
    if not file_path.exists():
        return {
            "success": False,
            "stdout": "",
            "stderr": f"File not found: {filename}",
            "pip_install": None,
            "original_file": filename,
            "execution_time": 0,
            "terminal_command": f"cd {SANDBOX_DIR} && python3 {filename}",
            "auto_run": False
        }

    # If auto-run is disabled, return the command for the user to run manually
    if not AUTO_RUN_ENABLED:
        return {
            "success": True,
            "stdout": "",
            "stderr": "",
            "pip_install": None,
            "original_file": filename,
            "execution_time": 0,
            "terminal_command": f"cd {SANDBOX_DIR} && python3 {filename}",
            "auto_run": False,
            "message": "Auto-run is disabled. Run the command in the terminal below."
        }

    # Choose Python interpreter based on RUN_MODE
    python_cmd = str(SYSTEM_PYTHON) if RUN_MODE == "system" else str(VENV_PYTHON)
    if not python_cmd:
        return {
            "success": False,
            "stdout": "",
            "stderr": (
                "No host Python interpreter was found. Install Python and ensure "
                "python/py is on PATH, then restart Evo IDE."
            ),
            "pip_install": None,
            "original_file": filename,
            "execution_time": 0,
            "terminal_command": f"python {filename}",
            "auto_run": True,
        }

    start_time = datetime.now()
    try:
        result = subprocess.run(
            [python_cmd, str(file_path)],
            capture_output=True, text=True, cwd=str(SANDBOX_DIR), timeout=timeout
        )
        execution_time = (datetime.now() - start_time).total_seconds()
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "pip_install": None,
            "original_file": filename,
            "execution_time": execution_time,
            "terminal_command": f"cd {SANDBOX_DIR} && python3 {filename}",
            "auto_run": True,
            "python_used": python_cmd
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Execution timed out ({timeout}s)",
            "pip_install": None,
            "original_file": filename,
            "execution_time": timeout,
            "terminal_command": f"cd {SANDBOX_DIR} && python3 {filename}",
            "auto_run": True
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "pip_install": None,
            "original_file": filename,
            "execution_time": 0,
            "terminal_command": f"cd {SANDBOX_DIR} && python3 {filename}",
            "auto_run": True
        }


def run_terminal_command(command):
    """Run a terminal command in the sandbox directory using the system shell."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=str(SANDBOX_DIR), timeout=30
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "Command timed out"}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e)}


def import_project(zip_path):
    """Import a project from a ZIP file."""
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            files_imported = 0
            for name in z.namelist():
                if name.startswith('__MACOSX') or name.startswith('.'):
                    continue
                target = SANDBOX_DIR / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not name.endswith('/'):
                    with z.open(name) as src:
                        with file_lock:
                            target.write_bytes(src.read())
                    files_imported += 1
                    if name.endswith('.py'):
                        vector_index.index_file(target, name)
            logger.info(f"Imported {files_imported} files from ZIP")
            return True, files_imported
    except Exception as e:
        logger.error(f"Import failed: {e}")
        return False, str(e)


def get_execution_history():
    """Get recent execution history from memory."""
    try:
        with file_lock:
            data = json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
            return data.get("events", [])[-50:]  # Last 50 events
    except Exception:
        return []


def add_web_source(url: str, title: str = "", status: str = "fetched"):
    """Track a web source that was scraped or fetched."""
    source = {
        "url": url,
        "title": title or url,
        "status": status,
        "timestamp": datetime.now().isoformat()
    }
    # Prevent duplicates
    existing = [s for s in session_stats.web_sources if s["url"] == url]
    if not existing:
        session_stats.web_sources.append(source)
    else:
        existing[0]["timestamp"] = source["timestamp"]
        existing[0]["status"] = status


def get_web_sources() -> list:
    """Get list of tracked web sources."""
    return session_stats.web_sources


def get_terminal_debug_context() -> str:
    """
    Get the last terminal output for LLM debugging context.
    This is what the LLM sees when debugging — terminal output only.
    """
    try:
        with file_lock:
            data = json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
            events = data.get("events", [])
            # Filter for terminal-related events
            terminal_events = [e for e in events if "terminal" in e.get("text", "").lower() or "$" in e.get("text", "")]
            if terminal_events:
                return "\n".join([f"[{e['time']}] {e['text']}" for e in terminal_events[-10:]])
            return "No recent terminal output."
    except Exception:
        return ""
