"""The job queue: one JSON file per message, plus the lock that guards it.

Two processes share this - the web server writes new jobs and reads status, the
worker claims them and writes results - so every write goes to a temp file that
is then moved into place. A reader therefore sees either the old file or the new
one, never half of either.

The needs_input handshake goes through the same files rather than a second
channel: the server records the user's choice on the job, and the worker (which
is already polling it) picks it up. One place both sides already agree on.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import JOBS_DIR


# ── the queue ─────────────────────────────────────────────────────────────────

# status: queued -> running -> done | error
#                      \-> needs_input -> running -> ...
# stage is the finer sub-state while running - queued, opening, typing,
# generating, needs_input, reading, done. The website owns the wording for both.
LIVE = ("queued", "running", "needs_input")


def _now() -> float:
    return time.time()


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _write(job: dict[str, Any]) -> dict[str, Any]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(job["id"])
    # The pid keeps two writers from colliding on one temp name.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return job


def create(prompt: str, thread_title: str, *, context: str = "") -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex[:12],
        "thread_title": thread_title,
        "prompt": prompt,
        # What the website knew when the message was sent (filters, selection).
        # Kept separate from the prompt so the transcript shows what the user
        # actually typed.
        "context": context,
        "status": "queued",
        "stage": "queued",
        "answer": None,
        "partial": "",
        "error": None,
        "question": None,
        "options": None,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
    }
    return _write(job)


def read(job_id: str) -> Optional[dict[str, Any]]:
    path = _path(job_id)
    # A concurrent replace can briefly make the read fail; a retry costs nothing.
    for _ in range(4):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return None


def update(job_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    job = read(job_id)
    if job is None:
        return None
    job.update(fields)
    return _write(job)


def all_jobs(thread_title: Optional[str] = None, *, limit: int = 0) -> list[dict[str, Any]]:
    """Every job, oldest first; optionally just one conversation's."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in JOBS_DIR.glob("*.json"):
        job = read(path.stem)
        if job and (thread_title is None or job.get("thread_title") == thread_title):
            out.append(job)
    out.sort(key=lambda j: j.get("created_at", 0))
    return out[-limit:] if limit else out


def next_queued() -> Optional[dict[str, Any]]:
    """The oldest waiting job, in any conversation.

    One at a time regardless of chat: the worker types into whichever
    conversation is on screen, so running two would interleave them.
    """
    for job in all_jobs():
        if job.get("status") == "queued":
            return job
    return None


def threads() -> list[dict[str, Any]]:
    """One entry per conversation that has messages, most recent first."""
    stats: dict[str, dict[str, Any]] = {}
    for job in all_jobs():
        title = job.get("thread_title") or ""
        if not title:
            continue
        entry = stats.setdefault(title, {"title": title, "messages": 0,
                                         "last_at": 0.0, "pending": 0})
        entry["messages"] += 1
        entry["last_at"] = max(entry["last_at"], job.get("created_at") or 0)
        if job.get("status") in LIVE:
            entry["pending"] += 1
    return sorted(stats.values(), key=lambda e: e["last_at"], reverse=True)


def answer(job_id: str, *, choice: Optional[str] = None, text: Optional[str] = None,
           skip: bool = False) -> dict[str, Any]:
    """Record how the user answered a question. The worker polls for these.

    An option has to be one actually on offer: the label is what gets clicked in
    the desktop app, so an unknown one would either miss or hit another control.
    """
    if sum([choice is not None, text is not None, bool(skip)]) != 1:
        raise ValueError("answer with exactly one of: choice, text, skip")

    job = read(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.get("status") != "needs_input":
        raise ValueError(
            f"This message is {job.get('status')!r}, not waiting for an answer."
        )
    if choice is not None:
        if choice not in (job.get("options") or []):
            raise ValueError(f"{choice!r} is not one of the options offered.")
        return update(job_id, chosen=choice)
    if text is not None:
        if not text.strip():
            raise ValueError("The answer is empty.")
        return update(job_id, chosen_text=text.strip())
    return update(job_id, skipped=True)


def reset_stale() -> int:
    """Fail anything left mid-flight by a worker that died.

    needs_input included: the panel may still be on screen, but the worker that
    would have clicked it is gone, so nothing will ever pick the answer up.
    """
    count = 0
    for job in all_jobs():
        if job.get("status") in ("running", "needs_input"):
            update(job["id"], status="error", stage="done",
                   error="The bridge worker stopped while this message was running.",
                   finished_at=_now())
            count += 1
    return count


# ── the worker lock ───────────────────────────────────────────────────────────

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but it exists
    return True


def holder(path: Path) -> Optional[int]:
    """The pid holding the lock, if one still is."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = int(data.get("pid", 0))
    return pid if pid and _alive(pid) else None


def acquire(path: Path) -> bool:
    if holder(path) is not None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
    # Re-read: if two workers raced, the loser sees the winner's pid.
    return holder(path) == os.getpid()


def release(path: Path) -> None:
    if holder(path) == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass
