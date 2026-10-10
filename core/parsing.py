#!/usr/bin/env python3
"""
Evo IDE V5 - Structural Code Parsing (AST-based "Tree-sitter lite")

V5 Changes:
- Added async/await support detection
- Added decorator extraction
- Added type hint extraction
- Better error recovery for malformed code
- Added module-level docstring extraction
"""
import ast
import re
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field


@dataclass
class CodeNode:
    """Represents a structural node in code (function, class, method, etc.)"""
    node_type: str
    name: str
    start_line: int
    end_line: int
    start_col: int
    end_col: int
    content: str
    parent: Optional[str] = None
    children: List[str] = field(default_factory=list)
    signature: str = ""
    docstring: str = ""
    dependencies: List[str] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)
    returns: Optional[str] = None
    is_async: bool = False
    is_static: bool = False
    is_classmethod: bool = False
    is_property: bool = False


class StructuralParser:
    """Python AST-based structural parser with enhanced metadata extraction."""

    def __init__(self, code: str, filename: str = ""):
        self.code = code
        self.filename = filename
        self.lines = code.split('\n')
        self.nodes: Dict[str, CodeNode] = {}
        self.imports: List[str] = []
        self.module_docstring: str = ""
        self._parse()

    def _parse(self):
        try:
            tree = ast.parse(self.code)
            self.module_docstring = ast.get_docstring(tree) or ""
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.FunctionDef):
                    self._extract_function(node)
                elif isinstance(node, ast.AsyncFunctionDef):
                    self._extract_function(node, is_async=True)
                elif isinstance(node, ast.ClassDef):
                    self._extract_class(node)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    self._extract_import(node)
                elif isinstance(node, ast.Assign):
                    self._extract_global_var(node)
        except SyntaxError as e:
            # Create a minimal node so the system doesn't crash
            self.nodes["__syntax_error__"] = CodeNode(
                node_type="error",
                name="syntax_error",
                start_line=e.lineno or 1,
                end_line=e.lineno or 1,
                start_col=e.offset or 0,
                end_col=e.offset or 0,
                content=str(e),
                docstring="Syntax error in file"
            )

    def _extract_decorators(self, node) -> List[str]:
        """Extract decorator names from a function/class node."""
        decorators = []
        for dec in node.decorator_list:
            try:
                if isinstance(dec, ast.Name):
                    decorators.append(dec.id)
                elif isinstance(dec, ast.Call):
                    if isinstance(dec.func, ast.Name):
                        decorators.append(dec.func.id)
                    elif isinstance(dec.func, ast.Attribute):
                        decorators.append(dec.func.attr)
                elif isinstance(dec, ast.Attribute):
                    decorators.append(dec.attr)
            except Exception:
                pass
        return decorators

    def _extract_function(self, node, parent: str = None, is_async: bool = False):
        start_line = node.lineno - 1
        end_line = node.end_lineno
        content = '\n'.join(self.lines[start_line:end_line])

        args = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                try:
                    arg_str += f": {ast.unparse(arg.annotation)}"
                except Exception:
                    pass
            args.append(arg_str)
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")

        decorators = self._extract_decorators(node)
        is_static = "staticmethod" in decorators
        is_classmethod = "classmethod" in decorators
        is_property = "property" in decorators

        prefix = "async " if is_async else ""
        signature = f"{prefix}def {node.name}({', '.join(args)})"

        if node.returns:
            try:
                signature += f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass
        signature += ":"

        docstring = ast.get_docstring(node) or ""

        deps = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    deps.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    deps.append(child.func.attr)

        node_id = f"{parent}.{node.name}" if parent else node.name
        self.nodes[node_id] = CodeNode(
            node_type="function",
            name=node.name,
            start_line=start_line,
            end_line=end_line,
            start_col=node.col_offset,
            end_col=node.end_col_offset,
            content=content,
            parent=parent,
            signature=signature,
            docstring=docstring,
            dependencies=list(set(deps)),
            decorators=decorators,
            is_async=is_async,
            is_static=is_static,
            is_classmethod=is_classmethod,
            is_property=is_property
        )

        if parent:
            self.nodes[parent].children.append(node_id)

        for child in node.body:
            if isinstance(child, ast.FunctionDef):
                self._extract_function(child, node_id)
            elif isinstance(child, ast.AsyncFunctionDef):
                self._extract_function(child, node_id, is_async=True)

    def _extract_class(self, node: ast.ClassDef):
        start_line = node.lineno - 1
        end_line = node.end_lineno
        content = '\n'.join(self.lines[start_line:end_line])

        bases = []
        for b in node.bases:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                bases.append("object")

        signature = f"class {node.name}"
        if bases:
            signature += f"({', '.join(bases)})"
        signature += ":"

        docstring = ast.get_docstring(node) or ""
        decorators = self._extract_decorators(node)

        self.nodes[node.name] = CodeNode(
            node_type="class",
            name=node.name,
            start_line=start_line,
            end_line=end_line,
            start_col=node.col_offset,
            end_col=node.end_col_offset,
            content=content,
            signature=signature,
            docstring=docstring,
            decorators=decorators
        )

        for child in node.body:
            if isinstance(child, ast.FunctionDef):
                self._extract_function(child, node.name)
            elif isinstance(child, ast.AsyncFunctionDef):
                self._extract_function(child, node.name, is_async=True)

    def _extract_import(self, node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                self.imports.append(f"{module}.{alias.name}")

    def _extract_global_var(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                start_line = node.lineno - 1
                end_line = node.end_lineno
                content = '\n'.join(self.lines[start_line:end_line])
                self.nodes[target.id] = CodeNode(
                    node_type="global_var",
                    name=target.id,
                    start_line=start_line,
                    end_line=end_line,
                    start_col=node.col_offset,
                    end_col=node.end_col_offset,
                    content=content
                )

    def get_node_at_line(self, line: int) -> Optional[CodeNode]:
        """Get the most specific node containing the given line."""
        best_match = None
        for node in self.nodes.values():
            if node.start_line <= line <= node.end_line:
                if best_match is None:
                    best_match = node
                elif (node.end_line - node.start_line) < (best_match.end_line - best_match.start_line):
                    best_match = node
        return best_match

    def get_context_around_line(self, line: int, context_lines: int = 5) -> str:
        """Get context around a line, preferring the containing node."""
        node = self.get_node_at_line(line)
        if node:
            return node.content
        start = max(0, line - context_lines)
        end = min(len(self.lines), line + context_lines + 1)
        return '\n'.join(self.lines[start:end])

    def get_relevant_nodes(self, query: str) -> List[CodeNode]:
        """Find nodes relevant to a query string."""
        query_lower = query.lower()
        relevant = []
        for node in self.nodes.values():
            score = 0
            if query_lower in node.name.lower():
                score += 10
            if query_lower in node.content.lower():
                score += 5
            if query_lower in node.docstring.lower():
                score += 3
            if query_lower in ' '.join(node.dependencies).lower():
                score += 2
            if score > 0:
                relevant.append((score, node))
        relevant.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in relevant]

    def get_project_structure(self) -> Dict[str, Any]:
        """Return a hierarchical structure of the file for visualization."""
        structure = {
            "type": "file",
            "name": self.filename,
            "docstring": self.module_docstring,
            "imports": self.imports,
            "children": []
        }

        for node_id, node in self.nodes.items():
            if node.parent is None:
                structure["children"].append({
                    "id": node_id,
                    "type": node.node_type,
                    "name": node.name,
                    "signature": node.signature,
                    "line_start": node.start_line + 1,
                    "line_end": node.end_line,
                    "docstring": node.docstring[:100] if node.docstring else "",
                    "is_async": node.is_async,
                    "children": self._get_child_structure(node_id)
                })

        return structure

    def _get_child_structure(self, parent_id: str) -> List[Dict[str, Any]]:
        """Recursively get child nodes."""
        children = []
        for node_id, node in self.nodes.items():
            if node.parent == parent_id:
                children.append({
                    "id": node_id,
                    "type": node.node_type,
                    "name": node.name,
                    "signature": node.signature,
                    "line_start": node.start_line + 1,
                    "line_end": node.end_line,
                    "docstring": node.docstring[:100] if node.docstring else "",
                    "is_async": node.is_async,
                    "children": self._get_child_structure(node_id)
                })
        return children
