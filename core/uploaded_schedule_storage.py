"""Per-facility uploaded production schedules.

A planner uploads a real production CSV via the sidebar; this module lets
them save that exact CSV under a name so teammates (or themselves on
another day) can reload the same schedule without re-emailing the file.

Mirrors ``core/scenario_storage.py`` — same concurrency pattern, same
local-mirror strategy, same per-facility keying — but the stored payload
is the raw CSV text rather than a slim list of manual entries. Keeping the
CSV intact means future loader improvements (e.g. consuming new columns)
work retroactively on old saves.

Top-level schema (``data/uploaded_schedules.json`` on GitHub):

.. code-block:: json

    {
      "Henderson": {
        "May 2026 production plan": {
          "saved_at": "2026-05-21T10:30:00",
          "rows":     193,
          "csv_content": "LOCATION,FG SKU ID,...\\nHENDERSON,BOSS25-010,...\\n"
        }
      },
      "Spartanburg": {...},
      "Cypress":     {...}
    }

``rows`` is the CSV's parsed data-row count (:func:`count_csv_data_rows`).
Blocks saved before 2026-10 counted line breaks instead, so a quoted
multi-line cell inflated them — the app recounts from ``csv_content`` when it
lists a saved schedule rather than trusting the stored number.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .catalog_storage import (
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
    GitHubConflict,
)
from .constants import now_local

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SCHEDULES_PATH = DATA_DIR / "uploaded_schedules.json"
GITHUB_FILE_PATH = "data/uploaded_schedules.json"


def load_all_uploaded_schedules() -> dict:
    """Read uploaded_schedules.json from local disk. Returns ``{}`` if missing
    or unparseable.

    Streamlit Cloud deploys this file along with the rest of the repo, so the
    local copy is the source of truth between redeploys. Saves go to GitHub,
    triggering a redeploy that pulls the new file into the local FS — plus we
    mirror locally after each save so the saving user sees it immediately.
    """
    if not SCHEDULES_PATH.exists():
        return {}
    try:
        with open(SCHEDULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _saved_at_key(block) -> tuple:
    """Sort key that puts the most recently saved block first.

    ``saved_at`` is an ISO-8601 string with an explicit offset; parse it so a
    block saved under a different offset still lands in the right place, and
    fall back to the raw text when it doesn't parse. A block with no
    ``saved_at`` (or a malformed one) sorts after every dated block.
    """
    raw = block.get("saved_at") if isinstance(block, dict) else None
    if not raw:
        return (2, 0.0, "")
    try:
        return (0, -datetime.fromisoformat(str(raw)).timestamp(), "")
    except (TypeError, ValueError, OverflowError, OSError):
        return (1, 0.0, str(raw))


def names_newest_first(by_loc: dict) -> list[str]:
    """Schedule names of one facility's ``{name: block}`` dict, most recently
    saved first; ties (and blocks with no usable ``saved_at``) fall back to
    alphabetical order so the result is stable."""
    if not isinstance(by_loc, dict):
        return []
    return sorted(by_loc.keys(), key=lambda n: (_saved_at_key(by_loc[n]), str(n)))


def list_uploaded_schedules(location: str) -> list[str]:
    """Schedule names for the given facility, newest save first, or ``[]``
    if none."""
    by_loc = load_all_uploaded_schedules().get(location, {})
    return names_newest_first(by_loc)


def get_uploaded_schedule(location: str, name: str) -> Optional[dict]:
    """Return one schedule block ``{"saved_at", "rows", "csv_content"}`` or None."""
    return load_all_uploaded_schedules().get(location, {}).get(name)


def _fetch_existing_from_github(token: str) -> "tuple[dict, str | None]":
    """Pull the current uploaded_schedules.json from GitHub.

    A 404 (file doesn't exist yet) comes back as ({}, None) so the first save
    can proceed. Any OTHER failure — network error, HTTP 5xx, rate-limit,
    corrupt JSON — is RAISED, not swallowed. Aborting the save (the UI shows
    "Save failed — retry") is safe; falling back to THIS container's stale
    deploy-time copy and pushing it would silently clobber every saved
    schedule other users stored since our deploy.
    """
    content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
    if not content_bytes:
        if sha is not None:
            # File EXISTS (has a sha) but its content came back empty — a fetch
            # problem (e.g. a >1 MB file the blob API couldn't return), NOT an
            # empty file. Returning {} here and saving would WIPE every saved
            # schedule. Abort instead so the caller shows "try again".
            raise RuntimeError(
                f"{GITHUB_FILE_PATH} exists on GitHub but its contents couldn't "
                "be read just now — not overwriting it. Try again in a moment."
            )
        return {}, None  # genuine 404 → empty starting point for a first save
    parsed = json.loads(content_bytes.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{GITHUB_FILE_PATH} on GitHub is not a JSON object.")
    return parsed, sha


def _write_local(content: str) -> None:
    """Mirror the latest JSON to local disk so the saving user sees the new
    schedule immediately without waiting for the Streamlit Cloud redeploy."""
    try:
        SCHEDULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SCHEDULES_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        # Filesystem might be read-only on some hosts — GitHub still has
        # source of truth; other sessions just wait for the redeploy.
        pass


def count_csv_data_rows(csv_text: str) -> int:
    """Number of data rows in a CSV: parsed records after the header row.

    Parses with the ``csv`` module rather than counting line breaks, so a
    quoted cell that spans several lines (an Excel comment with a hard return)
    counts as the one row it is. Blank and whitespace-only lines are skipped,
    which is what ``pandas.read_csv`` does too — so this is the same total the
    loader starts from, and the "of N rows" on the sidebar pill and the
    picker's row count agree. ``0`` for empty text or a header-only file.
    """
    if not csv_text or not csv_text.strip():
        return 0
    records = 0
    for rec in csv.reader(io.StringIO(csv_text)):
        if not rec or (len(rec) == 1 and not rec[0].strip()):
            continue  # a blank line (csv.reader yields [] or [" "])
        records += 1
    return max(records - 1, 0)


# Older name, kept for anything that imported it.
_count_csv_data_rows = count_csv_data_rows


def save_uploaded_schedule_to_github(
    location: str, name: str, csv_text: str, token: str,
) -> dict:
    """Persist one named uploaded schedule (under ``location``) on GitHub.

    Retries once on ``GitHubConflict`` (the narrow race where two users push
    within the same second). Last writer wins for the same ``(location, name)``
    key — older saves of that exact name are overwritten.

    Side effect: mirrors the latest JSON content to the local data/ folder so
    the saving user sees the new entry immediately.

    Returns the GitHub commit payload.
    """
    if not name or not name.strip():
        raise ValueError("Schedule name cannot be empty.")
    name = name.strip()
    location = str(location).strip()
    if not csv_text or not csv_text.strip():
        raise ValueError("Cannot save an empty schedule.")

    new_block = {
        # Las Vegas wall-clock with explicit offset so "Last saved: ..." in
        # the Load dropdown matches the planner's clock.
        "saved_at": now_local().isoformat(timespec="seconds"),
        "rows": count_csv_data_rows(csv_text),
        "csv_content": csv_text,
    }

    for attempt in (1, 2):
        existing, sha = _fetch_existing_from_github(token)
        if not isinstance(existing.get(location), dict):
            existing[location] = {}
        existing[location][name] = new_block
        new_content = json.dumps(existing, indent=2)
        try:
            response = save_catalog_to_github(
                new_content, GITHUB_FILE_PATH, token,
                message=f"Save uploaded schedule '{name}' for {location} via app",
                sha=sha,
            )
            _write_local(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue


def delete_uploaded_schedule_on_github(
    location: str, name: str, token: str,
) -> dict:
    """Delete one named uploaded schedule for a facility. Retries once on conflict."""
    if not name or not name.strip():
        raise ValueError("Schedule name cannot be empty.")
    name = name.strip()
    location = str(location).strip()

    for attempt in (1, 2):
        existing, sha = _fetch_existing_from_github(token)
        block = existing.get(location, {})
        if name in block:
            del block[name]
            existing[location] = block
        new_content = json.dumps(existing, indent=2)
        try:
            response = save_catalog_to_github(
                new_content, GITHUB_FILE_PATH, token,
                message=f"Delete uploaded schedule '{name}' for {location} via app",
                sha=sha,
            )
            _write_local(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue
