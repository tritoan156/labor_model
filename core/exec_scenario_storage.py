"""Per-facility executive-summary scenarios.

An "executive scenario" is a *frozen snapshot* of the monthly labor summary for
one facility: the assumptions that produced it (so it can be reloaded and
edited) AND the computed metrics (so executives see exactly the numbers as of
the save, even if the catalog / crew change later).

Persists to ``data/exec_scenarios.json`` on GitHub so every user / browser sees
the same list. Same concurrency pattern as the other saves: fetch latest, merge
our changes in, push with SHA — retry once on ``GitHubConflict``.

Top-level schema:
    {
      "Cypress": {
        "Cypress May": {
          "saved_at": "ISO timestamp",
          "assumptions": {units, total_earned_hours, base_efficiency,
                          new_hire_pct, new_hire_productivity, absenteeism,
                          safety, shift_minutes, working_days, available_hc,
                          schedule_label},
          "metrics": { ...full executive_summary() output... }
        },
        ...
      },
      "Henderson": {...},
      "Spartanburg": {...}
    }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .catalog_storage import (
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
    GitHubConflict,
)
from .constants import now_local

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EXEC_SCENARIOS_PATH = DATA_DIR / "exec_scenarios.json"
GITHUB_FILE_PATH = "data/exec_scenarios.json"


def load_all_exec_scenarios() -> dict:
    """Read exec_scenarios.json from local disk. Returns {} if missing/unparseable."""
    if not EXEC_SCENARIOS_PATH.exists():
        return {}
    try:
        with open(EXEC_SCENARIOS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_exec_scenarios(location: str) -> list[str]:
    """Sorted executive-scenario names for the facility, or [] if none."""
    by_location = load_all_exec_scenarios().get(location, {})
    if not isinstance(by_location, dict):
        return []
    return sorted(by_location.keys())


def get_exec_scenario(location: str, name: str) -> Optional[dict]:
    """Return one scenario dict (`{saved_at, assumptions, metrics}`) or None."""
    return load_all_exec_scenarios().get(location, {}).get(name)


def _fetch_existing_from_github(token: str) -> "tuple[dict, str | None]":
    """Pull current exec_scenarios.json from GitHub → (parsed_dict, sha).

    Falls back to the local file on any error so the save still has something to
    merge against.
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
        return load_all_exec_scenarios(), None


def _write_local_exec_scenarios(content: str) -> None:
    """Mirror the latest JSON to the local data/ folder so the same container
    sees the change immediately (without waiting for the GitHub redeploy)."""
    try:
        EXEC_SCENARIOS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EXEC_SCENARIOS_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def save_exec_scenario_to_github(
    location: str, name: str, assumptions: dict, metrics: dict, token: str,
) -> dict:
    """Persist one named executive scenario (under `location`).

    Stores the assumptions AND the frozen metrics. Retries once on
    ``GitHubConflict``; last writer wins for the same (location, name). Mirrors
    locally. Returns the GitHub commit payload.
    """
    if not name or not name.strip():
        raise ValueError("Scenario name cannot be empty.")
    name = name.strip()
    location = str(location).strip()

    new_block = {
        "saved_at": now_local().isoformat(timespec="seconds"),
        "assumptions": dict(assumptions or {}),
        "metrics": dict(metrics or {}),
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
                message=f"Save executive scenario '{name}' for {location} via app",
                sha=sha,
            )
            _write_local_exec_scenarios(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue


def delete_exec_scenario_on_github(
    location: str, name: str, token: str,
) -> dict:
    """Delete one named executive scenario for a facility. Retries once."""
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
                message=f"Delete executive scenario '{name}' for {location} via app",
                sha=sha,
            )
            _write_local_exec_scenarios(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue
