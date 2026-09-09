"""Settings, and where things live on disk.

One JSON file, read fresh on every access so a change takes effect without a
restart - the timing values in particular are meant to be retuned against a real
machine (DRIVERSPEC §2: "settle_seconds ships at 2.5 and was not enough").
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
CONFIG_PATH = PKG / "config.json"
DATA_DIR = PKG / "data"
JOBS_DIR = DATA_DIR / "jobs"
CAPTURES_DIR = PKG / "captures"
LOCK_PATH = DATA_DIR / "worker.lock"

DEFAULTS: dict[str, Any] = {
    # The Claude Desktop conversation every prompt is typed into. It must match a
    # sidebar entry - open it once by hand so it appears under Recents.
    "thread_title": "",
    "bundle_id": "com.anthropic.claudefordesktop",

    "host": "127.0.0.1",
    "port": 8765,

    # How long one answer may take end to end. Long because a research-style
    # question in the desktop app genuinely runs for minutes.
    "generation_timeout": 900,
    # After typing, how long to wait for the transcript to actually grow before
    # calling the send failed.
    "send_confirm_timeout": 25,
    # Generation must read false continuously for this long before a turn counts
    # as finished - a single false reading between tokens is not the end.
    "idle_grace": 3.0,
    "poll_interval": 1.0,

    # Waking the tree (DRIVERSPEC §2).
    "settle_seconds": 2.5,
    "wake_timeout": 30.0,
    "stub_tree_nodes": 50,

    # The composer sits ~40 levels down; a shallow walk silently returns a
    # truncated tree and every lookup then reports "not found".
    "walk_depth": 80,
    "walk_node_budget": 20000,

    "focus_retries": 3,
    # Extra guidance prepended to every prompt sent from the website.
    "system_preamble": "",
}


class ConfigError(RuntimeError):
    pass


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{CONFIG_PATH.name} is not valid JSON: {exc}") from exc
        if not isinstance(stored, dict):
            raise ConfigError(f"{CONFIG_PATH.name} must contain a JSON object.")
        cfg.update(stored)
    return cfg


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the stored config. Written whole, via a temp file, so a
    crash mid-write cannot leave an unparseable config behind."""
    stored: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            stored = {}
    stored.update(patch)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return load()


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
