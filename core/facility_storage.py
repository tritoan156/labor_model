"""Facility crew config persistence — load from JSON, save via GitHub API.

The JSON file `data/facility_crew.json` holds HC/Conc/Crew per station for
each facility. Saves go through ``catalog_storage`` like every other storage
module — the same SHA-locked fetch → merge → PUT, retried once on
``GitHubConflict`` — so they land on whichever branch
``catalog_storage.GITHUB_BRANCH`` resolved from ``st.secrets["github_branch"]``
(``main`` for the live app, ``staging`` for the staging app). This module
used to hardcode ``main``, which sent the staging app's crew saves into the
live data file.

Requires a GitHub Personal Access Token stored in Streamlit Secrets
(`st.secrets["github_token"]`) for the save path. Reads work without a token.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .catalog_storage import (
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
    GitHubConflict,
)
from .constants import STATION_DEFAULTS, SUPPORT_ROLES

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FACILITY_CREW_PATH = DATA_DIR / "facility_crew.json"
GITHUB_FILE_PATH = "data/facility_crew.json"


def _default_facility_config() -> dict:
    """Fallback: build a facility config from STATION_DEFAULTS constants."""
    cfg = {}
    for st_name, (hc, conc, crew) in STATION_DEFAULTS.items():
        cfg[st_name] = {"HC": hc, "Conc": conc, "Crew": crew}
    return cfg


def load_all_facility_configs() -> dict:
    """Return dict of {facility_name: {station: {HC, Conc, Crew}}}.

    Falls back to STATION_DEFAULTS-based config if the JSON is missing.
    """
    if FACILITY_CREW_PATH.exists():
        with open(FACILITY_CREW_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_facility_crew_df(facility: str) -> pd.DataFrame:
    """Return a crew DataFrame (indexed by Station) for the given facility.

    Falls back to STATION_DEFAULTS if the facility has no saved config.
    Also merges in any STATION_DEFAULTS keys that are missing from the saved
    config — so adding a new station (e.g. Com Accessories) doesn't disappear
    from facilities whose JSON was saved before the station existed.

    Non-production SUPPORT_ROLES (Lead/Other) are always appended last (in fixed
    order). They carry HC only; their Conc/Crew default to 0 and are ignored by
    the capacity model.
    """
    all_cfg = load_all_facility_configs()
    facility_cfg = dict(all_cfg.get(facility) or _default_facility_config())

    # Backfill any stations that are in STATION_DEFAULTS but missing from the
    # saved config. Use a sensible default of (0, 0, default_crew) for new
    # stations — admin can edit later, but at least the row exists.
    for st_name, (hc, conc, crew) in STATION_DEFAULTS.items():
        if st_name not in facility_cfg:
            facility_cfg[st_name] = {"HC": 0, "Conc": 0, "Crew": crew}
    # Backfill support roles (Lead/Other) so they appear for every facility.
    for role in SUPPORT_ROLES:
        if role not in facility_cfg:
            facility_cfg[role] = {"HC": 0, "Conc": 0, "Crew": 0}

    def _row(name: str) -> dict:
        v = facility_cfg[name]
        return {"Station": name, "HC": v["HC"], "Conc": v["Conc"], "Crew": v["Crew"]}

    ordered = []
    # 1) Stations in STATION_DEFAULTS order
    for st_name in STATION_DEFAULTS.keys():
        if st_name in facility_cfg:
            ordered.append(_row(st_name))
    # 2) Any saved stations not in STATION_DEFAULTS and not support roles (rare)
    for st_name in facility_cfg:
        if st_name not in STATION_DEFAULTS and st_name not in SUPPORT_ROLES:
            ordered.append(_row(st_name))
    # 3) Support roles last, in fixed SUPPORT_ROLES order
    for role in SUPPORT_ROLES:
        ordered.append(_row(role))

    return pd.DataFrame(ordered).set_index("Station")


def _fetch_existing_from_github(token: str) -> "tuple[dict, str | None]":
    """Pull current facility_crew.json from GitHub → (parsed_dict, sha).

    A true 404 (the file doesn't exist on this branch yet) seeds from the local
    copy, so a first save on a fresh branch carries every facility rather than
    only the one being saved. Any OTHER failure — the file exists but its
    content couldn't be read, or it isn't a JSON object — is RAISED instead of
    falling back to this container's deploy-time copy: pushing that would
    silently clobber crew edits other users made since our deploy.
    """
    content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
    if not content_bytes:
        if sha is not None:
            raise RuntimeError(
                f"{GITHUB_FILE_PATH} exists on GitHub but its contents couldn't "
                "be read just now — not overwriting it. Try again in a moment."
            )
        return load_all_facility_configs(), None
    parsed = json.loads(content_bytes.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{GITHUB_FILE_PATH} on GitHub is not a JSON object.")
    return parsed, sha


def _write_local(content: str) -> None:
    """Mirror the latest JSON to the local data/ folder so the same container
    sees the change immediately (without waiting for the GitHub redeploy)."""
    try:
        FACILITY_CREW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(FACILITY_CREW_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def save_facility_crew_to_github(facility: str, crew_df: pd.DataFrame, token: str) -> dict:
    """Update facility_crew.json on GitHub via the REST API.

    Args:
      facility: facility name (e.g. "Henderson")
      crew_df: DataFrame indexed by Station with HC/Conc/Crew columns
      token: GitHub Personal Access Token (Contents: write permission)

    Fetches the latest file from ``catalog_storage.GITHUB_BRANCH``, replaces
    this facility's block, and PUTs it back SHA-locked; a ``GitHubConflict``
    (someone else saved in between) is retried once with a fresh fetch, then
    re-raised. Mirrors the result locally. Returns the GitHub API response
    JSON. Raises on HTTP error.
    """
    new_facility_cfg = {
        st_name: {
            "HC": int(crew_df.loc[st_name, "HC"]),
            "Conc": int(crew_df.loc[st_name, "Conc"]),
            "Crew": int(crew_df.loc[st_name, "Crew"]),
        }
        for st_name in crew_df.index
    }

    for attempt in (1, 2):
        existing, sha = _fetch_existing_from_github(token)
        existing[facility] = new_facility_cfg
        new_content = json.dumps(existing, indent=2)
        try:
            response = save_catalog_to_github(
                new_content, GITHUB_FILE_PATH, token,
                message=f"Update {facility} crew config via app",
                sha=sha,
            )
            _write_local(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue
