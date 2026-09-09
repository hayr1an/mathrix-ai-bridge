"""The two processes: the localhost JSON API, and the worker that drives the app.

    python -m bridge.service              # API + worker (what start-assistant.sh runs)
    python -m bridge.service --no-worker
    python -m bridge.worker               # the worker on its own

The website never talks to the API directly from the browser: Next.js proxies it
server-side, so the bridge stays bound to 127.0.0.1 and nothing about it is
reachable from the page.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import config, core, jobs
from .core import Snapshot
from .macos import (
    AnswerNotFound, AppNotRunning, Driver, NotTrusted, SendFailed, Timeout, TreeAsleep,
)


# ── the worker ────────────────────────────────────────────────────────────────

_running = True


def _stop(_signum: Any, _frame: Any) -> None:
    global _running
    _running = False


def _log(message: str) -> None:
    print(f"[worker] {message}", flush=True)


class Worker:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.driver = Driver(cfg)
        self.poll = float(cfg.get("poll_interval", 1.0))

    # ── one job ──────────────────────────────────────────────────────────────

    def run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        thread = job.get("thread_title") or ""
        jobs.update(job_id, status="running", stage="opening", started_at=time.time(),
                    error=None)
        _log(f"{job_id}: opening {thread!r}")

        self.driver.open_thread(thread)

        # An in-flight turn from somewhere else - the user typing in the app, or
        # a previous job - has to finish before this prompt can be typed, or the
        # two answers interleave.
        if self.driver.is_generating():
            _log(f"{job_id}: waiting for the conversation to go idle first")
            self.driver.wait_for_idle(timeout=self.cfg.get("generation_timeout", 900))

        prompt = job.get("prompt") or ""
        context = job.get("context") or ""
        preamble = str(self.cfg.get("system_preamble") or "").strip()
        full = "\n\n".join(p for p in (preamble, prompt, context) if p.strip())

        jobs.update(job_id, stage="typing")
        # send_prompt does not return until the transcript has actually grown.
        mark = self.driver.send_prompt(full, thread)
        jobs.update(job_id, stage="generating")
        _log(f"{job_id}: generating")

        self._wait_with_questions(job_id, thread)

        jobs.update(job_id, stage="reading", partial="")
        answer = self.driver.read_answer(after=mark)
        jobs.update(job_id, status="done", stage="done", answer=answer,
                    question=None, options=None, finished_at=time.time())
        _log(f"{job_id}: done ({len(answer)} chars)")

    def _wait_with_questions(self, job_id: str, thread: str) -> None:
        """Wait out the turn, handing any question panel to the website.

        The app still reports itself as generating while a question sits on
        screen, so waiting alone would burn the whole timeout on a panel that
        only needs a click (DRIVERSPEC §3).
        """
        deadline = time.time() + float(self.cfg.get("generation_timeout", 900))
        last_partial = ""

        while time.time() < deadline:
            def on_tick(snap: Snapshot, _self=self) -> None:
                nonlocal last_partial
                text = core.partial_answer(snap)
                if text and text != last_partial:
                    last_partial = text
                    jobs.update(job_id, partial=text)

            snap = self.driver.wait_for_idle(
                timeout=max(1.0, deadline - time.time()),
                stop_when=lambda s: core.pending_question(s) is not None,
                on_tick=on_tick,
            )

            question = core.pending_question(snap)
            if question is None:
                return
            self._ask_website(job_id, question)
            jobs.update(job_id, status="running", stage="generating")

        raise Timeout(
            "Claude Desktop never finished this turn. The answer may still arrive in "
            "the app; the website stopped waiting."
        )

    def _ask_website(self, job_id: str, question: core.Question) -> None:
        """Publish the question, wait for the user, then click their answer."""
        jobs.update(job_id, status="needs_input", stage="needs_input",
                    question=question.text, options=list(question.options),
                    chosen=None, chosen_text=None, skipped=False)
        _log(f"{job_id}: asking - {question.text[:60]!r}")

        deadline = time.time() + float(self.cfg.get("generation_timeout", 900))
        while time.time() < deadline and _running:
            job = jobs.read(job_id) or {}
            if job.get("chosen"):
                self.driver.answer_question(choice=job["chosen"])
                return
            if job.get("chosen_text"):
                self.driver.answer_question(text=job["chosen_text"])
                return
            if job.get("skipped"):
                self.driver.answer_question(skip=True)
                return
            # The user may equally have clicked the option in the desktop app.
            if core.pending_question(self.driver.fresh()) is None:
                _log(f"{job_id}: the question was answered in the app")
                return
            time.sleep(self.poll)
        raise Timeout("Nobody answered Claude's question in time.")

    # ── the loop ─────────────────────────────────────────────────────────────

    def loop(self) -> int:
        stale = jobs.reset_stale()
        if stale:
            _log(f"failed {stale} job(s) left behind by a previous worker")
        _log("ready - waiting for messages from the website")

        idle_logged = False
        while _running:
            job = jobs.next_queued()
            if job is None:
                if not idle_logged:
                    idle_logged = True
                time.sleep(self.poll)
                continue
            idle_logged = False
            try:
                self.run_job(job)
            except (NotTrusted, AppNotRunning, TreeAsleep, SendFailed, AnswerNotFound,
                    Timeout, LookupError, ValueError) as exc:
                # Expected failures: the message carries the fix, so it goes
                # straight to the website.
                _log(f"{job['id']}: {type(exc).__name__}: {exc}")
                jobs.update(job["id"], status="error", stage="done", partial="",
                            error=str(exc), finished_at=time.time())
            except Exception as exc:  # noqa: BLE001 - one bad job must not end the worker
                _log(f"{job['id']}: unexpected {type(exc).__name__}: {exc}")
                jobs.update(job["id"], status="error", stage="done", partial="",
                            error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
        return 0


def run_worker() -> int:
    """The worker process: claim the lock, then drain the queue until stopped."""
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    config.ensure_dirs()

    if not jobs.acquire(config.LOCK_PATH):
        _log(f"another worker is already running (pid {jobs.holder(config.LOCK_PATH)}) - "
             "not starting a second one.")
        return 1
    try:
        return Worker(config.load()).loop()
    finally:
        jobs.release(config.LOCK_PATH)
        _log("stopped")


# ── the HTTP API ──────────────────────────────────────────────────────────────

app = FastAPI(title="mathrix-bridge", docs_url=None, redoc_url=None)
_worker: Optional[subprocess.Popen] = None

# One driver for the API process. Walking the tree takes a second or two and
# hits the same app the worker is driving, so the result is cached briefly and
# guarded - the website polls health, and an unthrottled poll would fight the
# worker for the app's accessibility tree.
_driver: Optional[Driver] = None
_driver_lock = threading.Lock()
_health_cache: tuple[float, dict[str, Any]] = (0.0, {})
HEALTH_TTL = 4.0


def driver() -> Driver:
    global _driver
    if _driver is None:
        _driver = Driver(config.load())
    return _driver


def worker_running() -> bool:
    """Trust the lock file, not our own child handle: the worker may have been
    started separately, or ours may have died."""
    return jobs.holder(config.LOCK_PATH) is not None


class NewMessage(BaseModel):
    prompt: str
    thread: Optional[str] = None
    context: str = ""


class ConfigPatch(BaseModel):
    thread_title: Optional[str] = None
    system_preamble: Optional[str] = None


class Answer(BaseModel):
    """Exactly one of these: `choice` clicks an option, `text` types into the
    panel's own box, `skip` dismisses it."""

    choice: Optional[str] = None
    text: Optional[str] = None
    skip: bool = False


@app.get("/api/health")
def health(fresh: bool = False) -> dict[str, Any]:
    """Is everything the chat depends on actually up?

    Cached, because this walks the live accessibility tree. The website polls it
    on an interval and one walk per poll would slow the app the worker is trying
    to type into.
    """
    global _health_cache
    age, cached = _health_cache
    if not fresh and cached and time.time() - age < HEALTH_TTL:
        return {**cached, "worker_running": worker_running(), "cached": True}

    with _driver_lock:
        info = driver().health()
    info["worker_running"] = worker_running()
    info["cached"] = False
    _health_cache = (time.time(), info)
    return info


@app.get("/api/chats")
def chats() -> dict[str, Any]:
    """Conversations the website can send to: what the sidebar shows, annotated
    with how many messages have gone through the bridge."""
    cfg = config.load()
    stats = {t["title"]: t for t in jobs.threads()}
    live = health().get("chats") or []

    seen: list[str] = []
    for title in list(live) + list(stats) + [cfg.get("thread_title") or ""]:
        if title and title not in seen:
            seen.append(title)

    # A duplicated title in the sidebar is unusable: the driver refuses to guess
    # which one is meant, so the website disables it with the reason showing.
    counts: dict[str, int] = {}
    for title in live:
        counts[title] = counts.get(title, 0) + 1

    return {
        "selected": cfg.get("thread_title") or "",
        "chats": [
            {
                "title": title,
                "in_sidebar": title in live,
                "duplicate": counts.get(title, 0) > 1,
                "messages": stats.get(title, {}).get("messages", 0),
                "pending": stats.get(title, {}).get("pending", 0),
                "last_at": stats.get(title, {}).get("last_at"),
            }
            for title in seen
        ],
    }


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    cfg = config.load()
    return {"thread_title": cfg.get("thread_title") or "",
            "system_preamble": cfg.get("system_preamble") or ""}


@app.post("/api/config")
def set_config(patch: ConfigPatch) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if patch.thread_title is not None:
        changes["thread_title"] = patch.thread_title.strip()
    if patch.system_preamble is not None:
        changes["system_preamble"] = patch.system_preamble
    if changes:
        try:
            config.save(changes)
        except config.ConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    global _driver
    _driver = None  # pick up new settings on the next call
    return get_config()


@app.get("/api/messages")
def list_messages(thread: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    return jobs.all_jobs(thread, limit=limit)


@app.post("/api/messages")
def post_message(msg: NewMessage) -> dict[str, Any]:
    prompt = (msg.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="The message is empty.")
    if len(prompt) > 20000:
        raise HTTPException(status_code=413, detail="That message is too long to send.")

    thread = (msg.thread or config.load().get("thread_title") or "").strip()
    if not thread:
        raise HTTPException(
            status_code=409,
            detail="No Claude Desktop conversation is selected. Pick one in the "
                   "assistant's settings first.",
        )
    if not worker_running():
        raise HTTPException(
            status_code=503,
            detail="The bridge worker is not running, so nothing would pick this "
                   "message up. Start it with: python -m bridge.service",
        )
    return jobs.create(prompt, thread, context=msg.context or "")


@app.get("/api/messages/{job_id}")
def get_message(job_id: str) -> dict[str, Any]:
    job = jobs.read(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such message.")
    return job


@app.post("/api/messages/{job_id}/answer")
def answer_message(job_id: str, answer: Answer) -> dict[str, Any]:
    """Pick one of the options Claude offered. The worker is polling for it.

    409 rather than 400 when the job is not waiting: the website polls, so by the
    time a click lands the question may already have been answered in the desktop
    app. That is a state conflict, not a malformed request.
    """
    try:
        return jobs.answer(job_id, choice=answer.choice, text=answer.text,
                           skip=answer.skip)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such message.")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _free(host: str, port: int) -> bool:
    """Checked before anything else starts.

    Binding is the last thing main() does, so without this a port clash prints
    the worker's pid and the URL - looking exactly like a successful start - and
    only then fails, having left a worker running against a server that never
    came up.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main() -> int:
    """The API process, which also starts the worker as a child."""
    global _worker
    config.ensure_dirs()
    cfg = config.load()
    host, port = str(cfg.get("host", "127.0.0.1")), int(cfg.get("port", 8765))

    if not _free(host, port):
        print(f"[server] {host}:{port} is already in use - most likely a bridge that "
              "is still running. Stop it, or change \"port\" in bridge/config.json.",
              file=sys.stderr, flush=True)
        return 1

    if "--no-worker" not in sys.argv:
        _worker = subprocess.Popen([sys.executable, "-m", "bridge.service", "--worker-only"],
                                   cwd=str(config.ROOT))
        print(f"[server] worker started (pid {_worker.pid})", flush=True)

    print(f"[server] http://{host}:{port}", flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        if _worker is not None and _worker.poll() is None:
            _worker.terminate()
            try:
                _worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _worker.kill()
    return 0


if __name__ == "__main__":
    sys.exit(run_worker() if "--worker-only" in sys.argv else main())
