"""Anonymous usage tracking for the Labor Capacity Tool.

Quiet, low-overhead logger that records a few interesting events to
``data/usage_log.jsonl`` on GitHub (append-only) so the tool owner can answer
"who/how much/what gets used" without exposing tracking UI to end users.

Design notes
------------
- **Identity**: one UUID per Streamlit session, kept in ``st.session_state``.
  No PII — never IP, never user-agent, never names.
- **Buffer + flush**: events accumulate in ``st.session_state['analytics_buffer']``
  and are flushed to GitHub when ≥10 events queue up or when the caller
  piggybacks a flush on an existing save (free GitHub round-trip).
- **Storage**: ``data/usage_log.jsonl`` — newline-delimited JSON, one event
  per line. Append-only; the file is overwritten whole each flush (with the
  prior contents + the new lines) because the GitHub Contents API doesn't
  support real appends. Conflicts retry once via the same SHA-locked pattern
  used by ``scenario_storage``.
- **Local mirror**: after a successful push, the JSONL is written to the
  local ``data/`` folder so the admin view in the same session sees fresh
  data without waiting for the Streamlit Cloud redeploy.

Event schema (one line each):
    {"ts": "2026-05-21T13:42:10", "uid": "<short uuid>",
     "type": "session_start" | "save" | "schedule_load" | ...,
     "facility": "Henderson", "file": "machine_clean.csv", ...}

Public API:
    get_or_create_uid()           -> str
    track_event(event_type, **)   -> None
    flush_to_github(token)        -> int  (events flushed)
    load_usage_log()              -> list[dict]
    refresh_from_github(token)    -> list[dict]  (admin-only force pull)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import streamlit as st

from .catalog_storage import (
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
    GitHubConflict,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USAGE_LOG_PATH = DATA_DIR / "usage_log.jsonl"
GITHUB_FILE_PATH = "data/usage_log.jsonl"

# Buffer flush threshold — events accumulate locally until we hit this count,
# then we flush to GitHub. Save handlers also force a flush so each catalog
# save commits the analytics piggyback at the same time.
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
        "ts": datetime.now().isoformat(timespec="seconds"),
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


def _fetch_remote_log(token: str) -> "tuple[str, Optional[str]]":
    """Pull the current usage_log.jsonl bytes from GitHub.

    Returns ``(text, sha)``. Empty string + None on 404 or any error.
    """
    try:
        content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
        if not content_bytes:
            return "", sha
        return content_bytes.decode("utf-8"), sha
    except Exception:
        return "", None


def _write_local(content: str) -> None:
    """Mirror the latest JSONL to local disk so the admin view in the same
    Streamlit Cloud container sees fresh data without waiting for redeploy."""
    try:
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        # Filesystem might be read-only on some hosts — that's OK; GitHub
        # still has the source of truth.
        pass


def flush_to_github(token: Optional[str], force: bool = False) -> int:
    """Push buffered events to GitHub. Returns count of events flushed.

    - If ``token`` is None/empty → buffer is cleared (silent no-op) so long
      sessions without a token don't accumulate forever in memory.
    - If buffer is empty → returns 0.
    - If buffer size < ``FLUSH_THRESHOLD`` and ``force`` is False → returns 0.
    - Retries once on ``GitHubConflict``.
    """
    buffer: List[dict] = st.session_state.get(_BUFFER_KEY, [])
    if not buffer:
        return 0

    if not token:
        # No token → drop buffered events so memory doesn't grow unbounded.
        st.session_state[_BUFFER_KEY] = []
        return 0

    if not force and len(buffer) < FLUSH_THRESHOLD:
        return 0

    # Build new lines and try to push. We always fetch the latest remote
    # contents first and concatenate so we don't drop anyone else's events.
    new_lines = "\n".join(_serialize_event(e) for e in buffer)

    for attempt in (1, 2):
        existing_text, sha = _fetch_remote_log(token)
        if existing_text and not existing_text.endswith("\n"):
            combined = existing_text + "\n" + new_lines + "\n"
        else:
            combined = (existing_text or "") + new_lines + "\n"
        try:
            save_catalog_to_github(
                combined, GITHUB_FILE_PATH, token,
                message=f"Append {len(buffer)} usage event(s)",
                sha=sha,
            )
            _write_local(combined)
            count = len(buffer)
            st.session_state[_BUFFER_KEY] = []
            return count
        except GitHubConflict:
            if attempt == 2:
                # Give up cleanly — keep buffer for next attempt rather than
                # losing events.
                return 0
            continue
        except Exception:
            # Network / auth issue — keep the buffer so we can retry later.
            return 0

    return 0


def load_usage_log() -> List[dict]:
    """Read ``data/usage_log.jsonl`` from local disk and return parsed events.

    Streamlit Cloud syncs this file via redeploy, so the local copy is the
    most recent committed version. Combine with the in-session buffer in the
    admin view to show un-flushed activity too.
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


def refresh_from_github(token: Optional[str]) -> List[dict]:
    """Force a pull from GitHub, overwrite the local mirror, and return parsed events.

    Used by the admin view's ``🔄 Refresh from GitHub`` button so admins on
    a stale Streamlit container can pull fresh log content without waiting
    for the next redeploy.
    """
    if not token:
        return load_usage_log()
    text, _sha = _fetch_remote_log(token)
    if text:
        _write_local(text)
    return load_usage_log()


def get_buffer_snapshot() -> List[dict]:
    """Return a copy of the in-session buffer (un-flushed events).

    The admin view merges this with the on-disk log so the operator sees the
    current session's actions immediately, even if they haven't been pushed
    to GitHub yet.
    """
    return list(st.session_state.get(_BUFFER_KEY, []))
