#!/usr/bin/env python3
"""
Evo IDE V7 - Patch Transaction Engine
======================================
Turns a set of proposed file changes into a reviewable, atomic unit:

  1. build a Transaction from (path, mode, old_content, new_content) tuples
  2. every non-identical region between old/new becomes a Hunk the caller
     can accept or reject independently
  3. commit() resolves accept/reject decisions into final per-file content,
     writes everything through a temp-file + os.replace two-phase commit
     (nothing lands on disk until every file has been staged successfully),
     and journals a revert record to SANDBOX_DIR/.transactions/<tx_id>.json
  4. revert() restores a previously committed transaction from that journal

Nothing here talks to the LLM. It is pure, deterministic, local-only code —
the "native edit engine" fallback path for when the model output can't be
trusted to describe its own diff.
"""
import difflib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SANDBOX_DIR, TRANSACTIONS_DIR, MAX_TRANSACTIONS_KEPT, logger
from .indexing import vector_index
from .state import file_lock


@dataclass
class Hunk:
    id: str
    tag: str            # replace | delete | insert
    old_start: int       # 1-based line number in old content
    old_lines: List[str]
    new_start: int       # 1-based line number in new content
    new_lines: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tag": self.tag,
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
        }


@dataclass
class PatchedFile:
    path: str
    mode: str  # write | fix | delete
    old_content: Optional[str]   # None => file does not exist yet
    new_content: Optional[str]   # None => file is being deleted
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    hunks: List[Hunk] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "mode": self.mode,
            "is_new": self.old_content is None,
            "is_delete": self.new_content is None,
            "diagnostics": self.diagnostics,
            "hunks": [h.as_dict() for h in self.hunks],
            "old_lines": len((self.old_content or "").splitlines()),
            "new_lines": len((self.new_content or "").splitlines()),
        }


@dataclass
class Transaction:
    tx_id: str
    created_at: float
    files: Dict[str, PatchedFile]
    status: str = "pending"  # pending | committed | rejected | reverted

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "created_at": self.created_at,
            "status": self.status,
            "files": [f.as_dict() for f in self.files.values()],
        }


_transactions: Dict[str, Transaction] = {}


def _split(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return text.split("\n")


def _hunks_from_diff(old_content: Optional[str], new_content: Optional[str]) -> List[Hunk]:
    old_lines = _split(old_content)
    new_lines = _split(new_content)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: List[Hunk] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes()):
        if tag == "equal":
            continue
        display_tag = "delete" if j1 == j2 else ("insert" if i1 == i2 else "replace")
        hunks.append(Hunk(
            id=f"h{idx}",
            tag=display_tag,
            old_start=i1 + 1,
            old_lines=old_lines[i1:i2],
            new_start=j1 + 1,
            new_lines=new_lines[j1:j2],
        ))
    return hunks


def build_patched_file(path: str, mode: str, old_content: Optional[str],
                        new_content: Optional[str],
                        diagnostics: Optional[List[Dict[str, Any]]] = None) -> PatchedFile:
    return PatchedFile(
        path=path, mode=mode, old_content=old_content, new_content=new_content,
        diagnostics=diagnostics or [], hunks=_hunks_from_diff(old_content, new_content),
    )


def unified_diff_text(path: str, old_content: Optional[str], new_content: Optional[str]) -> str:
    old_lines = _split(old_content)
    new_lines = _split(new_content)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}",
                                lineterm="")
    return "\n".join(diff)


def create_transaction(patched_files: List[PatchedFile]) -> Transaction:
    tx = Transaction(tx_id=uuid.uuid4().hex[:12], created_at=time.time(),
                     files={f.path: f for f in patched_files})
    _transactions[tx.tx_id] = tx
    return tx


def get_transaction(tx_id: str) -> Optional[Transaction]:
    return _transactions.get(tx_id)


def _resolve_file_content(pf: PatchedFile, file_decision: Dict[str, Any]) -> Optional[str]:
    """Reconstruct a file's final content honoring which hunks were rejected.
    file_decision: {"accepted": bool, "rejected_hunks": [hunk_id, ...]}."""
    if not file_decision.get("accepted", True):
        return pf.old_content  # unchanged
    rejected = set(file_decision.get("rejected_hunks") or [])
    if not rejected:
        return pf.new_content
    if pf.new_content is None:
        # a rejected delete-hunk set on a deletion just means "keep the file"
        return pf.old_content

    old_lines = _split(pf.old_content)
    new_lines = _split(pf.new_content)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    out: List[str] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes()):
        if tag == "equal":
            out.extend(old_lines[i1:i2])
            continue
        hunk_id = f"h{idx}"
        if hunk_id in rejected:
            out.extend(old_lines[i1:i2])
        else:
            out.extend(new_lines[j1:j2])
    return "\n".join(out)


