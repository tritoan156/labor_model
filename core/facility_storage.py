"""Facility crew config persistence — load from JSON, save via GitHub API.

The JSON file `data/facility_crew.json` holds HC/Conc/Crew per station for
each facility. Saves go directly to GitHub via the REST API so all users
see updates after Streamlit Cloud redeploys.

Requires a GitHub Personal Access Token stored in Streamlit Secrets
(`st.secrets["github_token"]`) for the save path. Reads work without a token.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pandas as pd
import requests

from .constants import STATION_DEFAULTS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FACILITY_CREW_PATH = DATA_DIR / "facility_crew.json"

GITHUB_REPO = "tritoan156/labor_model"
GITHUB_FILE_PATH = "data/facility_crew.json"
GITHUB_BRANCH = "main"


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
    """
    all_cfg = load_all_facility_configs()
    facility_cfg = dict(all_cfg.get(facility) or _default_facility_config())

    # Backfill any stations that are in STATION_DEFAULTS but missing from the
    # saved config. Use a sensible default of (0, 0, default_crew) for new
    # stations — admin can edit later, but at least the row exists.
    for st_name, (hc, conc, crew) in STATION_DEFAULTS.items():
        if st_name not in facility_cfg:
            facility_cfg[st_name] = {"HC": 0, "Conc": 0, "Crew": crew}

    # Preserve the STATION_DEFAULTS ordering for display consistency
    ordered = []
    for st_name in STATION_DEFAULTS.keys():
        v = facility_cfg.get(st_name)
        if v is not None:
            ordered.append({
                "Station": st_name, "HC": v["HC"], "Conc": v["Conc"], "Crew": v["Crew"],
            })
    # Tack on any stations that were saved but aren't in STATION_DEFAULTS (rare)
    for st_name, v in facility_cfg.items():
        if st_name not in STATION_DEFAULTS:
            ordered.append({
                "Station": st_name, "HC": v["HC"], "Conc": v["Conc"], "Crew": v["Crew"],
            })

    return pd.DataFrame(ordered).set_index("Station")


def save_facility_crew_to_github(facility: str, crew_df: pd.DataFrame, token: str) -> dict:
    """Update facility_crew.json on GitHub via the REST API.

    Args:
      facility: facility name (e.g. "Henderson")
      crew_df: DataFrame indexed by Station with HC/Conc/Crew columns
      token: GitHub Personal Access Token (Contents: write permission)

    Returns the GitHub API response JSON. Raises on HTTP error.
    """
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    # Fetch the current file SHA (required to update)
    sha = None
    existing = {}
    r = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=15)
    if r.status_code == 200:
        sha = r.json().get("sha")
        try:
            existing = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
        except Exception:
            existing = load_all_facility_configs()
    elif r.status_code == 404:
        existing = load_all_facility_configs()
    else:
        r.raise_for_status()

    # Build the new content
    new_facility_cfg = {
        st_name: {
            "HC": int(crew_df.loc[st_name, "HC"]),
            "Conc": int(crew_df.loc[st_name, "Conc"]),
            "Crew": int(crew_df.loc[st_name, "Crew"]),
        }
        for st_name in crew_df.index
    }
    existing[facility] = new_facility_cfg
    new_content = json.dumps(existing, indent=2)

    # Commit via PUT
    payload = {
        "message": f"Update {facility} crew config via app",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    put_r = requests.put(api_url, headers=headers, json=payload, timeout=15)
    put_r.raise_for_status()
    return put_r.json()
