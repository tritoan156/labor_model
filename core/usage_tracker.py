"""Anonymous usage tracking for the Labor Capacity Tool.

Quiet, low-overhead logger that records a few interesting events to
``data/usage_log.jsonl`` on the Streamlit Cloud container's own disk, so the
tool owner can answer "who/how much/what gets used" without exposing tracking
UI to end users.

Design notes
------------
- **Identity**: one UUID per Streamlit session, kept in ``st.session_state``.
  No PII — never IP, never user-agent, never names.
- **Buffer + flush**: events accumulate in ``st.session_state['analytics_buffer']``
  and are appended to the log when ≥ ``FLUSH_THRESHOLD`` events queue up or
  when a caller forces a flush (every save handler does, so each save lands
  in the log right away).
- **Storage**: ``data/usage_log.jsonl`` — newline-delimited JSON, one event
  per line, append-only. The file is **local to the container and
  git-ignored**: it is never pushed to GitHub, because the repo is public and
  the operational tempo it records (facility activity, save cadence) shouldn't
  be. Events survive across sessions within one deploy and reset on the next
  redeploy. On a fresh container the file doesn't exist yet; readers treat
  that as an empty log and the first flush creates it.

Event schema (one line each):
    {"ts": "2026-05-21T13:42:10-07:00", "uid": "<short uuid>",
     "type": "session_start" | "catalog_save" | "schedule_load" | ...,
     "facility": "Henderson", "file": "machine_clean.csv", ...}

Public API:
    get_or_create_uid()           -> str
    track_event(event_type, **)   -> None
    flush_buffer(force=False)     -> int  (events written)
    load_usage_log()              -> list[dict]
    get_buffer_snapshot()         -> list[dict]  (un-flushed events)
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import List

import streamlit as st

from .constants import now_local

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USAGE_LOG_PATH = DATA_DIR / "usage_log.jsonl"

# Buffer flush threshold — events accumulate in the session until we hit this
# count, then they're appended to the log. Save handlers force a flush so each
# save shows up in the admin view immediately.
FLUSH_THRESHOLD = 10

_BUFFER_KEY = "analytics_buffer"
_UID_KEY = "analytics_uid"


def get_or_create_uid() -> str:
    """Return the anonymous UUID for this Streamlit session.

    Generated on first call, persisted in ``st.session_state`` so every event
    in the same browser tab shares the same ID. Cleared automatically when
    the tab is closed (Streamlit drops session_state).
    """
    if _UID_KEY not in st.session_state:
        st.session_state[_UID_KEY] = uuid.uuid4().hex[:12]
    return st.session_state[_UID_KEY]


def track_event(event_type: str, **payload) -> None:
    """Queue one event into the session buffer.

    `payload` is a free-form dict that gets merged into the event line.
    Common fields: ``facility``, ``file``, ``mode``, ``name``. Missing /
    empty values are dropped so the JSONL stays lean.
    """
    if not event_type:
        return
    buffer = st.session_state.setdefault(_BUFFER_KEY, [])
    event = {
        # Las Vegas wall-clock with explicit offset (e.g. "-07:00") so the
        # admin dashboard can render times that match the planner's clock.
        "ts": now_local().isoformat(timespec="seconds"),
        "uid": get_or_create_uid(),
        "type": str(event_type),
    }
    for k, v in payload.items():
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        event[k] = s
    buffer.append(event)


def _serialize_event(event: dict) -> str:
    """One-line JSON encoding, no trailing newline."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def _write_local(content: str) -> None:
    """Write the whole JSONL to local disk, creating ``data/`` if needed.

    Errors propagate to :func:`flush_buffer`, which keeps the buffer for
    the next attempt (read-only filesystem, etc.).
    """
    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def flush_buffer(force: bool = False) -> int:
    """Append buffered events to the on-container usage log. Returns count
    written.

    Behavior:
    - If buffer is empty → returns 0.
    - If buffer size < ``FLUSH_THRESHOLD`` and ``force`` is False → returns 0.
    - On any filesystem error → keeps the buffer for the next attempt and
      returns 0.
    """
    buffer: List[dict] = st.session_state.get(_BUFFER_KEY, [])
    if not buffer:
        return 0

    if not force and len(buffer) < FLUSH_THRESHOLD:
        return 0

    new_lines = "\n".join(_serialize_event(e) for e in buffer)

    try:
        # Read whatever's already on disk and append our buffer to it. The
        # admin dashboard reads the same file, so the new events show up in
        # the next render. A missing file is simply an empty log.
        existing_text = ""
        if USAGE_LOG_PATH.exists():
            try:
                with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
                    existing_text = f.read()
            except Exception:
                existing_text = ""

        if existing_text and not existing_text.endswith("\n"):
            combined = existing_text + "\n" + new_lines + "\n"
        else:
            combined = (existing_text or "") + new_lines + "\n"

        _write_local(combined)
        count = len(buffer)
        st.session_state[_BUFFER_KEY] = []
        return count
    except Exception:
        # Filesystem might be read-only on some hosts — keep the buffer so
        # we can retry later (next save, end of session, etc.).
        return 0


def load_usage_log() -> List[dict]:
    """Read ``data/usage_log.jsonl`` from local disk and return parsed events.

    A missing file (fresh container, nothing flushed yet) is an empty log.
    Malformed lines are skipped. Combine with :func:`get_buffer_snapshot` in
    the admin view to show un-flushed activity too.
    """
    if not USAGE_LOG_PATH.exists():
        return []
    events: List[dict] = []
    try:
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    except Exception:
        return events
    return events


def get_buffer_snapshot() -> List[dict]:
    """Return a copy of the in-session buffer (un-flushed events).

    The admin view merges this with the on-disk log so the operator sees the
    current session's actions immediately, even if they haven't been written
    to disk yet.
    """
    return list(st.session_state.get(_BUFFER_KEY, []))