def resolve_decision(tx: Transaction, decision: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Returns {path: final_content_or_None}. None means "delete / do not
    create". A path absent from the result was rejected outright."""
    action = decision.get("action", "apply_all")
    per_file = decision.get("files") or {}
    resolved: Dict[str, Optional[str]] = {}

    for path, pf in tx.files.items():
        if action == "reject_all":
            continue
        if action == "apply_all":
            resolved[path] = pf.new_content
            continue
        # selective
        file_decision = per_file.get(path, {"accepted": True})
        if not file_decision.get("accepted", True):
            continue
        resolved[path] = _resolve_file_content(pf, file_decision)
    return resolved


def _safe_target(path: str) -> Path:
    target = (SANDBOX_DIR / path).resolve()
    target.relative_to(SANDBOX_DIR.resolve())
    return target


def commit(tx: Transaction, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically write every accepted file. Two-phase: stage every file to a
    sibling temp path first; only if *all* stages succeed do we replace the
    real files. A staging failure leaves the workspace untouched."""
    resolved = resolve_decision(tx, decision)
    applied: List[Dict[str, Any]] = []
    rejected = [p for p in tx.files if p not in resolved]
    journal: Dict[str, Dict[str, Optional[str]]] = {}

    staged: List[Dict[str, Any]] = []
    try:
        with file_lock:
            for path, final_content in resolved.items():
                pf = tx.files[path]
                target = _safe_target(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if final_content is None:  # deletion
                    staged.append({"path": path, "target": target, "delete": True})
                    continue
                fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".evo_tx_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(final_content)
                staged.append({"path": path, "target": target, "tmp": tmp_name,
                               "content": final_content, "delete": False})

            for item in staged:
                if item["delete"]:
                    if item["target"].exists():
                        item["target"].unlink()
                else:
                    os.replace(item["tmp"], str(item["target"]))
    except Exception as exc:
        logger.error(f"Transaction {tx.tx_id} commit failed, rolling back stage: {exc}")
        for item in staged:
            tmp = item.get("tmp")
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        tx.status = "rejected"
        return {"ok": False, "error": str(exc), "applied": [], "rejected": list(tx.files.keys())}

    for item in staged:
        path, pf = item["path"], tx.files[item["path"]]
        journal[path] = {"old_content": pf.old_content,
                         "new_content": None if item["delete"] else item["content"]}
        rel = path
        if item["delete"]:
            vector_index.remove_file(rel)
        else:
            vector_index.index_file(item["target"], rel)
        applied.append({"path": path, "mode": pf.mode,
                        "lines": len((item.get("content") or "").splitlines()),
                        "deleted": item["delete"], "diagnostics": pf.diagnostics})

    tx.status = "committed"
    if journal:
        _write_journal(tx, journal)
    return {"ok": True, "applied": applied, "rejected": rejected}


def _write_journal(tx: Transaction, files: Dict[str, Dict[str, Optional[str]]]) -> None:
    TRANSACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    record = {"tx_id": tx.tx_id, "created_at": tx.created_at, "committed_at": time.time(),
              "status": "committed", "files": files}
    path = TRANSACTIONS_DIR / f"{tx.tx_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    _prune_journal()


def _prune_journal() -> None:
    entries = sorted(TRANSACTIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    excess = len(entries) - MAX_TRANSACTIONS_KEPT
    for p in entries[:max(excess, 0)]:
        try:
            p.unlink()
        except OSError:
            pass


def list_transactions(limit: int = 50) -> List[Dict[str, Any]]:
    if not TRANSACTIONS_DIR.exists():
        return []
    entries = sorted(TRANSACTIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in entries[:limit]:
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "tx_id": record.get("tx_id"),
            "committed_at": record.get("committed_at"),
            "status": record.get("status", "committed"),
            "files": list((record.get("files") or {}).keys()),
        })
    return out


def revert(tx_id: str) -> Dict[str, Any]:
    path = TRANSACTIONS_DIR / f"{tx_id}.json"
    if not path.exists():
        return {"ok": False, "error": "Unknown or already-pruned transaction"}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}
    if record.get("status") == "reverted":
        return {"ok": False, "error": "Transaction already reverted"}

    files = record.get("files") or {}
    staged: List[Dict[str, Any]] = []
    try:
        with file_lock:
            for rel, snapshot in files.items():
                target = _safe_target(rel)
                old_content = snapshot.get("old_content")
                if old_content is None:
                    staged.append({"path": rel, "target": target, "delete": True})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".evo_tx_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(old_content)
                staged.append({"path": rel, "target": target, "tmp": tmp_name, "delete": False})

            for item in staged:
                if item["delete"]:
                    if item["target"].exists():
                        item["target"].unlink()
                else:
                    os.replace(item["tmp"], str(item["target"]))
    except Exception as exc:
        for item in staged:
            tmp = item.get("tmp")
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return {"ok": False, "error": str(exc)}

    for item in staged:
        if item["delete"]:
            vector_index.remove_file(item["path"])
        else:
            vector_index.index_file(item["target"], item["path"])

    record["status"] = "reverted"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {"ok": True, "reverted_files": list(files.keys())}
