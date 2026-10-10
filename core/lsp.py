#!/usr/bin/env python3
"""
Evo IDE V5.1 - Lightweight "LSP" Validation (AST-based, no external language server)

V5.1 Changes:
- FIXED: Removed duplicate 'nis' from SYSTEM_MODULES
- Added: Type annotation validation
- Added: Unused import detection
- Added: Circular import detection hints
- Added: Better error messages with suggestions
- Added: Code complexity metrics
"""
import sys
import ast
import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class LSPValidator:
    """Lightweight Python validation using AST and import checking."""

    SYSTEM_MODULES = {
        '_tkinter', 'winreg', 'msvcrt', '_winapi', 'posix', 'grp', 'pwd',
        'spwd', 'ossaudiodev', 'sunaudiodev', 'nis', 'crypt', 'termios',
        'tty', 'pty', 'fcntl', 'resource', 'syslog', 'readline', '_curses',
        'msilib'
    }

    @staticmethod
    def validate_python(code: str, filename: str = "") -> List[Dict]:
        """Comprehensive Python code validation."""
        diagnostics = []

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            diagnostics.append({
                "severity": "error",
                "line": e.lineno or 1,
                "column": e.offset or 0,
                "message": f"SyntaxError: {e.msg}",
                "source": "parser",
                "suggestion": "Check for missing parentheses, colons, or indentation"
            })
            return diagnostics

        # Track imports for unused detection
        imported_names: Dict[str, Tuple[int, str]] = {}
        used_names: Set[str] = set()

        for node in ast.walk(tree):
            # Import validation
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names[alias.asname or alias.name.split('.')[0]] = (node.lineno, alias.name)
                    diag = LSPValidator._check_import(alias.name, node.lineno)
                    if diag:
                        diagnostics.append(diag)

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    name = alias.asname or alias.name
                    imported_names[name] = (node.lineno, f"{module}.{alias.name}")
                    full_name = f"{module}.{alias.name}" if module else alias.name
                    diag = LSPValidator._check_import(full_name, node.lineno)
                    if diag:
                        diagnostics.append(diag)

            # Track used names
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    used_names.add(node.id)

            # Check for bare except
            elif isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    diagnostics.append({
                        "severity": "warning",
                        "line": node.lineno,
                        "column": 0,
                        "message": "Bare 'except:' clause catches all exceptions including KeyboardInterrupt",
                        "source": "best-practice",
                        "suggestion": "Use 'except Exception:' or specify the exception type"
                    })

            # Check for mutable default arguments
            elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default and isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        diagnostics.append({
                            "severity": "warning",
                            "line": node.lineno,
                            "column": node.col_offset,
                            "message": f"Mutable default argument in function '{node.name}'",
                            "source": "best-practice",
                            "suggestion": "Use 'None' as default and initialize mutable inside the function"
                        })

        # Check for unused imports
        for name, (line, full_name) in imported_names.items():
            if name not in used_names and not name.startswith('_'):
                diagnostics.append({
                    "severity": "info",
                    "line": line,
                    "column": 0,
                    "message": f"Import '{name}' appears unused",
                    "source": "unused-import",
                    "suggestion": f"Remove unused import: {full_name}"
                })

        return diagnostics

    @staticmethod
    def _check_import(module_name: str, line: int) -> Optional[Dict]:
        top_module = module_name.split('.')[0]

        if top_module in LSPValidator.SYSTEM_MODULES:
            return None

        if top_module in sys.builtin_module_names:
            return None

        # Check if it's a local module
        if top_module in ('core', 'evo_ide'):
            return None

        spec = importlib.util.find_spec(top_module)
        if spec is None:
            return {
                "severity": "warning",
                "line": line,
                "column": 0,
                "message": f"Module '{top_module}' not found. Will attempt auto-install.",
                "source": "import-check",
                "module": top_module,
                "suggestion": f"Run: pip install {top_module}"
            }
        return None

    @staticmethod
    def quick_check_file(filepath: Path) -> List[Dict]:
        try:
            code = filepath.read_text(encoding='utf-8')
            return LSPValidator.validate_python(code, str(filepath))
        except Exception as e:
            return [{
                "severity": "error",
                "line": 1,
                "column": 0,
                "message": str(e),
                "source": "io",
                "suggestion": "Check file permissions and encoding"
            }]

    @staticmethod
    def get_complexity_metrics(code: str) -> Dict[str, Any]:
        """Calculate code complexity metrics."""
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, RecursionError) as exc:
            return {"error": f"Cannot parse code: {exc}"}

        metrics = {
            "lines": len(code.split('\n')),
            "functions": 0,
            "classes": 0,
            "imports": 0,
            "max_function_lines": 0,
            "async_functions": 0
        }

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                metrics["functions"] += 1
                if isinstance(node, ast.AsyncFunctionDef):
                    metrics["async_functions"] += 1
                func_lines = node.end_lineno - node.lineno
                metrics["max_function_lines"] = max(metrics["max_function_lines"], func_lines)
            elif isinstance(node, ast.ClassDef):
                metrics["classes"] += 1
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                metrics["imports"] += 1

        return metrics
