"""Where the labor standards come from — provenance, the Excel-first flag,
and the in-app changes that haven't made it back to Excel yet.

``data/catalog_source.json`` holds:

* ``excel_source_of_truth`` — the team has decided the IE's workbook owns the
  labor minutes. The in-app editors still accept a minute edit (a planner
  often needs a number *now*), but every such edit is recorded as **pending**
  until a later import shows the workbook agrees.
* ``imports`` — per catalog, the last workbook import: file, sheet, when, how
  many cells and new SKUs it wrote.
* ``pending`` — per catalog, per SKU, per column: the value set in the app,
  the Excel value it replaced, and when. Reconciled by the import (see
  ``excel_io.reconcile_pending``): cleared once Excel carries the app value,
  kept — and the in-app value protected — while Excel still has the old one.

Routing, descriptions and the add-SKU / placeholder forms are never pending:
those are the app's own overlay, and the workbook never carries them.

Same persistence model as the other small stores (see process_flow_storage):
every write fetches the file fresh from GitHub, applies one change on top,
pushes with the SHA as a lock (retrying once on a conflict) and mirrors the
result locally. Writes compose: a pending record and a lock flip from two
sessions both land.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from .catalog_storage import (
    GitHubConflict,
    fetch_catalog_csv_from_github,
    save_catalog_to_github,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CATALOG_SOURCE_PATH = DATA_DIR / "catalog_source.json"
GITHUB_FILE_PATH = "data/catalog_source.json"

#: Keys an import / pending change may be recorded under — matches
#: ``excel_io.CATALOG_SPECS``.
CATALOG_KEYS = ("machine", "accessory")


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_num(value) -> Optional[float]:
    """A pending value as a float, or ``None`` for blank / unparseable (an
    override on a cell that was blank in Excel has no Excel value)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="minutes")


def _clean(data) -> dict:
    """Coerce whatever was on disk / in the repo into the known shape, dropping
    anything unrecognised so a hand-edited or truncated file can't crash a load."""
    out: dict = {"excel_source_of_truth": False, "imports": {}, "pending": {}}
    if not isinstance(data, dict):
        return out
    out["excel_source_of_truth"] = bool(data.get("excel_source_of_truth", False))

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

    pending = data.get("pending")
    if isinstance(pending, dict):
        for key in CATALOG_KEYS:
            by_sku = pending.get(key)
            if not isinstance(by_sku, dict):
                continue
            cleaned_skus: dict = {}
            for sku, by_col in by_sku.items():
                sku = str(sku).strip()
                if not sku or not isinstance(by_col, dict):
                    continue
                cleaned_cols: dict = {}
                for col, rec in by_col.items():
                    col = str(col).strip()
                    if not col or not isinstance(rec, dict):
                        continue
                    app_value = _as_num(rec.get("app_value"))
                    if app_value is None:
                        continue  # a pending change with no value is noise
                    cleaned_cols[col] = {
                        "app_value": app_value,
                        "excel_value": _as_num(rec.get("excel_value")),
                        "at": str(rec.get("at", "")).strip(),
                    }
                if cleaned_cols:
                    cleaned_skus[sku] = cleaned_cols
            if cleaned_skus:
                out["pending"][key] = cleaned_skus
    return out


def load_catalog_source() -> dict:
    """Read the local JSON. Missing or unparseable → the default (Excel not
    the source of truth, nothing imported, nothing pending)."""
    if not CATALOG_SOURCE_PATH.exists():
        return _clean(None)
    try:
        with open(CATALOG_SOURCE_PATH, "r", encoding="utf-8") as f:
            return _clean(json.load(f))
    except Exception:
        return _clean(None)


def is_excel_source_of_truth() -> bool:
    return bool(load_catalog_source().get("excel_source_of_truth", False))


def last_import(catalog: str) -> Optional[dict]:
    """The recorded import for ``catalog``, or ``None`` if there has never been one."""
    return load_catalog_source().get("imports", {}).get(catalog)


def pending_for(catalog: str) -> dict:
    """``{sku: {column: {app_value, excel_value, at}}}`` for ``catalog``."""
    return dict(load_catalog_source().get("pending", {}).get(catalog, {}))


def pending_count(catalog: str) -> int:
    return sum(len(cols) for cols in pending_for(catalog).values())


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


def _check_catalog(catalog: str) -> None:
    if catalog not in CATALOG_KEYS:
        raise ValueError(f"unknown catalog {catalog!r}; expected one of {CATALOG_KEYS}")


def record_import_to_github(
    catalog: str, file: str, sheet: str, cells: int, new_skus: int,
    token: str, at: Optional[str] = None,
) -> dict:
    """Note that ``catalog``'s labor standards were just imported from
    ``file`` › ``sheet``. ``at`` defaults to now (ISO, minute precision)."""
    _check_catalog(catalog)
    stamp = at or _now()

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


def set_excel_source_to_github(on: bool, token: str) -> dict:
    """Flip the team-wide 'Excel is the source of truth for labor minutes' flag."""
    def _mutate(state: dict) -> None:
        state["excel_source_of_truth"] = bool(on)

    verb = "Make Excel" if on else "Stop treating Excel as"
    return _push(_mutate, token, message=f"{verb} the source of truth for labor minutes")


def record_pending_to_github(
    catalog: str, changes: Iterable[tuple], token: str, at: Optional[str] = None,
) -> dict:
    """Record in-app minute edits that Excel doesn't have yet.

    ``changes`` is ``(sku, column, app_value, excel_value)`` tuples —
    ``excel_value`` being the catalog value the edit replaced (what Excel is
    presumed to still say). Re-editing a cell that's already pending keeps the
    *original* Excel value: the thing to reconcile against is what the
    workbook had before the first override, not the previous override.
    """
    _check_catalog(catalog)
    stamp = at or _now()
    rows = [(str(s).strip(), str(c).strip(), _as_num(v), _as_num(x))
            for s, c, v, x in changes]
    rows = [r for r in rows if r[0] and r[1] and r[2] is not None]
    if not rows:
        return {}

    def _mutate(state: dict) -> None:
        by_sku = state.setdefault("pending", {}).setdefault(catalog, {})
        for sku, col, app_value, excel_value in rows:
            prior = by_sku.get(sku, {}).get(col)
            by_sku.setdefault(sku, {})[col] = {
                "app_value": app_value,
                "excel_value": prior["excel_value"] if prior else excel_value,
                "at": stamp,
            }

    return _push(
        _mutate, token,
        message=f"Record {len(rows)} temporary {catalog} minute change(s) pending in Excel",
    )


def clear_pending_to_github(
    catalog: str, keys: Iterable[tuple], token: str, reason: str = "reconciled",
) -> dict:
    """Drop ``(sku, column)`` pending records — Excel caught up, or the planner
    chose Excel's value."""
    _check_catalog(catalog)
    wanted = {(str(s).strip(), str(c).strip()) for s, c in keys}
    wanted = {k for k in wanted if k[0] and k[1]}
    if not wanted:
        return {}

    def _mutate(state: dict) -> None:
        by_sku = state.get("pending", {}).get(catalog, {})
        for sku, col in wanted:
            cols = by_sku.get(sku)
            if cols and col in cols:
                del cols[col]
                if not cols:
                    del by_sku[sku]

    return _push(
        _mutate, token,
        message=f"Clear {len(wanted)} pending {catalog} minute change(s) ({reason})",
    )
