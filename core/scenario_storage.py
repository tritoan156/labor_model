"""Per-facility build-plan scenarios.

A "scenario" is a named snapshot of the manual-entry table — a list of
(FG SKU, Accessory SKU, Qty) rows that a planner saves to come back to later.

Persists to ``data/scenarios.json`` on GitHub so every user / browser sees the
same list. Same concurrency pattern as the catalog saves: fetch latest,
merge our changes in, push with SHA — retry once on ``GitHubConflict``.

Top-level schema:
    {
      "Henderson": {
        "May 2026 push": {"saved_at": "ISO timestamp", "entries": [...]},
        ...
      },
      "Spartanburg": {...},
      "Cypress": {...}
    }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from .catalog_storage import (
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
    GitHubConflict,
)
from .constants import now_local

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SCENARIOS_PATH = DATA_DIR / "scenarios.json"
GITHUB_FILE_PATH = "data/scenarios.json"


def load_all_scenarios() -> dict:
    """Read scenarios.json from local disk. Returns {} if file missing or unparseable.

    Streamlit Cloud deploys this file along with the rest of the repo, so the
    local copy is the source of truth between redeploys. Saves go to GitHub,
    triggering a redeploy that pulls the new file into the local FS.
    """
    if not SCENARIOS_PATH.exists():
        return {}
    try:
        with open(SCENARIOS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_scenarios(location: str) -> list[str]:
    """Sorted scenario names for the given facility, or [] if none."""
    all_scenarios = load_all_scenarios()
    by_location = all_scenarios.get(location, {})
    if not isinstance(by_location, dict):
        return []
    return sorted(by_location.keys())


def get_scenario(location: str, name: str) -> Optional[dict]:
    """Return one scenario dict (`{"saved_at": ..., "entries": [...]}`)
    or None if it doesn't exist."""
    all_scenarios = load_all_scenarios()
    return all_scenarios.get(location, {}).get(name)


def _fetch_existing_from_github(token: str) -> "tuple[dict, str | None]":
    """Pull the current scenarios.json from GitHub and return (parsed_dict, sha).

    Falls back to the local file contents (or empty dict) on any error so the
    save still has *something* to merge against — better than nothing.
    """
    try:
        content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
        if not content_bytes:
            return {}, sha
        parsed = json.loads(content_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            return {}, sha
        return parsed, sha
    except Exception:
        # GitHub unreachable / invalid JSON — start from the local copy
        return load_all_scenarios(), None


def _write_local_scenarios(content: str) -> None:
    """Mirror the latest scenarios JSON to the local data/ folder so subsequent
    reads in the same Streamlit Cloud container see the change immediately —
    without waiting for the GitHub redeploy."""
    try:
        SCENARIOS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SCENARIOS_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        # Filesystem might be read-only on some hosts — that's OK; the file on
        # GitHub is still updated. Other sessions just wait for the redeploy.
        pass


def save_scenario_to_github(
    location: str, name: str, entries_df: pd.DataFrame, token: str,
) -> dict:
    """Persist one named scenario (under `location`) into scenarios.json on GitHub.

    Cleans empty rows out of `entries_df` before saving. Retries once on
    ``GitHubConflict`` (the narrow race where two users push within the same
    second). Last writer wins for the SAME (location, name) key.

    Side effect: mirrors the latest JSON content to the local data/ folder so
    the saving user sees the new scenario immediately (without waiting for the
    Streamlit Cloud redeploy).

    Returns the GitHub commit payload.
    """
    if not name or not name.strip():
        raise ValueError("Scenario name cannot be empty.")
    name = name.strip()
    location = str(location).strip()

    # Build clean entries: drop rows where SKU is blank or Qty <= 0
    if entries_df is None or entries_df.empty:
        clean_rows = []
    else:
        rows = []
        for _, r in entries_df.iterrows():
            fg = str(r.get("FG SKU", "") or "").strip()
            acc = str(r.get("Accessory SKU", "") or "").strip()
            try:
                qty = int(r.get("Qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            if not fg or qty <= 0:
                continue
            rows.append({"FG SKU": fg, "Accessory SKU": acc, "Qty": qty})
        clean_rows = rows

    new_block = {
        # Las Vegas wall-clock with explicit offset so "Last saved: ..." in
        # the Load dropdown matches the planner's clock.
        "saved_at": now_local().isoformat(timespec="seconds"),
        "entries": clean_rows,
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
                message=f"Save scenario '{name}' for {location} via app",
                sha=sha,
            )
            _write_local_scenarios(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue


def delete_scenario_on_github(
    location: str, name: str, token: str,
) -> dict:
    """Delete one named scenario for a facility. Retries once on conflict."""
    if not name or not name.strip():
        raise ValueError("Scenario name cannot be empty.")
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
                message=f"Delete scenario '{name}' for {location} via app",
                sha=sha,
            )
            _write_local_scenarios(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue
