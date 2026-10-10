#!/usr/bin/env python3
"""
Evo IDE V5 - Lightweight Vector Indexing & RAG (TF-IDF-like, no external deps)

V5 Changes:
- Added project structure export for canvas visualization
- Added file dependency graph
- Added import relationship tracking
- Better error recovery
"""
import os
import re
import json
import hashlib
from pathlib import Path
from typing import Dict, List, Any
from datetime import datetime

from .config import SANDBOX_DIR, INDEX_DIR, logger
from .parsing import StructuralParser


class VectorIndex:
    """Lightweight vector indexing for code chunks using TF-IDF-like scoring."""

    def __init__(self, index_dir: Path):
        self.index_dir = index_dir
        self._index_path = index_dir / "code_index.json"
        self.chunks: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if self._index_path.exists():
            try:
                self.chunks = json.loads(self._index_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Index load failed: {e}, starting fresh")
                self.chunks = {}

    def _save(self):
        try:
            self._index_path.write_text(json.dumps(self.chunks, indent=2), encoding='utf-8')
        except Exception as e:
            logger.error(f"Index save failed: {e}")

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())

    def _compute_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def index_file(self, filepath: Path, relative_path: str):
        try:
            content = filepath.read_text(encoding='utf-8')
            parser = StructuralParser(content, str(relative_path))

            for node_id, node in parser.nodes.items():
                chunk_id = f"{relative_path}::{node_id}"
                tokens = self._tokenize(node.content)

                self.chunks[chunk_id] = {
                    "file": relative_path,
                    "node_type": node.node_type,
                    "name": node.name,
                    "content": node.content,
                    "signature": node.signature,
                    "docstring": node.docstring,
                    "start_line": node.start_line,
                    "end_line": node.end_line,
                    "tokens": tokens,
                    "hash": self._compute_hash(node.content),
                    "timestamp": datetime.now().isoformat(),
                    "dependencies": node.dependencies,
                    "is_async": node.is_async,
                    "decorators": node.decorators
                }

            if parser.imports:
                chunk_id = f"{relative_path}::__imports__"
                self.chunks[chunk_id] = {
                    "file": relative_path,
                    "node_type": "imports",
                    "name": "imports",
                    "content": "\n".join(parser.imports),
                    "tokens": self._tokenize(" ".join(parser.imports)),
                    "hash": self._compute_hash(" ".join(parser.imports)),
                    "timestamp": datetime.now().isoformat()
                }

            self._save()
        except Exception as e:
            logger.error(f"Indexing error for {filepath}: {e}")

    def search(self, query: str, top_k: int = 5, current_file: str = "", cursor_line: int = 0) -> List[Dict]:
        query_tokens = set(self._tokenize(query))
        if not query_tokens:
            return []

        scored = []
        for chunk_id, chunk in self.chunks.items():
            score = 0
            chunk_tokens = set(chunk.get("tokens", []))

            overlap = len(query_tokens & chunk_tokens)
            score += overlap * 10

            if any(q in chunk.get("name", "").lower() for q in query_tokens):
                score += 20

            if chunk.get("file") == current_file:
                score += 15
                start = chunk.get("start_line", 0)
                end = chunk.get("end_line", 0)
                if start <= cursor_line <= end:
                    score += 30
                elif abs(start - cursor_line) < 20:
                    score += 10

            if any(q in chunk.get("docstring", "").lower() for q in query_tokens):
                score += 8

            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    def remove_file(self, relative_path: str):
        to_remove = [k for k, v in self.chunks.items() if v.get("file") == relative_path]
        for k in to_remove:
            del self.chunks[k]
        self._save()

    def rebuild(self):
        self.chunks = {}
        for root, dirs, files in os.walk(SANDBOX_DIR):
            if '.venv' in dirs:
                dirs.remove('.venv')
            if '__pycache__' in dirs:
                dirs.remove('__pycache__')
            if '.index' in dirs:
                dirs.remove('.index')
            for f in files:
                if f.endswith('.py'):
                    path = Path(root) / f
                    rel = str(path.relative_to(SANDBOX_DIR))
                    self.index_file(path, rel)
        self._save()

    def get_project_structure(self) -> Dict[str, Any]:
        """Export full project structure for canvas visualization."""
        structure = {
            "files": [],
            "dependencies": [],
            "stats": {
                "total_files": 0,
                "total_functions": 0,
                "total_classes": 0,
                "total_lines": 0
            }
        }

        files_seen = set()
        for chunk_id, chunk in self.chunks.items():
            filepath = chunk.get("file", "")
            if filepath not in files_seen and not filepath.endswith("::__imports__"):
                files_seen.add(filepath)
                structure["stats"]["total_files"] += 1

                # Parse file for structure
                try:
                    fpath = SANDBOX_DIR / filepath
                    content = fpath.read_text(encoding='utf-8')
                    parser = StructuralParser(content, filepath)
                    file_struct = parser.get_project_structure()
                    structure["files"].append(file_struct)

                    # Count stats
                    for node in parser.nodes.values():
                        if node.node_type == "function":
                            structure["stats"]["total_functions"] += 1
                        elif node.node_type == "class":
                            structure["stats"]["total_classes"] += 1

                    structure["stats"]["total_lines"] += len(content.split('\n'))

                    # Track imports as dependencies
                    for imp in parser.imports:
                        structure["dependencies"].append({
                            "from": filepath,
                            "import": imp
                        })

                except Exception as e:
                    logger.warning(f"Structure parse failed for {filepath}: {e}")

        return structure

    def get_dependency_graph(self) -> Dict[str, Any]:
        """Generate a dependency graph for visualization."""
        nodes = []
        edges = []
        node_ids = {}

        for chunk_id, chunk in self.chunks.items():
            filepath = chunk.get("file", "")
            if filepath.endswith("::__imports__"):
                continue

            if filepath not in node_ids:
                node_id = len(nodes)
                node_ids[filepath] = node_id
                nodes.append({
                    "id": node_id,
                    "label": filepath,
                    "type": "file"
                })

        # Build edges from imports
        for chunk_id, chunk in self.chunks.items():
            if chunk.get("node_type") == "imports":
                from_file = chunk.get("file", "")
                if from_file in node_ids:
                    for imp in chunk.get("content", "").split("\n"):
                        # Find if this import maps to another file in project
                        top_module = imp.split(".")[0] if imp else ""
                        for filepath in node_ids:
                            if filepath.replace("/", ".").replace("\\", ".").startswith(top_module):
                                edges.append({
                                    "source": node_ids[from_file],
                                    "target": node_ids[filepath],
                                    "label": imp
                                })

        return {"nodes": nodes, "edges": edges}


# Global index instance
vector_index = VectorIndex(INDEX_DIR)
