#!/usr/bin/env python3
"""
Evo IDE V5 - Streaming Session Management

V5 Changes:
- Added: Session metadata tracking
- Added: Progress reporting
- Added: Better cleanup on errors
- Added: Pause/resume support
"""
import queue
import time
from typing import Optional, Dict, Any

from . import state
from .state import active_streams, stream_lock
from .config import logger


def get_stream_id() -> str:
    with stream_lock:
        state.stream_counter += 1
        sid = f"stream_{state.stream_counter}"
        active_streams[sid] = {
            "stop": False,
            "pause": False,
            "approval_queue": queue.Queue(),
            "created_at": time.time(),
            "metadata": {},
            "progress": 0.0
        }
        logger.info(f"Created stream {sid}")
        return sid


def stop_stream(sid: str):
    if sid in active_streams:
        active_streams[sid]["stop"] = True
        logger.info(f"Stopped stream {sid}")


def pause_stream(sid: str):
    if sid in active_streams:
        active_streams[sid]["pause"] = True
        logger.info(f"Paused stream {sid}")


def resume_stream(sid: str):
    if sid in active_streams:
        active_streams[sid]["pause"] = False
        logger.info(f"Resumed stream {sid}")


def check_stop(sid: str) -> bool:
    return active_streams.get(sid, {}).get("stop", False)


def check_pause(sid: str) -> bool:
    return active_streams.get(sid, {}).get("pause", False)


def wait_for_approval(sid: str, timeout: int = 300) -> Optional[bool]:
    if sid not in active_streams:
        return None
    try:
        result = active_streams[sid]["approval_queue"].get(timeout=timeout)
        return result
    except queue.Empty:
        logger.warning(f"Approval timeout for stream {sid}")
        return None


def set_approval(sid: str, approved: bool):
    if sid in active_streams:
        active_streams[sid]["approval_queue"].put(approved)
        logger.info(f"Approval set for stream {sid}: {approved}")


def wait_for_transaction_decision(sid: str, tx_id: str, timeout: int = 600) -> Optional[Dict[str, Any]]:
    """Block the agent thread until the frontend posts a decision for tx_id,
    or the timeout elapses (treated by the caller as reject-all)."""
    if sid not in active_streams:
        return None
    tx_queues = active_streams[sid].setdefault("tx_queues", {})
    q = tx_queues.setdefault(tx_id, queue.Queue())
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        logger.warning(f"Transaction decision timeout for {sid}/{tx_id}")
        return None


def set_transaction_decision(sid: str, tx_id: str, decision: Dict[str, Any]):
    if sid not in active_streams:
        return
    tx_queues = active_streams[sid].setdefault("tx_queues", {})
    tx_queues.setdefault(tx_id, queue.Queue()).put(decision)
    logger.info(f"Transaction decision received for {sid}/{tx_id}: {decision.get('action')}")


def set_stream_metadata(sid: str, key: str, value: Any):
    if sid in active_streams:
        active_streams[sid]["metadata"][key] = value


def get_stream_metadata(sid: str, key: str, default: Any = None) -> Any:
    if sid in active_streams:
        return active_streams[sid]["metadata"].get(key, default)
    return default


def set_stream_progress(sid: str, progress: float):
    if sid in active_streams:
        active_streams[sid]["progress"] = max(0.0, min(1.0, progress))


def get_stream_progress(sid: str) -> float:
    return active_streams.get(sid, {}).get("progress", 0.0)


def cleanup_stream(sid: str):
    if sid in active_streams:
        # Clear the queue to prevent memory leaks
        try:
            while True:
                active_streams[sid]["approval_queue"].get_nowait()
        except queue.Empty:
            pass
        del active_streams[sid]
        logger.info(f"Cleaned up stream {sid}")


def get_active_stream_count() -> int:
    return len(active_streams)


def get_stream_info(sid: str) -> Optional[Dict[str, Any]]:
    if sid not in active_streams:
        return None
    s = active_streams[sid]
    return {
        "id": sid,
        "stop": s["stop"],
        "pause": s["pause"],
        "created_at": s["created_at"],
        "age_seconds": time.time() - s["created_at"],
        "progress": s["progress"],
        "metadata": s["metadata"]
    }
