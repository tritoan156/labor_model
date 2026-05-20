"""Editable process-flow graph — per-class From → To edges, persisted to GitHub.

The model uses these edges to draw the Process Flow diagram. The user can edit
them in-app and click Save; the JSON is pushed to GitHub via the same
SHA-locked pattern used by scenario_storage / catalog_storage.

Schema of `data/process_flow.json`:
    {
      "edges": [
        {"From": "Warehouse", "To": "Wire",    "Class": "All"},
        {"From": "Wire",      "To": "Battery", "Class": "STD"},
        ...
      ]
    }

Valid Class values: "All", "STD", "HT", "HS".
From / To must come from STATION_KEYS in core.constants.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import pandas as pd

from .catalog_storage import (
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
    GitHubConflict,
)
from .constants import STATION_KEYS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROCESS_FLOW_PATH = DATA_DIR / "process_flow.json"
GITHUB_FILE_PATH = "data/process_flow.json"

VALID_CLASSES = ("All", "STD", "HT", "HS")

# Conservative defaults — match the old hardcoded shape. The user will edit
# these to reflect the real manufacturing flow.
DEFAULT_EDGES: List[dict] = [
    {"From": "Warehouse", "To": "Wire",    "Class": "All"},
    {"From": "Warehouse", "To": "Battery", "Class": "All"},
    {"From": "Warehouse", "To": "PMAcc",   "Class": "All"},
    {"From": "Warehouse", "To": "GenAcc",  "Class": "All"},
    {"From": "Warehouse", "To": "ComAcc",  "Class": "All"},
    {"From": "Warehouse", "To": "AccKIT",  "Class": "All"},
    {"From": "Warehouse", "To": "Trailer", "Class": "All"},
    {"From": "Warehouse", "To": "ETO",     "Class": "All"},
    {"From": "Wire",      "To": "Final",   "Class": "All"},
    {"From": "Battery",   "To": "Final",   "Class": "All"},
    {"From": "PMAcc",     "To": "Final",   "Class": "All"},
    {"From": "GenAcc",    "To": "Final",   "Class": "All"},
    {"From": "ComAcc",    "To": "Final",   "Class": "All"},
    {"From": "AccKIT",    "To": "Final",   "Class": "All"},
    {"From": "Trailer",   "To": "Final",   "Class": "All"},
    {"From": "ETO",       "To": "Final",   "Class": "All"},
    {"From": "Final",     "To": "PDI",     "Class": "All"},
    {"From": "PDI",       "To": "QC",      "Class": "All"},
    {"From": "QC",        "To": "Ship",    "Class": "All"},
]


def load_process_flow() -> List[dict]:
    """Read the edges list from the local JSON. Returns DEFAULT_EDGES if the
    file is missing or unparseable."""
    if not PROCESS_FLOW_PATH.exists():
        return list(DEFAULT_EDGES)
    try:
        with open(PROCESS_FLOW_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return list(DEFAULT_EDGES)
    edges = data.get("edges") if isinstance(data, dict) else None
    if not isinstance(edges, list):
        return list(DEFAULT_EDGES)
    # Sanitize each row
    cleaned = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        f = str(e.get("From", "")).strip()
        t = str(e.get("To", "")).strip()
        c = str(e.get("Class", "")).strip() or "All"
        if not f or not t:
            continue
        cleaned.append({"From": f, "To": t, "Class": c})
    return cleaned or list(DEFAULT_EDGES)


def _clean_edges_from_df(edges_df: pd.DataFrame) -> List[dict]:
    """Convert a user-edited DataFrame into a validated, deduped edge list.

    Drops rows with empty From/To, invalid station names, invalid classes,
    or duplicates. Returns an empty list if the input is empty.
    """
    if edges_df is None or edges_df.empty:
        return []
    seen = set()
    out = []
    valid_stations = set(STATION_KEYS)
    for _, row in edges_df.iterrows():
        f = str(row.get("From", "") or "").strip()
        t = str(row.get("To", "") or "").strip()
        c = str(row.get("Class", "") or "").strip() or "All"
        if not f or not t:
            continue
        if f not in valid_stations or t not in valid_stations:
            continue  # silently drop unknown stations
        if c not in VALID_CLASSES:
            continue
        key = (f, t, c)
        if key in seen:
            continue
        seen.add(key)
        out.append({"From": f, "To": t, "Class": c})
    return out


def _write_local(content: str) -> None:
    """Mirror the latest JSON to the local data/ folder so subsequent reads
    in this Streamlit Cloud container see the change immediately."""
    try:
        PROCESS_FLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROCESS_FLOW_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _fetch_remote_json(token: str) -> "tuple[dict, Optional[str]]":
    try:
        content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
        if not content_bytes:
            return {}, sha
        parsed = json.loads(content_bytes.decode("utf-8"))
        return (parsed if isinstance(parsed, dict) else {}), sha
    except Exception:
        return {}, None


def save_process_flow_to_github(
    edges_df: pd.DataFrame, token: str,
) -> dict:
    """Validate, push to GitHub, mirror locally. Retries once on
    ``GitHubConflict``. Last writer wins for the whole edges list (we always
    replace the entire list — no per-edge merge)."""
    cleaned = _clean_edges_from_df(edges_df)

    for attempt in (1, 2):
        _existing, sha = _fetch_remote_json(token)
        # Replace the whole edges list — process_flow.json is small and
        # users authoring this configure the entire flow together.
        new_payload = {"edges": cleaned}
        new_content = json.dumps(new_payload, indent=2)
        try:
            response = save_catalog_to_github(
                new_content, GITHUB_FILE_PATH, token,
                message=f"Update process flow ({len(cleaned)} edges)",
                sha=sha,
            )
            _write_local(new_content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue


def reset_process_flow_to_default(token: str) -> dict:
    """Overwrite process_flow.json with DEFAULT_EDGES."""
    df = pd.DataFrame(DEFAULT_EDGES, columns=["From", "To", "Class"])
    return save_process_flow_to_github(df, token)
