"""Where the labor standards come from — import provenance and the Excel lock.

``data/catalog_source.json`` records, per catalog, the last workbook import
(which file and sheet, when, how many cells and new SKUs it wrote) and one
team-wide flag, ``labor_locked``, that makes the in-app catalog editors stop
accepting labor-minute edits once the standards are maintained in Excel.

Routing, descriptions and the add-SKU / placeholder forms are *not* covered by
the lock: those are the app's own overlay on the IE's standards, and the
workbook never carries them.

Same persistence model as the other small stores (see process_flow_storage):
every write fetches the file fresh from GitHub, applies one change on top,
pushes with the SHA as a lock (retrying once on a conflict) and mirrors the
result locally so this container sees it immediately. Writes compose: a
machine import recorded while someone else flips the lock keeps both.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .catalog_storage import (
    GitHubConflict,
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CATALOG_SOURCE_PATH = DATA_DIR / "catalog_source.json"
GITHUB_FILE_PATH = "data/catalog_source.json"

#: Keys an import may be recorded under — matches ``excel_io.CATALOG_SPECS``.
CATALOG_KEYS = ("machine", "accessory")

_IMPORT_FIELDS = ("file", "sheet", "at", "cells", "new_skus")


def _clean(data) -> dict:
    """Coerce whatever was on disk / in the repo into the known shape, dropping
    anything unrecognised so a hand-edited or truncated file can't crash a load."""
    out: dict = {"labor_locked": False, "imports": {}}
    if not isinstance(data, dict):
        return out
    out["labor_locked"] = bool(data.get("labor_locked", False))
    imports = data.get("imports")
    if isinstance(imports, dict):
        for key in CATALOG_KEYS:
            rec = imports.get(key)
            if not isinstance(rec, dict) or not str(rec.get("file", "")).strip():
                continue
            out["imports"][key] = {
                "file": str(rec.get("file", "")).strip(),
                "sheet": str(rec.get("sheet", "")).strip(),
                "at": str(rec.get("at", "")).strip(),
                "cells": _as_int(rec.get("cells")),
                "new_skus": _as_int(rec.get("new_skus")),
            }
    return out


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def load_catalog_source() -> dict:
    """Read the local JSON. Missing or unparseable → the unlocked default."""
    if not CATALOG_SOURCE_PATH.exists():
        return _clean(None)
    try:
        with open(CATALOG_SOURCE_PATH, "r", encoding="utf-8") as f:
            return _clean(json.load(f))
    except Exception:
        return _clean(None)


def is_labor_locked() -> bool:
    return bool(load_catalog_source().get("labor_locked", False))


def last_import(catalog: str) -> Optional[dict]:
    """The recorded import for ``catalog``, or ``None`` if there has never been one."""
    return load_catalog_source().get("imports", {}).get(catalog)


def _write_local(content: str) -> None:
    """Mirror the latest JSON to the local data/ folder so subsequent reads
    in this Streamlit Cloud container see the change immediately."""
    try:
        CATALOG_SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CATALOG_SOURCE_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _fetch_remote(token: str) -> "tuple[dict, Optional[str]]":
    try:
        content_bytes, sha = fetch_catalog_csv_from_github(GITHUB_FILE_PATH, token)
        if not content_bytes:
            return _clean(None), sha
        return _clean(json.loads(content_bytes.decode("utf-8"))), sha
    except Exception:
        return _clean(None), None


def _push(mutate: Callable[[dict], None], token: str, message: str) -> dict:
    """Fetch fresh, apply ``mutate`` to that state, push SHA-locked, mirror.

    Applying the change on top of what's *currently* on GitHub — rather than
    on whatever this process last read — is what lets concurrent writes to
    different fields both land.
    """
    for attempt in (1, 2):
        current, sha = _fetch_remote(token)
        mutate(current)
        content = json.dumps(_clean(current), indent=2)
        try:
            response = save_catalog_to_github(
                content, GITHUB_FILE_PATH, token, message=message, sha=sha,
            )
            _write_local(content)
            return response
        except GitHubConflict:
            if attempt == 2:
                raise
            continue
    return {}  # unreachable — the loop returns or raises


def record_import_to_github(
    catalog: str, file: str, sheet: str, cells: int, new_skus: int,
    token: str, at: Optional[str] = None,
) -> dict:
    """Note that ``catalog``'s labor standards were just imported from
    ``file`` › ``sheet``. ``at`` defaults to now (ISO, minute precision)."""
    if catalog not in CATALOG_KEYS:
        raise ValueError(f"unknown catalog {catalog!r}; expected one of {CATALOG_KEYS}")
    stamp = at or datetime.now().isoformat(timespec="minutes")

    def _mutate(state: dict) -> None:
        state.setdefault("imports", {})[catalog] = {
            "file": str(file or "").strip(),
            "sheet": str(sheet or "").strip(),
            "at": stamp,
            "cells": _as_int(cells),
            "new_skus": _as_int(new_skus),
        }

    return _push(
        _mutate, token,
        message=f"Record {catalog} catalog import from Excel ({_as_int(cells)} cells, "
                f"{_as_int(new_skus)} new SKU(s))",
    )


def set_labor_locked_to_github(locked: bool, token: str) -> dict:
    """Flip the team-wide lock on labor-minute editing in the app."""
    def _mutate(state: dict) -> None:
        state["labor_locked"] = bool(locked)

    verb = "Lock" if locked else "Unlock"
    return _push(_mutate, token, message=f"{verb} in-app labor-minute editing (standards in Excel)")
