"""Per-facility planning assumptions (workforce drags).

A small persisted blob of the planning knobs that should be *remembered* between
sessions for each facility — currently the workforce-availability assumptions:

    * ``new_hire_pct``           — share of crew that are new hires (0..1)
    * ``new_hire_productivity``  — new-hire pace vs a seasoned worker (0..1)
    * ``absenteeism``            — share of paid time not worked (0..1)
    * ``excluded_stations``      — list of station display names to skip when
                                   sizing buildable units (not a fraction)
    * ``support_minutes``        — minutes per unit of support work, one entry
                                   per ``SUPPORT_FUNCTIONS`` name (receiving,
                                   inventory counting …), charged to every unit
                                   at the Material Handling station
    * ``mh_folded_stations``     — station display names (Warehouse (Pick),
                                   Ship) whose work the Material Handling team
                                   does at this facility

Persists to ``data/facility_settings.json`` on GitHub so every user / browser
sees the same values. Same concurrency pattern as the catalog / scenario saves:
fetch latest, merge our changes in, push with SHA — retry once on
``GitHubConflict``. The local file is mirrored so the saving session sees the
change immediately.

Top-level schema:
    {
      "Henderson":   {"new_hire_pct": 0.4, "new_hire_productivity": 0.5, "absenteeism": 0.05},
      "Spartanburg": {...},
      "Cypress":     {...}
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
from .constants import (
    DEFAULT_EFFICIENCY_FACTOR, DEFAULT_SAFETY_FACTOR, MH_FOLDABLE_STATIONS,
    STATION_KEY_TO_DISPLAY, SUPPORT_FUNCTIONS,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SETTINGS_PATH = DATA_DIR / "facility_settings.json"
GITHUB_FILE_PATH = "data/facility_settings.json"

# Defaults applied when a facility has nothing saved yet. New-hire share and
# productivity default to "no new-hire drag"; absenteeism defaults to 5% — the
# planner's standing assumption. Efficiency & safety mirror the app's sidebar
# defaults so a facility with nothing saved seeds the same values it always
# showed. All six are fractions in [0, 1], so they ride the same _clamp01 path.
DEFAULT_SETTINGS = {
    "new_hire_pct": 0.0,
    "new_hire_productivity": 0.5,
    "absenteeism": 0.05,
    "overtime": 0.0,
    "efficiency": float(DEFAULT_EFFICIENCY_FACTOR),
    "safety": float(DEFAULT_SAFETY_FACTOR),
}
_KEYS = tuple(DEFAULT_SETTINGS.keys())


def _clamp01(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


# Non-numeric settings that live alongside the [0,1] float knobs above but
# aren't clamped. ``excluded_stations`` is the list of station display names the
# planner has chosen to skip when sizing buildable units (persisted so the
# exclusion sticks across sessions until the underlying model is fixed).
def _clean_excluded(value) -> list:
    """Coerce a saved ``excluded_stations`` value to a clean list of unique,
    non-empty station-name strings (first-seen order). Non-list / junk → []."""
    if not isinstance(value, (list, tuple)):
        return []
    out, seen = [], set()
    for v in value:
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none") or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


#: Upper bound for one support function's minutes per unit — a typo guard
#: (a whole shift per unit), not a planning limit.
SUPPORT_MINUTES_MAX = 480.0


def clean_support_minutes(value) -> dict:
    """Coerce a saved ``support_minutes`` value to ``{function: minutes}`` with
    one non-negative float per ``SUPPORT_FUNCTIONS`` name (missing → 0, values
    capped at ``SUPPORT_MINUTES_MAX``). Unknown keys and junk are dropped."""
    src = value if isinstance(value, dict) else {}
    out = {}
    for name in SUPPORT_FUNCTIONS:
        try:
            v = float(src.get(name, 0.0))
        except (TypeError, ValueError):
            v = 0.0
        if v != v or v < 0.0:  # NaN / negative
            v = 0.0
        out[name] = min(v, SUPPORT_MINUTES_MAX)
    return out


#: Display names of the stations Material Handling can take over, in order.
MH_FOLDABLE_DISPLAY = tuple(STATION_KEY_TO_DISPLAY[k] for k in MH_FOLDABLE_STATIONS)


def clean_mh_folded(value) -> list:
    """Coerce a saved ``mh_folded_stations`` value to the foldable station
    display names it names, in ``MH_FOLDABLE_DISPLAY`` order. Junk → []."""
    if not isinstance(value, (list, tuple)):
        return []
    named = {str(v).strip() for v in value if v is not None}
    return [d for d in MH_FOLDABLE_DISPLAY if d in named]


def mh_folded_station_keys(folded_display) -> tuple:
    """Station keys for a list of foldable display names (what
    ``expand_schedule(mh_folded_stations=)`` takes)."""
    names = set(clean_mh_folded(folded_display))
    return tuple(k for k in MH_FOLDABLE_STATIONS if STATION_KEY_TO_DISPLAY[k] in names)


def support_minutes_per_unit(settings: dict | None) -> float:
    """Total support minutes per unit from a facility's settings dict."""
    return float(sum(clean_support_minutes(
        (settings or {}).get("support_minutes")).values()))


def load_all_facility_settings() -> dict:
    """Read facility_settings.json from local disk. Returns {} if missing/bad."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_facility_settings(location: str) -> dict:
    """Return the saved assumptions for a facility, merged over DEFAULT_SETTINGS.

    Always returns a complete, clamped dict (never raises), so callers can use
    it directly to seed widgets.
    """
    block = load_all_facility_settings().get(location, {})
    if not isinstance(block, dict):
        block = {}
    out = dict(DEFAULT_SETTINGS)
    for k in _KEYS:
        if k in block:
            out[k] = _clamp01(block[k])
    # Non-clamped list field — station display names excluded from buildable sizing.
    out["excluded_stations"] = _clean_excluded(block.get("excluded_stations", []))
    out["support_minutes"] = clean_support_minutes(block.get("support_minutes"))
    out["mh_folded_stations"] = clean_mh_folded(block.get("mh_folded_stations"))
    return out


def _fetch_existing_from_github(token: str) -> "tuple[dict, str | None]":
    """Pull current facility_settings.json from GitHub → (parsed_dict, sha).

    A 404 (file doesn't exist yet) comes back as ({}, None) so the first save
    can proceed. Any OTHER failure — network error, HTTP 5xx, rate-limit,
    corrupt JSON — is RAISED, not swallowed. Aborting the save (the UI shows
    "Save failed — retry") is safe; falling back to THIS container's stale
    deploy-time copy and pushing it would silently clobber every other
    facility's saved assumptions since our deploy.
    """
    content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
    if not content_bytes:
        if sha is not None:
            # File EXISTS but content came back empty (fetch problem, not an
            # empty file). Returning {} and saving would WIPE every facility's
            # saved assumptions — abort instead.
            raise RuntimeError(
                f"{GITHUB_FILE_PATH} exists on GitHub but its contents couldn't "
                "be read just now — not overwriting it. Try again in a moment."
            )
        return {}, None  # genuine 404 → empty starting point for a first save
    parsed = json.loads(content_bytes.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{GITHUB_FILE_PATH} on GitHub is not a JSON object.")
    return parsed, sha


def _write_local_settings(content: str) -> None:
    """Mirror the latest JSON to the local data/ folder so the same container
    sees the change immediately (without waiting for the GitHub redeploy)."""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def save_facility_settings_to_github(
    location: str, settings: dict, token: str,
) -> dict:
    """Persist one facility's planning assumptions into facility_settings.json.

    Clamps each value to [0, 1]. Retries once on ``GitHubConflict``. Mirrors the
    result locally. Returns the GitHub commit payload.
    """
    location = str(location).strip()
    if not location:
        raise ValueError("Facility cannot be empty.")
    clean = {k: _clamp01((settings or {}).get(k, DEFAULT_SETTINGS[k])) for k in _KEYS}
    clean["excluded_stations"] = _clean_excluded((settings or {}).get("excluded_stations", []))

    for attempt in (1, 2):
        existing, sha = _fetch_existing_from_github(token)
        # A caller that doesn't send the Material Handling fields keeps the
        # saved ones rather than wiping them.
        prev = existing.get(location) if isinstance(existing.get(location), dict) else {}
        for key, clean_fn in (("support_minutes", clean_support_minutes),
                              ("mh_folded_stations", clean_mh_folded)):
            src = settings if key in (settings or {}) else prev
            clean[key] = clean_fn(src.get(key))
        existing[location] = clean
        new_content = json.dumps(existing, indent=2)
        try:
            response = save_catalog_to_github(
                new_content, GITHUB_FILE_PATH, token,
                message=f"Save planning assumptions for {location} via app",
                sha=sha,
            )
            _write_local_settings(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue
