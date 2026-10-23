"""Labor Capacity Tool (Henderson / Spartanburg / Cypress) — Streamlit web app.

Run with:   streamlit run app.py

The app reads CSV inputs from data/, lets the user adjust crew/safety/days
in the sidebar, and shows the dashboards as top-level tabs.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import importlib as _importlib
import inspect as _inspect
import sys as _sys

# --- Self-heal stale core.* modules on Streamlit Cloud "warm reboots" --------
# Streamlit Cloud sometimes reruns app.py while keeping the PREVIOUS deploy's
# core.* modules cached in sys.modules. Names/params added in this push then
# look "missing" — `from core.X import <newname>` raises ImportError, or a call
# passes an unknown kwarg (TypeError). This block MUST run BEFORE any
# `from core.X import <name>` below, so the named imports bind against freshly
# reloaded modules rather than the stale cached ones.
#
# Step 1: bare-import every core module we pull names from, so each is present
# in sys.modules and can be reloaded. A bare `import core.X` of a stale cached
# module is safe — it binds the cached object without checking for any specific
# name (only `from core.X import name` would fail on a missing name).
import core.constants
import core.catalog_storage
import core.facility_storage
import core.facility_settings_storage
import core.scenario_storage
import core.exec_scenario_storage
import core.uploaded_schedule_storage
import core.process_flow_storage
import core.usage_tracker
import core.data_validator
import core.data_loader
import core.labor_calculator
import core.excel_io
import core.catalog_source_storage
import core.sku_routing

# Step 2: detect staleness via sentinels for names/params added in recent pushes.
#
# MAINTENANCE: whenever you add a new name to a `core.*` module — or a new
# parameter that app.py passes by keyword — add it here, and prefer replacing
# the previous sentinel for that module over piling another one on (the newest
# addition implies every older one). Miss this and a warm Streamlit Cloud
# process pairs the NEW app.py with the OLD module: the reload is skipped
# because the stale sentinel still passes, and the first call using the new
# parameter dies with a bare `TypeError: unexpected keyword argument`.
_stale = (
    not hasattr(core.constants, "SUPPORT_ROLES")
    or not hasattr(core.labor_calculator, "executive_summary")
    or "overtime" not in _inspect.signature(
        core.labor_calculator.executive_summary).parameters
    or not hasattr(core.data_loader, "ScheduleColumnError")
    or not hasattr(core.data_loader, "build_placeholder_map")
    or not hasattr(core.data_loader, "_lookup_placeholder")
    # Catalog column aliases — excel_io imports these from data_loader, so a
    # stale data_loader would make the (reloaded) excel_io fail to import and
    # the old one would hand back sheets with no `.aliased`.
    or not hasattr(core.data_loader, "canonical_catalog_column")
    or not hasattr(core.excel_io, "_is_sku_header")
    or not hasattr(core.excel_io, "reconcile_pending")
    or "absorb_foreign_locations" not in _inspect.signature(
        core.data_loader.load_schedule).parameters
    or "efficiency" not in getattr(
        core.facility_settings_storage, "DEFAULT_SETTINGS", {})
    or "Undercarriage" not in getattr(core.constants, "STATION_KEYS", [])
    or not hasattr(core.usage_tracker, "flush_buffer")
    or not hasattr(core.excel_io, "read_catalog_workbook")
    # Routing seeds for new SKUs (also where _machine_family / _machine_class
    # now live) — app.py imports these names directly.
    or not hasattr(core.sku_routing, "seed_facility_routing")
    # Saved-schedule picker: parsed row count + newest-first ordering.
    or not hasattr(core.uploaded_schedule_storage, "names_newest_first")
    # Catalog CSV saves mirror the pushed text into data/ so this session uses
    # the new values at once — app.py imports the helper directly.
    or not hasattr(core.catalog_storage, "mirror_csv_locally")
    # An unreadable CSV upload is a ScheduleColumnError, not a crash.
    or not hasattr(core.data_loader, "_read_schedule_table")
)
if _stale:
    # Reload in DEPENDENCY ORDER: constants first (every other module imports
    # from it), then the storage/loader leaf modules, then labor_calculator
    # (imports constants). Reload only re-executes modules already in
    # sys.modules; on a clean deploy nothing is stale and this is skipped.
    for _modname in (
        "core.constants",
        "core.sku_routing",
        "core.catalog_storage",
        "core.facility_storage",
        "core.facility_settings_storage",
        "core.scenario_storage",
        "core.exec_scenario_storage",
        "core.uploaded_schedule_storage",
        "core.process_flow_storage",
        "core.usage_tracker",
        "core.data_validator",
        "core.data_loader",
        "core.labor_calculator",
        "core.excel_io",
        "core.catalog_source_storage",
    ):
        _mod = _sys.modules.get(_modname)
        if _mod is not None:
            try:
                _importlib.reload(_mod)
            except Exception:
                pass

# --- Now bind the specific names (pulled from the freshly reloaded modules) ---
from core.sku_routing import (
    # The family / class classifiers keep their app-side names; the routing
    # seeder keys on them, which is why they live next to it.
    KNOWN_FAMILIES,
    machine_family as _machine_family,
    machine_class as _machine_class,
    RoutingSeeder, seed_facility_routing, routing_seed_table,
    routing_columns_in,
)
from core.data_loader import (
    load_machine_labor, load_acc_labor, load_schedule, build_manual_schedule,
    build_placeholder_map,
    load_item_master, load_item_packages,
    resolve_item_time,
    ScheduleColumnError,
)
from core.excel_io import (
    upload_to_schedule_csv,
    build_workbook as build_excel_workbook,
    database_tables as database_excel_tables,
    database_readme as database_excel_readme,
    raw_github_url,
    power_query_m_for_csv,
    DATABASE_TABLES as EXCEL_DATABASE_TABLES,
    CSV_LINKABLE_FILES as EXCEL_CSV_LINKABLE_FILES,
    SAVED_SCHEDULE_ROWS_SHEET as EXCEL_SAVED_ROWS_SHEET,
    read_catalog_workbook,
    diff_catalog_sheet,
    reconcile_pending,
    editable_columns as catalog_editable_columns,
    CatalogImportError,
)
from core.labor_calculator import (
    expand_schedule, build_capacity_table,
    battery_demand_by_sku, battery_demand_by_type,
    compute_unit_labor, unit_labor_split,
    buildable_by_line,
    executive_summary, adjusted_efficiency,
)
from core.constants import (
    LOCATIONS, STATION_DEFAULTS, DEFAULT_SHIFT_MINUTES,
    DEFAULT_SAFETY_FACTOR, DEFAULT_EFFICIENCY_FACTOR,
    STATION_KEYS, SUPPORT_ROLES, CUSTOMER_SUFFIXES,
    now_local, today_local_str, week_of_month, week_of_month_series,
)
from core.facility_storage import (
    load_facility_crew_df, save_facility_crew_to_github,
)
from core.facility_settings_storage import (
    get_facility_settings, save_facility_settings_to_github,
)
from core.exec_scenario_storage import (
    list_exec_scenarios, get_exec_scenario,
    save_exec_scenario_to_github, delete_exec_scenario_on_github,
)
from core.catalog_storage import (
    save_catalog_to_github,
    fetch_catalog_csv_from_github,
    latest_catalog_sha,
    mirror_csv_locally,
    GitHubConflict,
)
from core.scenario_storage import (
    load_all_scenarios,
    get_scenario,
    save_scenario_to_github,
    delete_scenario_on_github,
)
from core.uploaded_schedule_storage import (
    load_all_uploaded_schedules,
    get_uploaded_schedule,
    save_uploaded_schedule_to_github,
    delete_uploaded_schedule_on_github,
    count_csv_data_rows,
    names_newest_first,
)
from core.process_flow_storage import (
    load_process_flow,
    save_process_flow_to_github,
    reset_process_flow_to_default,
    DEFAULT_EDGES as PROCESS_FLOW_DEFAULTS,
    VALID_CLASSES as PROCESS_FLOW_VALID_CLASSES,
)
from core.catalog_source_storage import (
    load_catalog_source,
    record_import_to_github,
    set_excel_source_to_github,
    record_pending_to_github,
    clear_pending_to_github,
)
from core.data_validator import validate_all
from core.usage_tracker import (
    track_event,
    flush_buffer as _flush_usage_buffer,
    load_usage_log,
    get_buffer_snapshot as _usage_buffer_snapshot,
    USAGE_LOG_PATH as _USAGE_LOG_PATH,
)

DATA_DIR = Path(__file__).resolve().parent / "data"


# =============================================================
# Page config & styling
# =============================================================
st.set_page_config(
    page_title="Labor Capacity Tool",
    page_icon="🏭",
    layout="wide",
)

# A few small style tweaks for a cleaner, more dashboard-like look.
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
      [data-testid="stMetricValue"] { font-size: 1.55rem; }
      [data-testid="stMetricLabel"] { font-size: 0.85rem; opacity: 0.9; }
      h1, h2, h3 { letter-spacing: -0.01em; }
      /* Tighter tab labels */
      .stTabs [data-baseweb="tab-list"] { gap: 6px; }
      .stTabs [data-baseweb="tab"] { padding-left: 14px; padding-right: 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# =============================================================
# Data loading (cached, with file-mtime invalidation)
# =============================================================
def _csv_mtime(filename: str) -> float:
    """Return the mtime of a CSV under data/, or 0 if missing.

    Including this value as a cache argument makes Streamlit invalidate the
    cached DataFrame whenever the CSV file is updated (e.g. via git push).
    """
    p = DATA_DIR / filename
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_data(show_spinner=False)
def _raw_catalog_csv_cached(file_path: str, mtime: float) -> pd.DataFrame:
    """The catalog CSV as it sits on disk — every cell as text, blanks kept —
    for the routing-seed previews. The loaders fill a blank routing cell with
    the station's default, which would make a never-set cell look like a
    deliberate choice; the seeder has to see the blank. Keyed on the file's
    mtime like the other loaders."""
    p = Path(file_path)
    if not p.is_absolute():
        p = DATA_DIR.parent / file_path
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str, keep_default_na=False)


# Cache version — bump this when the loader's OUTPUT SCHEMA changes (column
# renames, new columns, etc.) so the cache invalidates even if the underlying
# CSV file's mtime hasn't changed. The bump takes effect on the next script run
# (no Cloud reboot): every CSV loader below passes it as a hashed cache arg.
_LOADER_SCHEMA_VERSION = 10


def _fmt_min_hr(value, *, with_thousands: bool = True, hr_decimals: int = 1) -> str:
    """Format a person-minutes number as ``"<N> p-min · <H> p-hr"``.

    All labor in this app is computed in person-minutes (p-min), but managers
    think in person-hours. Render both inline so a planner reading "Total
    work: 103,366 p-min" also sees "1,722.8 p-hr" without doing the math.

    ``with_thousands`` adds the comma separator on p-min (off for very small
    values where it just adds visual noise). ``hr_decimals`` controls the
    p-hr precision — keep it at 1 for headline metrics, bump to 2 for
    per-unit values where 0.05 hr (3 min) matters.
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    # Guard NaN / Inf: float() above accepts them, but int(round(nan)) raises
    # ValueError and int(round(inf)) raises OverflowError. Treat non-finite as 0
    # so a malformed upstream aggregate degrades gracefully instead of crashing
    # the metric render. (math isn't imported in this module — use comparisons.)
    if v != v or v in (float("inf"), float("-inf")):
        v = 0.0
    mins = int(round(v))
    hrs = v / 60.0
    mins_str = f"{mins:,}" if with_thousands else f"{mins}"
    return f"{mins_str} p-min · {hrs:.{hr_decimals}f} p-hr"


def _split_crew_hc(crew_config) -> tuple[int, int, int]:
    """Split a crew_config's headcount into (production, support, total).

    Support roles (SUPPORT_ROLES = Lead/Other) are non-production: they count
    toward TOTAL headcount only, never the production figure used for the
    Required-HC gap / Executive-Summary verdict.
    """
    if crew_config is None or getattr(crew_config, "empty", True):
        return 0, 0, 0
    try:
        support_mask = crew_config.index.isin(SUPPORT_ROLES)
        support = int(crew_config.loc[support_mask, "HC"].sum())
        production = int(crew_config.loc[~support_mask, "HC"].sum())
    except Exception:
        production = int(crew_config["HC"].sum())
        support = 0
    return production, support, production + support


# How the loader caches are keyed — read before touching these signatures.
#
# st.cache_data builds its key from the arguments ACTUALLY PASSED to the call,
# and it skips any parameter whose name starts with "_". A parameter's default
# value is never part of the key either. So the earlier shape,
# ``def _load_x(_mtime, _schema_ver=_LOADER_SCHEMA_VERSION)``, was cached on
# nothing at all: a changed file on disk or a bumped schema version never
# invalidated a warm process, and only ``st.cache_data.clear()`` (after an
# in-app save) or a container restart refreshed the data.
#
# Hence the two-layer shape for the CSV loaders: the ``*_cached`` function
# hashes on the plain ``mtime`` + ``schema_ver`` it receives (no underscore, no
# default), and the public wrapper always passes the current constant so no
# call site can forget it. The JSON/JSONL loaders are keyed on ``mtime`` alone.
# ``tests/test_cache_keys.py`` guards both halves.
@st.cache_data(show_spinner=False)
def _load_machine_df_cached(mtime: float, schema_ver: int):
    return load_machine_labor()


def _load_machine_df(mtime: float):
    """Load machine_clean.csv, cached on (file mtime, _LOADER_SCHEMA_VERSION)."""
    return _load_machine_df_cached(mtime, _LOADER_SCHEMA_VERSION)


def _project_facility_routing(machine_df, location: str):
    """Project the per-facility routing columns into legacy column names.

    machine_clean.csv carries 15 routing columns (5 stations × 3 facilities):
    e.g. ``Final Station HND`` / ``Final Station SPB`` / ``Final Station CYP``.
    The labor calculator and capacity table still read the legacy names
    ``Final Station`` / ``AccKIT Station`` / ``PDI Station`` / ``Wire Station`` /
    ``Undercarriage Station``, so we copy the facility-matched column into those
    names here. Returns a NEW DataFrame
    (does not mutate the cached one) so the cache stays clean across
    location switches.
    """
    from core.constants import FACILITY_CODE
    code = FACILITY_CODE.get(location, "HND")
    out = machine_df.copy()
    for base, default in (("Final Station", "Final"),
                          ("AccKIT Station", "AccKIT"),
                          ("PDI Station", "PDI"),
                          ("Wire Station", "Wire"),
                          ("Undercarriage Station", "Undercarriage")):
        sfx = f"{base} {code}"
        if sfx in out.columns:
            out[base] = out[sfx]
        elif base not in out.columns:
            # Safety net for a stale cache: never crash compute if the
            # facility column is missing — fall back to the default station.
            out[base] = default
    return out


@st.cache_data(show_spinner=False)
def _load_acc_df_cached(mtime: float, schema_ver: int):
    return load_acc_labor()


def _load_acc_df(mtime: float):
    """Load acc_clean.csv, cached on (file mtime, _LOADER_SCHEMA_VERSION) so a
    schema bump refreshes the frame even when the file mtime is unchanged."""
    return _load_acc_df_cached(mtime, _LOADER_SCHEMA_VERSION)


@st.cache_data(show_spinner=False)
def _load_item_master_df_cached(mtime: float, schema_ver: int):
    return load_item_master()


def _load_item_master_df(mtime: float):
    """Load item_master.csv, cached on (file mtime, _LOADER_SCHEMA_VERSION)."""
    return _load_item_master_df_cached(mtime, _LOADER_SCHEMA_VERSION)


@st.cache_data(show_spinner=False)
def _load_item_packages_df_cached(mtime: float, schema_ver: int):
    return load_item_packages()


def _load_item_packages_df(mtime: float):
    """Load item_packages.csv, cached on (file mtime, _LOADER_SCHEMA_VERSION)."""
    return _load_item_packages_df_cached(mtime, _LOADER_SCHEMA_VERSION)


@st.cache_data(show_spinner=False)
def _load_scenarios_dict(mtime: float):
    """Load data/scenarios.json. Cached per file mtime (hashed — keep the
    parameter un-prefixed, see the cache-key note above)."""
    return load_all_scenarios()


@st.cache_data(show_spinner=False)
def _load_uploaded_schedules_dict(mtime: float):
    """Load data/uploaded_schedules.json. Cached per file mtime (hashed)."""
    return load_all_uploaded_schedules()


@st.cache_data(show_spinner=False)
def _load_process_flow_edges(mtime: float):
    """Load data/process_flow.json. Cached per file mtime (hashed)."""
    return load_process_flow()


@st.cache_data(show_spinner=False)
def _load_catalog_source_cached(mtime: float):
    """Load data/catalog_source.json — import provenance + the labor-minute
    lock. Cached per file mtime (hashed)."""
    return load_catalog_source()


def _catalog_source() -> dict:
    """The current provenance/lock state, via the mtime-keyed cache."""
    return _load_catalog_source_cached(_csv_mtime("catalog_source.json"))


def _excel_source() -> bool:
    """True when the team has made the IE's workbook the source of truth for
    labor minutes. The in-app editors still accept a minute edit — a planner
    often needs a number *now* — but each one is saved as **pending** until a
    later import shows the workbook agrees."""
    return bool(_catalog_source().get("excel_source_of_truth", False))


def _pending_for(catalog_key: str) -> dict:
    """``{sku: {column: {app_value, excel_value, at}}}`` — the temporary
    in-app minute edits for one catalog that Excel doesn't carry yet."""
    return dict((_catalog_source().get("pending") or {}).get(catalog_key) or {})


def _pending_frame(pending: dict) -> pd.DataFrame:
    rows = []
    for sku, by_col in pending.items():
        for col, rec in by_col.items():
            ev = rec.get("excel_value")
            rows.append({
                "SKU": sku, "Column": col,
                "Excel": "" if ev is None else f"{ev:g}",
                "In app": f"{rec.get('app_value', 0):g}",
                "Since": rec.get("at", ""),
            })
    return pd.DataFrame(rows, columns=["SKU", "Column", "Excel", "In app", "Since"])


def _render_pending_summary(catalog_key: str, pending: dict) -> None:
    """The '⏳ N temporary change(s) not yet in Excel' expander shown above a
    catalog editor and in the import view."""
    n = sum(len(c) for c in pending.values())
    if not n:
        return
    with st.expander(
        f"⏳ {n} temporary minute change(s) on {len(pending)} SKU(s) not yet in Excel",
        expanded=False,
    ):
        st.caption(
            "These were changed here and are in use now. Update the workbook and "
            "re-import to make them permanent — each one clears itself once Excel "
            "carries the same value. Until then an import keeps your value rather "
            "than reverting it."
        )
        st.dataframe(_pending_frame(pending), width="stretch", hide_index=True)


def _provenance_line(catalog_key: str) -> str:
    """One-line 'where these standards came from' for an editor caption, or
    "" if that catalog has never been imported from a workbook."""
    rec = (_catalog_source().get("imports") or {}).get(catalog_key)
    if not rec:
        return ""
    where = f"`{rec['file']}`" + (f" › `{rec['sheet']}`" if rec.get("sheet") else "")
    when = rec.get("at", "")
    bits = []
    if rec.get("cells"):
        bits.append(f"{rec['cells']} cell(s)")
    if rec.get("new_skus"):
        bits.append(f"{rec['new_skus']} new SKU(s)")
    tail = f" — {', '.join(bits)}" if bits else ""
    return f"📘 Labor standards last imported from {where} on {when}{tail}."


@st.cache_data(show_spinner=False, ttl=60)
def _load_usage_log_cached(mtime: float):
    """Load data/usage_log.jsonl. Cached per file mtime (hashed); TTL=60s so
    the admin view auto-refreshes within a minute of new events without
    polling too aggressively."""
    return load_usage_log()


# ---------------------------------------------------------------------------
# Targeted cache clears.
#
# Every save used to call st.cache_data.clear(), which also threw away the
# content-keyed analysis caches (schedule expansion, capacity table, weekly
# heatmap) that the save never touched — so the first click after any save
# paid for a full recompute. Now that the loaders above key on file mtime, a
# file this container rewrites (scenarios, saved schedules, process flow, the
# usage log — and, since `core.catalog_storage.mirror_csv_locally`, every
# catalog CSV a save pushes) invalidates on its own; the clears below are
# cheap insurance for that path, and the 30-second SHA lookup must be dropped
# so the stale-data banner sees the new commit at once instead of up to 30 s
# later.
# ---------------------------------------------------------------------------
def _catalog_loader_for(file_path: str):
    """The cached loader that reads ``file_path`` (any ``data/...`` spelling)."""
    return {
        "machine_clean.csv": _load_machine_df_cached,
        "acc_clean.csv": _load_acc_df_cached,
        "item_master.csv": _load_item_master_df_cached,
        "item_packages.csv": _load_item_packages_df_cached,
    }.get(Path(file_path).name)


def _clear_catalog_cache(*file_paths: str) -> None:
    """Drop the cached frame(s) for the catalog CSV(s) just pushed to GitHub
    and the cached SHA lookup behind the stale-data banner."""
    for fp in file_paths:
        loader = _catalog_loader_for(fp)
        if loader is not None:
            try:
                loader.clear()
            except Exception:
                pass
    try:
        _latest_sha_cached.clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cached analysis pipeline.
#
# Streamlit reruns the WHOLE script on every widget interaction, and the heavy
# pipeline (per-unit schedule expansion → capacity table) previously re-ran in
# full each time — including ~5-6× inside the weekly heatmap. These wrappers
# cache the two pure functions on the CONTENT of their inputs (Streamlit hashes
# DataFrames + scalars), so:
#   • a slider nudge that doesn't change the schedule → expand is a cache HIT;
#   • a tab click that changes no inputs → BOTH are hits (near-instant);
#   • only the inputs that actually changed force a recompute.
# Cache keys cover every input, so there is no staleness risk: edit the
# schedule / catalog / crew / a setting and the matching key changes.
# None of these parameters may be underscore-prefixed: the DataFrames are
# hashed by content on purpose (an "_df" param would be skipped and the cache
# would serve the first schedule it ever saw to every later one).
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, max_entries=32)
def _expand_schedule_cached(schedule_df, machine_df, acc_df):
    """Cached expand_schedule. expand_schedule attaches .attrs
    (skipped_fg / unknown_acc) that don't reliably survive st.cache_data's
    pickle round-trip, so return them separately; the wrapper reattaches."""
    units = expand_schedule(schedule_df, machine_df, acc_df)
    attrs = {
        "skipped_fg": list(units.attrs.get("skipped_fg", []) or []),
        "unknown_acc": list(units.attrs.get("unknown_acc", []) or []),
    }
    return units, attrs


def _expand_schedule(schedule_df, machine_df, acc_df):
    """Call the cached expansion and reattach the data-quality .attrs.

    st.cache_data returns a fresh copy on each access, so setting .attrs here
    never mutates the cached value."""
    units, attrs = _expand_schedule_cached(schedule_df, machine_df, acc_df)
    units.attrs["skipped_fg"] = attrs["skipped_fg"]
    units.attrs["unknown_acc"] = attrs["unknown_acc"]
    return units


def _missing_catalog_skus(schedule_df, machine_df, acc_df) -> "tuple[list[str], list[str]]":
    """FG SKUs and accessory SKUs the schedule references that the catalogs
    don't carry — ``(fg_missing, acc_missing)``, each sorted.

    Same membership tests as ``expand_schedule`` (``FG_BASE`` against the
    machine catalog's index, ``ACC`` against the accessory catalog's), but
    computed from the schedule itself, so the list — and the ➕ Import button
    it drives — is there even when every FG SKU is unknown and the unit table
    came back empty. Accessories on rows whose FG was dropped are included
    too (the unit table never saw those rows), which is what the planner
    wants to import. Returns two empty lists for an empty schedule.
    """
    if schedule_df is None or schedule_df.empty:
        return [], []
    machine_index = set(machine_df.index) if machine_df is not None else set()
    acc_index = set(acc_df.index) if acc_df is not None else set()
    fg_col = "FG_BASE" if "FG_BASE" in schedule_df.columns else "FG_RAW"
    fg_missing: set[str] = set()
    if fg_col in schedule_df.columns:
        for v in schedule_df[fg_col].dropna().astype(str):
            v = v.strip()
            if v and v not in machine_index:
                fg_missing.add(v)
    acc_missing: set[str] = set()
    if "ACC" in schedule_df.columns:
        for v in schedule_df["ACC"].dropna().astype(str):
            v = v.strip()
            if v and v.lower() != "nan" and v not in acc_index:
                acc_missing.add(v)
    return sorted(fg_missing), sorted(acc_missing)


@st.cache_data(show_spinner=False, max_entries=64)
def _build_capacity_table_cached(units, crew_config, shift_minutes, working_days,
                                 safety_factor, efficiency_factor,
                                 new_hire_pct, new_hire_productivity,
                                 absenteeism, overtime):
    """Cached build_capacity_table (output carries no .attrs, so it caches
    directly). Scalars are passed positionally so each distinct setting is its
    own cache key."""
    return build_capacity_table(
        units, crew_config, shift_minutes, working_days, safety_factor,
        efficiency_factor,
        new_hire_pct=new_hire_pct,
        new_hire_productivity=new_hire_productivity,
        absenteeism=absenteeism,
        overtime=overtime,
    )


def _github_token() -> "str | None":
    """The GitHub PAT from Streamlit Secrets, or None when there isn't one.

    Every save handler and the stale-data banner read the token through this
    rather than ``st.secrets`` directly. ``st.secrets.get`` doesn't return its
    default when no secrets file exists at all — it raises
    ``StreamlitSecretNotFoundError`` — so a local run without a
    ``.streamlit/secrets.toml`` used to crash the catalog editors and the
    Excel import preview instead of landing on the "GitHub token not
    configured" path. A blank token counts as missing.
    """
    try:
        if not hasattr(st, "secrets"):
            return None
        token = st.secrets.get("github_token", None)
    except Exception:
        return None
    return token or None


def _track_and_flush(event_type: str, *, force_flush: bool = True, **payload) -> None:
    """Convenience wrapper: queue an event, then append the buffer to the
    on-container usage log (``data/usage_log.jsonl``; never pushed to GitHub).

    Most callers use ``force_flush=True`` because they're already in a save
    context. The session_start and schedule_load callers also force-flush so
    the very first event in a session always lands in the log within seconds.
    """
    try:
        track_event(event_type, **payload)
        _flush_usage_buffer(force=force_flush)
    except Exception:
        # Tracking must never break the user's actual save flow.
        pass


# ---------------------------------------------------------------------------
# Deferred reruns for sidebar actions
# ---------------------------------------------------------------------------
# Streamlit drops the state of every widget a script run did NOT build, and a
# run that ends in ``st.rerun()`` counts as a finished run. A rerun raised by a
# sidebar button (📥 Load, 💾 Save, ➕ Add, the manual grid's renumber …)
# therefore ended the script before the Plan month, Time window, working-day
# count, sliders and crew editor below it were built — and all of them reset
# on the very next run. The worst case was "Working days in the period": it
# has no default of its own, so it fell to its minimum of 1 while the browser
# still displayed the old number, and every capacity figure came out ~20× too
# high until the planner happened to touch the month or the window.
#
# Sidebar handlers therefore never call ``st.rerun()`` directly: they ask for
# one here, and render_sidebar() fires it once the whole sidebar has been
# built. Nothing is lost, and the main body (which hasn't started yet) costs
# nothing extra. tests/test_sidebar_state_survives_actions.py pins both the
# rule and the behaviour.
_DEFERRED_RERUN_KEY = "_rerun_after_sidebar"


def _defer_rerun() -> None:
    """Request a rerun that fires at the end of the sidebar, not right now."""
    st.session_state[_DEFERRED_RERUN_KEY] = True


def _flush_deferred_rerun() -> None:
    """Fire the rerun a sidebar handler asked for (no-op when none did)."""
    if st.session_state.pop(_DEFERRED_RERUN_KEY, False):
        st.rerun()


# The Plan-month picker's widget key, and the non-widget seed a main-body
# button writes to switch it. render_sidebar() builds the picker before main()
# runs, and Streamlit forbids writing a widget's state once the widget exists
# in that run — so "📆 Plan August 2026 instead" can't set the picker directly.
# It writes PLAN_MONTH_SEED_KEY and reruns; the sidebar consumes the seed into
# the widget's state right before the selectbox is built, the one moment a
# keyed widget's value may be set from code (the same way `_sync_working_days`
# seeds the working-day count). tests/test_schedule_load_moment.py pins it.
PLAN_MONTH_KEY = "plan_month_select"
PLAN_MONTH_SEED_KEY = "_plan_month_seed"
# Two more one-shot seeds a main-panel button leaves for the next run (see
# _jump_to): the tab the strip should show, and "open the crew expander".
# Each is consumed right before the widget it targets is built.
ACTIVE_TAB_SEED_KEY = "_active_tab_seed"
CREW_EXPANDER_SEED_KEY = "_crew_expander_seed"
# (year, month) tuples the loaded schedule says it is for (PRODUCTION MONTH),
# written by _stash_upload_summary and read by the picker, so those months are
# always on its list even when they fall outside the rolling default window.
SCHEDULE_MONTHS_KEY = "_schedule_months"
# The (year, month) tuples the active schedule's rows are DATED in (PRODUCTION
# DAY, i.e. after auto-spread) — recorded by main() so the sidebar can apply
# the same "does the Time window slice anything" rule (see _window_applies).
SCHEDULE_DATED_MONTHS_KEY = "_schedule_dated_months"


def _month_label(ym) -> str:
    """``(2026, 8)`` → ``"August 2026"`` — the one spelling of a plan month."""
    return f"{calendar.month_name[int(ym[1])]} {int(ym[0])}"


def _plan_month_options(now, extra=()) -> "list[tuple[int, int]]":
    """Options for the Plan-month picker, chronological.

    The default list is a rolling window of 2 months back to 12 months
    forward from ``now``. Every valid ``(year, month)`` in ``extra`` — the
    loaded schedule's own months and whatever is selected right now — is
    added to it, so a schedule older than the window can still be planned as
    its own month, and a selection never silently drops off the list.
    """
    opts: set = set()
    for delta in range(-2, 13):
        m0 = now.month - 1 + delta
        opts.add((now.year + m0 // 12, m0 % 12 + 1))
    for ym in extra or ():
        try:
            y, m = int(ym[0]), int(ym[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 1 <= m <= 12 and 1 <= y <= 9999:
            opts.add((y, m))
    return sorted(opts)


def _sync_working_days(target_days: int, sig: tuple) -> None:
    """Seed the sidebar's "Working days in the period" input.

    Re-seeded on a new ``sig`` — (plan month, time window, today-in-month) —
    so the day count follows the period the demand is sliced to, and whenever
    the widget's state is simply gone. The input has no default of its own,
    so a wiped state fell to ``min_value=1`` while the browser still displayed
    the old number, and every capacity figure came out ~20× too high.
    :func:`_defer_rerun` stops the wipe at its source; this keeps the number
    honest regardless. A manual edit sticks until ``sig`` changes.
    """
    if (st.session_state.get("_work_days_synced_sig") != sig
            or "work_days_input" not in st.session_state):
        st.session_state["work_days_input"] = int(target_days)
        st.session_state["_work_days_synced_sig"] = sig


# ---------------------------------------------------------------------------
# Auto-spread (LPT level-loading)
# ---------------------------------------------------------------------------
# Month-label parsers ("May-26" / "26-Apr") for the target-month detection.
_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec"]
_CURRENT_MONTH_RE = re.compile(r"^([A-Za-z]{3})-(\d{2})$")
_CARRYOVER_MONTH_RE = re.compile(r"^(\d{2})-([A-Za-z]{3})$")


def _parse_target_month(month_label: str) -> "tuple[int, int] | None":
    """Convert a PRODUCTION MONTH cell value to (year, month_num) or None.

    Accepts the two formats the existing loader produces:
      "May-26"  → (2026, 5)   (current-month style)
      "26-Apr"  → (2026, 4)   (carryover style)
    """
    if not month_label:
        return None
    s = str(month_label).strip()
    m = _CURRENT_MONTH_RE.match(s)
    if m:
        mon_str, yy = m.group(1), m.group(2)
    else:
        m = _CARRYOVER_MONTH_RE.match(s)
        if not m:
            return None
        yy, mon_str = m.group(1), m.group(2)
    try:
        mon_idx = _MONTH_NAMES.index(mon_str.lower()) + 1
        year = 2000 + int(yy)
    except (ValueError, IndexError):
        return None
    return year, mon_idx


def _working_days_in_month(year: int, month: int) -> list[date]:
    """M–F dates in the given month, ordered chronologically.

    No holiday awareness — if the team has a known company holiday, the
    heatmap surfaces the resulting overload and the planner can re-date
    the affected rows by hand.
    """
    last = calendar.monthrange(year, month)[1]
    out = []
    for d in range(1, last + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:  # Monday=0 … Friday=4
            out.append(dt)
    return out


def _row_labor_weight(row: pd.Series, machine_df, acc_df) -> float:
    """Approximate per-row labor (person-minutes × qty).

    Falls back to ``BUILD QTY × 100`` when ``compute_unit_labor`` can't
    resolve the SKU — still gets the row a non-zero weight so LPT can
    place it somewhere reasonable.
    """
    fg = row.get("FG_BASE") or row.get("FG SKU ID")
    acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
    qty = float(row.get("BUILD QTY", 0) or 0)
    try:
        labor = compute_unit_labor(fg, acc, machine_df, acc_df)
        if labor is None:
            return max(qty, 1.0) * 100.0
        per_unit = sum(float(labor.get(s, 0) or 0) for s in STATION_KEYS)
        return max(qty, 1.0) * per_unit
    except Exception:
        return max(qty, 1.0) * 100.0


_MONTH_FROM_NAME = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_month_from_name(label: str) -> "tuple[int, int] | None":
    """Try to extract a (year, month) from a free-form name like
    "Hen 2026 June" or "May-26 plan". Returns None if no month token
    can be found.
    """
    if not label:
        return None
    text = str(label).lower()
    # Find month token
    month = None
    for token, m in _MONTH_FROM_NAME.items():
        # Whole-word match so "marble" doesn't match "mar"
        if re.search(rf"\b{token}\b", text):
            month = m
            break
    if month is None:
        return None
    # Find year token — 4-digit first, then 2-digit (assume 20xx)
    year = None
    y4 = re.search(r"\b(20\d{2})\b", text)
    if y4:
        year = int(y4.group(1))
    else:
        y2 = re.search(r"\b(\d{2})\b", text)
        if y2:
            year = 2000 + int(y2.group(1))
    if year is None:
        year = now_local().year
    return (year, month)


def _auto_spread_dates(
    df: pd.DataFrame, machine_df, acc_df, location: str,
    target_month_hint: "tuple[int, int] | None" = None,
) -> pd.DataFrame:
    """Fill blank PRODUCTION DAY cells with auto-leveled dates.

    Uses the **Longest Processing Time** heuristic for the parallel-
    machine scheduling problem: sort blank rows by labor weight, descending,
    and place each into the working day with the smallest running total.
    Provably gives ≤ 4/3× optimal makespan and runs in O(n log n).

    Mixed-mode aware: rows that already have a valid PRODUCTION DAY are
    preserved, and their labor weight seeds that day's running total so
    the blank rows are level-loaded **on top** of the existing pinned plan.

    Target-month resolution order:
      1. ``target_month_hint`` (callers in manual mode pass this from a
         scenario name like "Hen 2026 June" → (2026, 6)).
      2. First non-null ``PRODUCTION MONTH`` value parseable by
         ``_parse_target_month`` (e.g. "May-26" or "26-Apr").
      3. Fallback: today's month in Las Vegas time.

    Side effects:
      • Sets ``df["PRODUCTION DAY"]`` for previously-blank rows to a real
        ``pd.Timestamp`` in the target month.
      • Recomputes ``df["WEEK_OF_MONTH"]`` for all rows from the new
        PRODUCTION DAY values.
      • Attaches ``df.attrs["auto_spread"]`` = a small dict with metadata
        for the UI to surface: ``{count, total_rows, target_month_label,
        working_days}``.

    Returns the (mutated) df. Guards against the obvious edge cases — empty
    schedule, no working days, undetectable target month — by returning the
    df unchanged with no ``auto_spread`` attribute set.
    """
    if df is None or df.empty:
        return df

    # Identify which rows need a date assigned. Anything that isn't a real
    # Timestamp counts as "blank" (handles NaT, empty strings, etc.).
    if "PRODUCTION DAY" in df.columns:
        day_series = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce")
    else:
        day_series = pd.Series([pd.NaT] * len(df), index=df.index)
    blank_mask = day_series.isna()
    if not blank_mask.any():
        return df  # nothing to do — every row is already dated

    # Detect the target (year, month). Hint wins → schedule's PRODUCTION
    # MONTH → today's month as fallback.
    target = target_month_hint
    if target is None and "PRODUCTION MONTH" in df.columns:
        for v in df["PRODUCTION MONTH"].dropna():
            target = _parse_target_month(v)
            if target:
                break
    if target is None:
        _now = now_local()
        target = (_now.year, _now.month)
    year, month = target

    working = _working_days_in_month(year, month)
    if not working:
        return df  # defensive — never happens for real months

    # Map each working day to its week-of-month bucket so we can balance
    # load at the WEEK level first (so a small schedule doesn't bunch into
    # the first few days), then break ties at the day level. Week loads are
    # compared PER WORKING DAY of the week: a month's first or last calendar
    # week is usually partial (Aug 2026 ends on a lone Monday), and comparing
    # raw week totals handed such a week a full week's units on its one day.
    day_to_week: dict[date, int] = {d: week_of_month(d) for d in working}
    weeks_set = sorted(set(day_to_week.values()))
    week_days: dict[int, int] = {
        w: sum(1 for d in working if day_to_week[d] == w) for w in weeks_set
    }

    # Seed running loads from rows that ARE already dated.
    day_load: dict[date, float] = {d: 0.0 for d in working}
    week_load: dict[int, float] = {w: 0.0 for w in weeks_set}
    for idx in df.index[~blank_mask]:
        d = day_series.loc[idx].date()
        if d in day_load:
            w = _row_labor_weight(df.loc[idx], machine_df, acc_df)
            day_load[d] += w
            week_load[day_to_week[d]] = week_load.get(day_to_week[d], 0.0) + w
        # If a pinned date falls outside the target month's working days,
        # we don't add it to any bucket — auto-spread still works, the
        # heatmap just won't show that pin.

    # Compute labor weights for the blank rows once, then walk them
    # largest-first and drop each into the lightest-loaded (week, day).
    # The (week_load per working day, day_load) key means a 5-unit plan
    # spreads as 1-per-week instead of clustering in the first ~5 days.
    blank_idxs = list(df.index[blank_mask])
    weights = {idx: _row_labor_weight(df.loc[idx], machine_df, acc_df)
               for idx in blank_idxs}
    blank_idxs.sort(key=lambda i: weights[i], reverse=True)

    assigned: dict = {}
    for idx in blank_idxs:
        d = min(
            working,
            key=lambda x: (
                week_load[day_to_week[x]] / week_days[day_to_week[x]],
                day_load[x],
            ),
        )
        assigned[idx] = d
        day_load[d] += weights[idx]
        week_load[day_to_week[d]] += weights[idx]

    # Write the new dates back. Preserve the pinned rows untouched.
    new_days = day_series.copy()
    for idx, d in assigned.items():
        new_days.loc[idx] = pd.Timestamp(d)
    df["PRODUCTION DAY"] = new_days

    # Recompute WEEK_OF_MONTH from the (now-complete) PRODUCTION DAY.
    df["WEEK_OF_MONTH"] = week_of_month_series(new_days)

    df.attrs["auto_spread"] = {
        "count": int(blank_mask.sum()),
        "total_rows": int(len(df)),
        "target_month_label": f"{calendar.month_name[month]} {year}",
        "working_days": len(working),
    }
    return df


def _schedule_month_units(df: pd.DataFrame) -> "dict[tuple[int, int], int]":
    """Units per calendar month the schedule says it is for, from PRODUCTION
    MONTH (``May-26`` or carryover ``26-Apr``), weighted by BUILD QTY.

    Deliberately not from PRODUCTION DAY: right after a load, auto-spread has
    filled every blank day with a date in the *Plan* month, so the dates say
    whatever the picker says. PRODUCTION MONTH is the file's own statement.
    Rows whose month doesn't parse ("Manual", blank) are ignored.
    """
    out: dict = {}
    if df is None or df.empty or "PRODUCTION MONTH" not in df.columns:
        return out
    if "BUILD QTY" in df.columns:
        qty = pd.to_numeric(df["BUILD QTY"], errors="coerce").fillna(0)
    else:
        qty = pd.Series(1, index=df.index)
    for month_value, units in zip(df["PRODUCTION MONTH"], qty):
        ym = _parse_target_month(month_value)
        if ym is None:
            continue
        out[ym] = out.get(ym, 0) + int(units)
    return out


def _schedule_dominant_month(df: pd.DataFrame) -> "tuple[int, int] | None":
    """The (year, month) carrying the most units in
    :func:`_schedule_month_units` — the month the schedule is *for*. A tie
    goes to the later month (a file with 20 carryover units and 20 new ones
    is for the new month). None when no PRODUCTION MONTH parses."""
    by_month = _schedule_month_units(df)
    if not by_month:
        return None
    return max(by_month, key=lambda ym: (by_month[ym], ym))


def _row_is_carryover(month_value, plan_month) -> bool:
    """True when a row's PRODUCTION MONTH is *older* than the chosen Plan
    month — i.e. it was supposed to be built earlier and has carried over.

    ``plan_month`` is the (year, month) tuple from the sidebar picker.
    Rows whose PRODUCTION MONTH can't be parsed (blank, "Manual", …) are not
    carryover.
    """
    if not plan_month:
        return False
    t = _parse_target_month(month_value)
    return bool(t is not None and tuple(t) < tuple(plan_month))


def _recompute_carryover(df: pd.DataFrame, plan_month) -> pd.DataFrame:
    """Set ``df['CARRYOVER']`` relative to the Plan month: True when the row's
    PRODUCTION MONTH is earlier than the plan month. Overrides the loader's
    format-based flag so the whole app shares one carryover definition."""
    if (
        df is None or df.empty
        or "PRODUCTION MONTH" not in df.columns
        or not plan_month
    ):
        return df
    df = df.copy()
    df["CARRYOVER"] = df["PRODUCTION MONTH"].apply(
        lambda v: _row_is_carryover(v, plan_month)
    )
    return df


def _plan_month_transfer_counts(df: pd.DataFrame, plan_month) -> "tuple[int, int]":
    """Rows that won't appear on the plan-month calendar and can be pulled in.

    Union of (a) rows DATED in a different month and (b) carryover rows
    (PRODUCTION MONTH earlier than the plan month) not yet dated in it — exactly
    the rows :func:`_transfer_to_plan_month` re-spreads. Returns
    ``(n_to_move, n_carryover_total)``; the second is only for the all-clear note.
    """
    if df is None or df.empty or not plan_month:
        return 0, 0
    py, pm = plan_month
    if "PRODUCTION DAY" in df.columns:
        pday = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce")
    else:
        pday = pd.Series(pd.NaT, index=df.index)
    in_plan = pday.notna() & (pday.dt.year == py) & (pday.dt.month == pm)
    out_mask = pday.notna() & ~in_plan  # dated in a different month
    if "PRODUCTION MONTH" in df.columns:
        carry = df["PRODUCTION MONTH"].apply(lambda v: _row_is_carryover(v, plan_month))
    else:
        carry = pd.Series(False, index=df.index)
    need = out_mask | (carry & ~in_plan)
    return int(need.sum()), int(carry.sum())


def _transfer_to_plan_month(df, machine_df, acc_df, location, plan_month):
    """Pull every row that isn't on the plan-month calendar INTO it.

    Merges what used to be two near-identical buttons (they did the same thing
    whenever a row matched both) into one action over the union of:
      • rows whose PRODUCTION DAY is dated in a different month (frozen dates
        from a save under another Plan month), and
      • carryover rows (PRODUCTION MONTH earlier than the plan month) not yet
        dated in it — including undated ones.
    Clears their PRODUCTION DAY and level-loads via the LPT auto-spread, seeded
    by rows already in-month. PRODUCTION MONTH is left untouched, so carryover
    rows stay flagged as carryover and are built first. Returns (new_df, n_moved).
    """
    if df is None or df.empty or not plan_month:
        return df, 0
    py, pm = plan_month
    out = df.copy()
    if "PRODUCTION DAY" in out.columns:
        pday = pd.to_datetime(out["PRODUCTION DAY"], errors="coerce")
    else:
        pday = pd.Series(pd.NaT, index=out.index)
    in_plan = pday.notna() & (pday.dt.year == py) & (pday.dt.month == pm)
    out_mask = pday.notna() & ~in_plan
    if "PRODUCTION MONTH" in out.columns:
        carry = out["PRODUCTION MONTH"].apply(lambda v: _row_is_carryover(v, plan_month))
    else:
        carry = pd.Series(False, index=out.index)
    need = out_mask | (carry & ~in_plan)
    n = int(need.sum())
    if n == 0:
        return out, 0
    out["PRODUCTION DAY"] = pday
    out.loc[need, "PRODUCTION DAY"] = pd.NaT
    _auto_spread_dates(out, machine_df, acc_df, location, target_month_hint=plan_month)
    return out, n


def _suggest_schedule_dates(
    schedule_df: pd.DataFrame, machine_df, acc_df, inputs: dict,
) -> "tuple[pd.DataFrame, list[dict]]":
    """Constraint-aware version of auto-spread.

    Different from ``_auto_spread_dates`` in three ways:
      1. **Overrides** existing PRODUCTION DAY values — the planner clicked
         "Suggest" so they want a fresh re-spread, not an additive one.
      2. **Per-station capacity check** — for each candidate day, the placer
         verifies that putting this unit's labor into that week wouldn't
         tip any station above ``UTIL_THRESHOLD_OVER`` (1.0). If every day
         fails, the unit lands on the least-loaded day and the offending
         station(s) are recorded in the returned overload list.
      3. **Returns a new DataFrame** rather than mutating the original, so
         the UI can preview the proposal alongside the current schedule.

    Returns ``(new_schedule_df, overloads)`` where ``overloads`` is a list
    of dicts: ``{"row_idx", "fg", "week", "stations"}``.
    """
    if schedule_df is None or schedule_df.empty:
        return schedule_df, []

    new_df = schedule_df.copy()

    # ---- Target month detection ----------------------------------------
    # The sidebar "Plan month" picker is authoritative (same as auto-spread),
    # so the suggestion level-loads across the month the planner chose — not
    # whatever the CSV's PRODUCTION MONTH happens to say. Fall back to the
    # CSV month, then today, only if no plan month is set.
    target = inputs.get("plan_month") if isinstance(inputs, dict) else None
    if target is None and "PRODUCTION MONTH" in new_df.columns:
        for v in new_df["PRODUCTION MONTH"].dropna():
            target = _parse_target_month(v)
            if target:
                break
    if target is None:
        _now = now_local()
        target = (_now.year, _now.month)
    year, month = target

    working = _working_days_in_month(year, month)
    if not working:
        return new_df, []

    # ---- Per-station capacity per WORKING DAY ---------------------------
    # Scaled to each week's true Mon–Fri count below. A flat 5-day week let a
    # partial first / last calendar week (1–4 working days) absorb a full
    # week's units — the Calendar then showed a lone Monday carrying five
    # days of work and the Overview heatmap lit that week up at 500%.
    from core.constants import STATION_KEY_TO_DISPLAY, UTIL_THRESHOLD_OVER
    crew_config = inputs["crew_config"]
    shift = float(inputs["shift"])
    eff = float(inputs["efficiency"])
    safety = float(inputs["safety"])

    station_capacity: dict[str, float] = {}
    for st_key in STATION_KEYS:
        st_disp = STATION_KEY_TO_DISPLAY.get(st_key)
        if not st_disp or st_disp not in crew_config.index:
            station_capacity[st_key] = 0.0
            continue
        try:
            hc = int(crew_config.loc[st_disp, "HC"])
        except Exception:
            hc = 0
        station_capacity[st_key] = hc * shift * eff * safety if hc > 0 else 0.0

    # Map each working day → its week, and each week → its working days.
    day_to_week = {d: week_of_month(d) for d in working}
    weeks = sorted(set(day_to_week.values()))
    week_ndays = {w: sum(1 for d in working if day_to_week[d] == w) for w in weeks}

    def _week_cap(st_key: str, w: int) -> float:
        """A station's labor capacity over the working days of week ``w``."""
        return station_capacity.get(st_key, 0.0) * week_ndays.get(w, 5)

    def _per_day(w: int) -> float:
        """Week ``w``'s running load per working day — the balance key."""
        return week_total_load[w] / week_ndays[w]

    # Running per-station-per-week load + total per-day load + total
    # per-week load. The week-level total is what lets us spread a small
    # plan across all 4-5 weeks instead of clustering everything in the
    # first few days (LPT minimizes makespan; balancing weeks first then
    # days within a week gives the level-loaded heatmap planners expect).
    station_load: dict = {(st, w): 0.0 for st in STATION_KEYS for w in weeks}
    day_total_load: dict = {d: 0.0 for d in working}
    week_total_load: dict = {w: 0.0 for w in weeks}

    # Per-row labor breakdown
    def _row_breakdown(row) -> "tuple[dict, float]":
        fg = row.get("FG_BASE") or row.get("FG SKU ID")
        acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
        try:
            qty = float(row.get("BUILD QTY", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            labor = compute_unit_labor(fg, acc, machine_df, acc_df)
            if labor is None:
                return {}, max(qty, 1.0) * 100.0
            breakdown = {
                s: float(labor.get(s, 0) or 0) * qty for s in STATION_KEYS
            }
            total = sum(breakdown.values())
            return breakdown, total
        except Exception:
            return {}, max(qty, 1.0) * 100.0

    # Compute all breakdowns up front and sort largest-first (LPT)
    rows = []
    for idx in new_df.index:
        br, total = _row_breakdown(new_df.loc[idx])
        rows.append((idx, br, total))
    rows.sort(key=lambda r: r[2], reverse=True)

    overloads: list[dict] = []
    assigned: dict = {}
    for idx, breakdown, total in rows:
        # Walk candidate days lightest-first, where "light" means the
        # day's *week* has the least load. Within a week, prefer the
        # lightest day. Without the week-level key, a small plan would
        # cluster into the first few days because LPT optimizes makespan
        # per day, not balance per week.
        candidates = sorted(
            working,
            key=lambda d: (_per_day(day_to_week[d]), day_total_load[d]),
        )
        placed_day = None
        for cand in candidates:
            w = day_to_week[cand]
            ok = True
            for st_key, cost in breakdown.items():
                if cost <= 0:
                    continue
                cap = _week_cap(st_key, w)
                if cap <= 0:
                    # Station doesn't exist here — skip the constraint check
                    # (treat it like infinite capacity for this purpose).
                    continue
                if (station_load[(st_key, w)] + cost) / cap > UTIL_THRESHOLD_OVER:
                    ok = False
                    break
            if ok:
                placed_day = cand
                break

        if placed_day is None:
            # No day satisfies the per-station check. Fall back to the
            # least-loaded week+day pair and record which stations are
            # going to be tipped over (same ordering as the constraint
            # search, so the fallback respects week-balance too).
            placed_day = min(
                working,
                key=lambda d: (_per_day(day_to_week[d]), day_total_load[d]),
            )
            w = day_to_week[placed_day]
            offenders = []
            for st_key, cost in breakdown.items():
                if cost <= 0:
                    continue
                cap = _week_cap(st_key, w)
                if cap > 0 and (station_load[(st_key, w)] + cost) / cap > UTIL_THRESHOLD_OVER:
                    offenders.append(st_key)
            if offenders:
                fg_label = (
                    new_df.loc[idx].get("FG_BASE")
                    or new_df.loc[idx].get("FG SKU ID")
                    or "?"
                )
                overloads.append({
                    "row_idx": idx,
                    "fg": str(fg_label),
                    "week": int(w),
                    "stations": offenders,
                })

        # Commit the placement
        assigned[idx] = placed_day
        day_total_load[placed_day] += total
        wk = day_to_week[placed_day]
        week_total_load[wk] = week_total_load.get(wk, 0.0) + total
        for st_key, cost in breakdown.items():
            station_load[(st_key, wk)] = station_load.get((st_key, wk), 0.0) + cost

    # ---- Write the proposed dates back ---------------------------------
    new_days = pd.Series(
        [pd.Timestamp(assigned[i]) for i in new_df.index],
        index=new_df.index,
        dtype="datetime64[ns]",
    )
    new_df["PRODUCTION DAY"] = new_days
    new_df["WEEK_OF_MONTH"] = week_of_month_series(new_days)
    new_df.attrs["suggested"] = {
        "target_month_label": f"{calendar.month_name[month]} {year}",
        "working_days": len(working),
        "total_rows": int(len(new_df)),
        "overloads_count": len(overloads),
    }
    return new_df, overloads


def _diagnose_overloads(
    schedule_df: pd.DataFrame,
    machine_df,
    acc_df,
    inputs: dict,
    overloads: "list[dict]",
) -> "list[dict]":
    """Turn an overload list into actionable diagnoses.

    For each station that's binding (i.e. some unit couldn't fit there at
    safe capacity), compute:
      • monthly demand at that station (p-min)
      • monthly capacity at the current settings (p-min)
      • shortfall (demand − capacity, p-min)
      • three concrete fixes:
          1. HC to add — ``ceil(shortfall / (shift × days × eff × safety))``
          2. Extra working days needed
          3. Which heaviest units to defer to free up the shortfall

    Returns a list of dicts (one per binding station), sorted by severity:
    ``[{station, demand, capacity, shortfall, shortfall_pct,
        hire_hc, extend_days, defer_units}, …]``.

    Returns ``[]`` when ``overloads`` is empty (sanity guard).
    """
    if not overloads or schedule_df is None or schedule_df.empty:
        return []
    from core.constants import STATION_KEY_TO_DISPLAY

    crew_config = inputs["crew_config"]
    shift = float(inputs["shift"])
    eff = float(inputs["efficiency"])
    safety = float(inputs["safety"])
    days = float(inputs["days"])

    # 1) Identify the set of stations called out as binding.
    binding_stations: set = set()
    for o in overloads:
        for s in o.get("stations", []):
            binding_stations.add(s)
    if not binding_stations:
        return []

    # 2) Compute monthly demand + capacity per binding station.
    diagnoses: list[dict] = []
    for st_key in binding_stations:
        # Sum p-min of this station across the whole schedule.
        demand = 0.0
        for _, row in schedule_df.iterrows():
            try:
                fg = row.get("FG_BASE") or row.get("FG SKU ID")
                acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
                qty = float(row.get("BUILD QTY", 0) or 0)
                labor = compute_unit_labor(fg, acc, machine_df, acc_df)
                if labor is None:
                    continue
                demand += float(labor.get(st_key, 0) or 0) * qty
            except Exception:
                continue
        if demand <= 0:
            continue

        st_disp = STATION_KEY_TO_DISPLAY.get(st_key, st_key)
        if st_disp not in crew_config.index:
            continue
        try:
            hc = int(crew_config.loc[st_disp, "HC"])
        except Exception:
            hc = 0

        capacity = hc * shift * days * eff * safety if hc > 0 else 0.0
        shortfall = max(demand - capacity, 0.0)
        if shortfall <= 0:
            continue  # not actually short at the monthly level
        shortfall_pct = (demand / capacity - 1.0) if capacity > 0 else float("inf")

        # 3a) HC to add: ceil(shortfall / per-HC monthly cap)
        per_hc_month = shift * days * eff * safety
        hire_hc = int((shortfall + per_hc_month - 1) // per_hc_month) if per_hc_month > 0 else 0

        # 3b) Extra working days: ceil(shortfall / per-day cap)
        per_day_cap = hc * shift * eff * safety if hc > 0 else 0.0
        extend_days = int((shortfall + per_day_cap - 1) // per_day_cap) if per_day_cap > 0 else 0

        # 3c) Heaviest units to defer: pick FG SKUs by descending labor at
        # this station × qty, accumulate until shortfall is covered.
        per_row_labor: list[tuple] = []
        for idx, row in schedule_df.iterrows():
            try:
                fg = row.get("FG_BASE") or row.get("FG SKU ID")
                acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
                qty = float(row.get("BUILD QTY", 0) or 0)
                labor = compute_unit_labor(fg, acc, machine_df, acc_df)
                if labor is None:
                    continue
                row_total = float(labor.get(st_key, 0) or 0) * qty
                if row_total > 0:
                    per_row_labor.append((row_total, str(fg), int(qty)))
            except Exception:
                continue
        per_row_labor.sort(reverse=True)
        defer_units: list[dict] = []
        freed = 0.0
        for total, fg, qty in per_row_labor:
            if freed >= shortfall:
                break
            defer_units.append({"fg": fg, "qty": qty, "labor_at_station": total})
            freed += total

        diagnoses.append({
            "station_key": st_key,
            "station_display": st_disp,
            "demand": demand,
            "capacity": capacity,
            "shortfall": shortfall,
            "shortfall_pct": shortfall_pct,
            "hire_hc": hire_hc,
            "extend_days": extend_days,
            "defer_units": defer_units,
            "freed_by_defer": freed,
        })

    # Sort severity (largest shortfall first)
    diagnoses.sort(key=lambda d: d["shortfall"], reverse=True)
    return diagnoses


def _upload_sig(uploaded_file) -> str:
    """Stable identity for a Streamlit ``UploadedFile`` so we can tell a
    genuinely new upload from the same file persisting across reruns.

    Prefers Streamlit's per-upload ``file_id``; falls back to name + size.
    """
    if uploaded_file is None:
        return ""
    fid = getattr(uploaded_file, "file_id", None)
    if fid:
        return str(fid)
    return f"{getattr(uploaded_file, 'name', '')}:{getattr(uploaded_file, 'size', '')}"


# ---------------------------------------------------------------------------
# Active-schedule resolution — shared by both sidebar modes
# ---------------------------------------------------------------------------
# The sidebar offers two ways to describe the build plan. Both end up as the
# same schedule frame, and every reshape action (Sequencer ✅ Accept, 🗓 Sequence
# Apply, 🔀 Bulk move, 🧹 Suffix strip, 📅 Move into plan month) hands its
# result over through ONE one-shot channel, ``_loaded_schedule_buffer``, which
# the next run's resolver consumes:
#
#   • 📤 Upload mode → ``_load_schedule_df`` promotes the buffer to the
#     persistent ``_active_schedule_csv`` (which is also what 💾 Save schedule
#     pushes to GitHub).
#   • ✏️ Manual mode → ``_resolve_manual_schedule`` keeps it as
#     ``_manual_schedule_override``, pinned to a signature of the sidebar grid,
#     so the reshape rides along across reruns but is dropped the moment the
#     planner changes the rows it was built from.
#
# Before the manual half existed, a buffer written in manual mode was never
# read ("accepted — refreshing…" and nothing changed), and the leftover buffer
# then hijacked the next load after a switch to upload mode.
SCHEDULE_MODE_UPLOAD = "📤 Upload schedule file"
SCHEDULE_MODE_MANUAL = "✏️ Paste or type SKUs"

# Session keys whose content was computed against ONE mode's schedule and must
# not survive a switch to the other: a pending Sequencer proposal, and a
# reshape waiting in the one-shot buffer.
_CROSS_MODE_SCHEDULE_KEYS = (
    "_loaded_schedule_buffer",
    "_loaded_schedule_source",
    "_loaded_schedule_name",
    "_loaded_schedule_kind",
    "_loaded_schedule_label",
    "_proposed_schedule",
    "_proposed_schedule_overloads",
)


def _discard_cross_mode_schedule_state() -> None:
    """Drop a proposal / pending reshape computed in the other sidebar mode."""
    for key in _CROSS_MODE_SCHEDULE_KEYS:
        st.session_state.pop(key, None)


def _note_schedule_mode(schedule_mode: str) -> bool:
    """Record the sidebar mode for this run; on a change from the previous run,
    discard the cross-mode state. Returns True when the mode changed.

    The upload summary (the pill and the dropped-unit numbers) describes the
    last upload; in ✏️ manual mode it would describe a file the planner is no
    longer analysing, so it goes too."""
    prev = st.session_state.get("_schedule_mode_seen")
    changed = prev is not None and prev != schedule_mode
    if changed:
        _discard_cross_mode_schedule_state()
        if schedule_mode == SCHEDULE_MODE_MANUAL:
            st.session_state.pop("_last_upload_summary", None)
    st.session_state["_schedule_mode_seen"] = schedule_mode
    return changed


def _commit_reshape(new_df: pd.DataFrame, label: str, *, event: "str | None" = None,
                    **payload) -> None:
    """Hand a reshaped schedule (Sequencer ✅ Accept, 🗓 Apply, 🔀 Bulk move,
    🧹 Strip, 📅 Move into plan month) to the resolver as the next active
    schedule, with the bookkeeping that makes it undoable.

    Writes the one-shot buffer the resolver consumes, tags it ``kind =
    "reshape"`` so :func:`_load_schedule_df` keeps the previous bytes as the
    undo step (and the schedule's source / name — a reshaped upload used to be
    relabelled "saved schedule"), and stashes the confirmation as a one-shot
    toast the main panel replays after the rerun — a ``st.success`` drawn here
    was thrown away with the run that raised it. The caller reruns.
    """
    st.session_state["_loaded_schedule_buffer"] = io.BytesIO(
        new_df.to_csv(index=False).encode("utf-8")
    )
    st.session_state["_loaded_schedule_kind"] = "reshape"
    st.session_state["_loaded_schedule_label"] = str(label)
    st.session_state["_schedule_toast"] = ("success", f"✅ {label}.")
    if event:
        _track_and_flush(event, **payload)


def _schedule_edit_count() -> int:
    """How many reshapes the active schedule carries since it was loaded."""
    try:
        return int(st.session_state.get("_schedule_edit_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _queue_schedule_bytes(csv_bytes: bytes, *, kind: "str | None", source: str,
                          name: "str | None", toast: str) -> None:
    """Common tail of the way-back actions: queue ``csv_bytes`` for the
    resolver with its kind / source / name and a one-shot toast."""
    st.session_state["_loaded_schedule_buffer"] = io.BytesIO(csv_bytes)
    if kind:
        st.session_state["_loaded_schedule_kind"] = kind
    else:
        st.session_state.pop("_loaded_schedule_kind", None)
    st.session_state.pop("_loaded_schedule_label", None)
    st.session_state["_loaded_schedule_source"] = source
    if name:
        st.session_state["_loaded_schedule_name"] = name
    else:
        st.session_state.pop("_loaded_schedule_name", None)
    st.session_state["_schedule_toast"] = ("success", toast)


def _undo_last_reshape() -> bool:
    """Queue the schedule as it was before the last reshape. The resolver
    (kind ``undo``) drops the undo step and counts the edit off. False when
    there is nothing to undo."""
    undo = st.session_state.get("_schedule_undo")
    if not isinstance(undo, dict) or not undo.get("csv"):
        return False
    label = undo.get("label") or "the last change"
    _queue_schedule_bytes(
        undo["csv"], kind="undo", source=undo.get("source") or "saved",
        name=undo.get("name"), toast=f"↩️ Undid: {label}.",
    )
    _track_and_flush("schedule_undo", label=label)
    return True


def _restore_schedule_as_loaded() -> bool:
    """Queue the schedule exactly as it was loaded (upload, 📥 Load, the
    automatic newest save, or the sample), dropping every reshape since.
    False when nothing was recorded."""
    orig = st.session_state.get("_schedule_original")
    if not isinstance(orig, dict) or not orig.get("csv"):
        return False
    _queue_schedule_bytes(
        orig["csv"], kind="restore", source=orig.get("source") or "saved",
        name=orig.get("name"), toast="⏮ Back to the schedule as it was loaded.",
    )
    _track_and_flush("schedule_restore_original", edits=_schedule_edit_count())
    return True


def _queue_bundled_sample_load() -> bool:
    """Queue the bundled sample (``data/may_schedule.csv``) as a chosen
    schedule — promoted like a 📥 Load, so the facility's newest save isn't
    picked again on the next run. False when the sample file is missing."""
    p = DATA_DIR / "may_schedule.csv"
    try:
        csv_bytes = p.read_bytes()
    except OSError:
        return False
    _queue_schedule_bytes(
        csv_bytes, kind=None, source="bundled", name="May 2026 sample",
        toast="🧪 Using the bundled May 2026 sample schedule.",
    )
    _track_and_flush("schedule_load_sample")
    return True


def _restore_schedule_before_error() -> bool:
    """After an upload the loader couldn't read, put back the schedule that
    was active before it (kept aside by the resolver) and clear the error.
    False when there was nothing active before."""
    prev = st.session_state.pop("_schedule_before_error", None)
    st.session_state.pop("_active_schedule_error", None)
    if not isinstance(prev, dict) or not prev.get("csv"):
        return False
    st.session_state["_active_schedule_csv"] = prev["csv"]
    st.session_state["_active_schedule_source"] = prev.get("source") or "saved"
    st.session_state["_active_schedule_name"] = prev.get("name")
    st.session_state["_active_schedule_sheet"] = prev.get("sheet")
    if prev.get("auto_for"):
        st.session_state["_active_schedule_auto_for"] = prev["auto_for"]
    else:
        st.session_state.pop("_active_schedule_auto_for", None)
    for _k in ("_schedule_undo", "_schedule_original", "_schedule_edit_count"):
        if prev.get(_k) is not None:
            st.session_state[_k] = prev[_k]
    return True


def _drop_manual_override() -> bool:
    """✏️ manual mode's way back: forget the reshape laid over the typed grid so
    the next run analyses the grid as typed. False when there was none."""
    if st.session_state.pop("_manual_schedule_override", None) is None:
        return False
    st.session_state["_schedule_toast"] = ("success", "↩️ Back to the list as typed.")
    return True


def _window_applies(plan_month, schedule_months) -> bool:
    """Whether a "This week" / "Remaining (from today)" window has anything to
    slice: today must fall inside the Plan month AND, when the schedule carries
    dated rows, inside a month those rows are dated in. One rule for the
    sidebar's working-day count and main()'s slice — they used to decide
    differently (plan month vs. dated months) and could judge a week of demand
    against a month of capacity, or the reverse.

    ``schedule_months`` is any iterable of ``(year, month)`` tuples (the
    ``SCHEDULE_MONTHS_KEY`` list, or the months of PRODUCTION DAY); empty /
    None means "no dated rows", which the Plan month alone decides.
    """
    if not plan_month:
        return False
    now = now_local()
    today = (now.year, now.month)
    if today != (int(plan_month[0]), int(plan_month[1])):
        return False
    months = set()
    for m in (schedule_months or ()):
        try:
            months.add((int(m[0]), int(m[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return not months or today in months


def _note_facility(location: str) -> bool:
    """Record the facility for this run; on a change, drop a pending Sequencer
    proposal. The proposal was built from the previous plant's rows and crew —
    ✅ Accept at the new plant would adopt it as that plant's schedule.
    Returns True when the facility changed."""
    prev = st.session_state.get("_facility_seen")
    changed = prev is not None and prev != location
    if changed:
        st.session_state.pop("_proposed_schedule", None)
        st.session_state.pop("_proposed_schedule_overloads", None)
    st.session_state["_facility_seen"] = location
    return changed


def _manual_schedule_signature(base_df) -> str:
    """Identity of the manual grid's content: which rows (location, SKU,
    accessory, qty) in which order. Blank rows never reach ``base_df``, so
    adding or deleting one doesn't count; changing a SKU or a quantity does."""
    if base_df is None or getattr(base_df, "empty", True):
        return ""
    cols = [
        c for c in ("LOCATION", "FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY")
        if c in base_df.columns
    ]
    text = "\n".join(
        "\t".join("" if pd.isna(v) else str(v).strip() for v in row)
        for row in base_df[cols].itertuples(index=False)
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _resolve_manual_schedule(base_df, location, machine_df, acc_df, plan_month=None):
    """Manual-mode half of the active-schedule resolver.

    ``base_df`` is the schedule freshly built from the sidebar grid. A reshape
    waiting in ``_loaded_schedule_buffer`` is adopted as the override for this
    grid content; an override that already exists for this grid content is
    returned instead of ``base_df`` — re-read through the same ``load_schedule``
    path an upload takes, so row splits from a 🔀 Bulk move and SKU rewrites
    from a 🧹 Suffix strip survive. Otherwise — including when the grid changed
    since the reshape was made, or the grid is empty — ``base_df`` wins and any
    stale override or buffer is dropped.

    Either way the result is auto-spread so undated rows land in the plan
    month, exactly as before.
    """
    sig = _manual_schedule_signature(base_df)
    buf = st.session_state.pop("_loaded_schedule_buffer", None)
    st.session_state.pop("_loaded_schedule_source", None)
    st.session_state.pop("_loaded_schedule_name", None)
    st.session_state.pop("_loaded_schedule_kind", None)
    st.session_state.pop("_loaded_schedule_label", None)
    if buf is not None and sig:
        st.session_state["_manual_schedule_override"] = {
            "sig": sig, "csv": buf.getvalue(),
        }
    override = st.session_state.get("_manual_schedule_override")
    if isinstance(override, dict) and sig and override.get("sig") == sig:
        df = None
        try:
            df = load_schedule(
                io.BytesIO(override.get("csv") or b""), location=location,
                machine_skus=set(machine_df["SKU"]),
                placeholder_map=build_placeholder_map(machine_df),
                acc_skus=set(acc_df["SKU"].astype(str)),
            )
        except Exception:
            df = None
        if df is not None and not df.empty:
            _auto_spread_dates(
                df, machine_df, acc_df, location, target_month_hint=plan_month,
            )
            return df
        st.session_state.pop("_manual_schedule_override", None)
    elif override is not None:
        # The grid rows changed since the reshape was made — it described a
        # plan that no longer exists, so fall back to the freshly built one.
        st.session_state.pop("_manual_schedule_override", None)
    if base_df is not None and not base_df.empty:
        _auto_spread_dates(
            base_df, machine_df, acc_df, location, target_month_hint=plan_month,
        )
    return base_df


def _has_active_schedule(uploaded_file=None) -> bool:
    """True when 💾 Save schedule has something to save in upload mode — see
    :func:`_active_schedule_bytes`. Cheap: never converts a workbook."""
    if st.session_state.get("_loaded_schedule_buffer") is not None:
        return True
    if (
        uploaded_file is not None
        and _upload_sig(uploaded_file) != st.session_state.get("_seen_upload_sig")
    ):
        return True
    if st.session_state.get("_active_schedule_error"):
        # The last upload couldn't be read; nothing analysable to push.
        return False
    return st.session_state.get("_active_schedule_csv") is not None


def _active_schedule_bytes(uploaded_file=None) -> "bytes | None":
    """CSV bytes of the schedule the app is analysing in upload mode — the
    same precedence ``_load_schedule_df`` applies, without consuming anything:
    a reshape waiting in the one-shot buffer, then a file the uploader holds
    that the loader hasn't promoted yet (converted to CSV the same way), then
    the persisted active schedule. ``None`` when there is nothing analysable —
    e.g. the only upload so far had no usable columns."""
    buf = st.session_state.get("_loaded_schedule_buffer")
    if buf is not None:
        return buf.getvalue()
    if (
        uploaded_file is not None
        and _upload_sig(uploaded_file) != st.session_state.get("_seen_upload_sig")
    ):
        csv_bytes, _sheet = upload_to_schedule_csv(
            getattr(uploaded_file, "name", ""), uploaded_file.getvalue(),
        )
        return csv_bytes
    return st.session_state.get("_active_schedule_csv")


# ---------------------------------------------------------------------------
# Month-aware week buckets
# ---------------------------------------------------------------------------
# ``WEEK_OF_MONTH`` is the Monday-anchored week index within a row's OWN month,
# so a carryover row dated in an earlier month shares its number with this
# month's rows. Every week filter therefore keys on (year, month, week) derived
# from PRODUCTION DAY — never on the week number alone, which used to merge e.g.
# September's week 2 into October's.
def _month_weeks(year: int, month: int) -> list[int]:
    """Week numbers of (year, month) that contain at least one M–F working day:
    four to six of them (e.g. ``[2, 3, 4, 5, 6]`` for a 31-day month that
    starts on a weekend), never a hardcoded 1–5."""
    return sorted({week_of_month(d) for d in _working_days_in_month(year, month)})


def _month_week_mask(df, year: int, month: int, week: int) -> pd.Series:
    """Boolean mask of the rows whose PRODUCTION DAY falls in week ``week`` of
    (year, month). Undated rows never match."""
    if df is None or df.empty or "PRODUCTION DAY" not in df.columns:
        return pd.Series(False, index=(df.index if df is not None else None), dtype=bool)
    days = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce")
    weeks = pd.Series(week_of_month_series(days), index=df.index)
    mask = (
        (days.dt.year == int(year))
        & (days.dt.month == int(month))
        & (weeks == int(week))
    )
    return mask.fillna(False).astype(bool)


def _month_week_keys(df) -> list[tuple[int, int, int]]:
    """Sorted distinct (year, month, week) buckets of the dated rows in ``df``."""
    if df is None or df.empty or "PRODUCTION DAY" not in df.columns:
        return []
    days = pd.to_datetime(df["PRODUCTION DAY"], errors="coerce").dropna()
    return sorted({(int(d.year), int(d.month), int(week_of_month(d))) for d in days})


def _month_week_label(year: int, month: int, week: int, with_month: bool) -> str:
    """Heatmap column label: ``Wk 3`` within one month, ``Sep Wk 5`` when the
    schedule's dated rows span more than one month."""
    return f"{calendar.month_abbr[int(month)]} Wk {week}" if with_month else f"Wk {week}"


def _first_working_day_of_week(year: int, month: int, week: int) -> pd.Timestamp:
    """First M–F working day of week ``week`` in (year, month); the month's
    first working day when that week has none (a weekend-only week 1)."""
    working = _working_days_in_month(year, month)
    for d in working:
        if week_of_month(d) == int(week):
            return pd.Timestamp(d)
    return pd.Timestamp(working[0]) if working else pd.Timestamp(date(year, month, 1))


def _calendar_month_slice(work: pd.DataFrame, plan_month=None):
    """Restrict ``work`` (rows with a real PRODUCTION DAY) to ONE month — the
    plan month, or, when none is given, the month most of the rows fall in —
    and return ``(sliced, (year, month))``. The Calendar's week cards are keyed
    on week-of-month, so letting two months through would merge a September
    carryover row's week 2 into October's."""
    if plan_month is not None:
        ym = (int(plan_month[0]), int(plan_month[1]))
    else:
        ym = (
            int(work["PRODUCTION DAY"].dt.year.mode()[0]),
            int(work["PRODUCTION DAY"].dt.month.mode()[0]),
        )
    sliced = work[
        (work["PRODUCTION DAY"].dt.year == ym[0])
        & (work["PRODUCTION DAY"].dt.month == ym[1])
    ].copy()
    return sliced, ym


def _load_schedule_df(
    uploaded_file=None, location: str = "HENDERSON",
    target_month_hint: "tuple[int, int] | None" = None,
) -> pd.DataFrame:
    """Schedule loader — accepts an uploaded CSV, a saved-schedule buffer, the
    facility's newest saved schedule, or falls back to the bundled May 2026
    sample.

    Resolution order:
      1. A one-shot ``st.session_state['_loaded_schedule_buffer']`` — produced
         by "📂 Load saved schedule", a Sequencer Accept, a 🗓 Sequence Apply,
         a Bulk Move, a 🧹 Suffix Strip, or 📅 Move into plan month. Consumed
         once, then **promoted** to the persistent active schedule (below).
         (In ✏️ manual mode the same buffer is consumed by
         :func:`_resolve_manual_schedule` instead.)
      2. A genuinely new ``uploaded_file`` from ``st.file_uploader`` (detected
         via :func:`_upload_sig`). Becomes the persistent active schedule.
      3. The persistent ``st.session_state['_active_schedule_csv']`` — whatever
         was loaded last. This is what lets the schedule survive reruns from
         unrelated widgets (e.g. the efficiency slider) instead of resetting.
         An *automatic* pick (step 3c) is bound to the facility it was picked
         for: a facility switch drops it and re-resolves for the new facility,
         while anything the planner chose (upload, 📥 Load, a reshape) stays.
      3c. First load for a facility — nothing uploaded, nothing active: the
         facility's newest saved schedule (``saved_at``, the 📂 picker's own
         order), promoted to the active schedule exactly like a 📥 Load, so a
         fresh session opens on the team's real plan instead of the sample.
      4. The bundled ``data/may_schedule.csv`` — only for a facility with no
         saved schedules.

    Passes file-like content as ``io.BytesIO`` so no temp file is written to
    disk — protects against race conditions when multiple users upload
    simultaneously and preserves the privacy guarantee that browser-only
    uploads stay browser-only unless the planner explicitly saves them.

    On a successful load the function stashes a tiny summary dict in
    ``st.session_state['_last_upload_summary']`` so the sidebar can render
    a "✅ Loaded N rows from X, month Y" confirmation.

    Re-raises ``ScheduleColumnError`` so the caller can surface a clean,
    planner-friendly message via ``st.warning``.
    """
    machine_skus = set(_load_machine_df(_csv_mtime("machine_clean.csv"))["SKU"])

    # MODEL TYPE → estimation-placeholder SKU map, so the loader can rescue
    # planned units that arrive with a BUILD QTY but a blank FG SKU ID (e.g. a
    # make-to-order hybrid whose finished-good SKU isn't generated yet) instead
    # of dropping them. Built from the catalog's `XXX` placeholder rows.
    placeholder_map = build_placeholder_map(
        _load_machine_df(_csv_mtime("machine_clean.csv"))
    )

    # Auto-spread needs the full machine + accessory frames so it can weight
    # each row by labor (per-station p-min × qty) before running LPT. Both
    # come from the same cached loaders the rest of main() uses. Project the
    # facility-specific routing so capacity-aware spreading sees the right
    # station mapping for this plant.
    machine_df_full = _project_facility_routing(
        _load_machine_df(_csv_mtime("machine_clean.csv")), location,
    )
    acc_df_full = _load_acc_df(_csv_mtime("acc_clean.csv"))
    # Accessory SKU set for auto-stripping customer-decal suffixes on load.
    acc_skus = set(acc_df_full["SKU"].astype(str))

    # Rows tagged with a *different* facility: a per-plant export sometimes uses
    # LOCATION for where the unit is headed rather than where it's assembled, so
    # they're this plant's work. Sidebar checkbox (default on); what it actually
    # moved is reported back so the planner sees it.
    _absorb_foreign = bool(st.session_state.get("absorb_foreign_loc", True))

    # The Plan-month picker is authoritative — show it on the confirmation pill
    # (and use it for auto-spread) rather than the CSV's own PRODUCTION MONTH.
    _plan_label = ""
    if target_month_hint:
        _py, _pm = target_month_hint
        _plan_label = f"{calendar.month_name[_pm]} {_py}"

    def _finalize(df: pd.DataFrame, source: str) -> pd.DataFrame:
        """Common post-load step: auto-strip customer-decal suffixes from the
        displayed SKU columns, auto-spread blank PRODUCTION DAY rows so the
        weekly heatmap has something to chart, then stash the summary."""
        # Capture the placeholder-recovery summary now, before the suffix-strip
        # / auto-spread steps below rebuild the frame and drop .attrs.
        _recovered = (
            df.attrs.get("placeholder_recovered")
            if df is not None and hasattr(df, "attrs") else None
        )
        _unscheduled = (
            df.attrs.get("unscheduled_recovered")
            if df is not None and hasattr(df, "attrs") else None
        )
        _location = (
            df.attrs.get("location_recovered")
            if df is not None and hasattr(df, "attrs") else None
        )
        _foreign = (
            df.attrs.get("foreign_location_recovered")
            if df is not None and hasattr(df, "attrs") else None
        )
        # Row accounting for the "Loaded N of M rows" pill (see load_schedule).
        _row_counts = (
            {k: df.attrs.get(k)
             for k in ("source_rows", "no_qty_rows", "other_location_rows")}
            if df is not None and hasattr(df, "attrs") else None
        )
        if df is not None and not df.empty:
            # Auto-clean customer-decal suffixes (e.g. BOSS70-001HRC →
            # BOSS70-001) on every load so the suffixed text never leaks into
            # the Sequence editor, Calendar, or downloaded CSV. Idempotent —
            # a no-op on already-clean data. Orphan suffixes (base not in
            # catalog) are intentionally left untouched by the helper.
            df, _n_fg, _n_acc = _strip_customer_suffixes_from_schedule(
                df, machine_skus, acc_skus,
            )
            _auto_spread_dates(
                df, machine_df_full, acc_df_full, location,
                target_month_hint=target_month_hint,
            )
        _stash_upload_summary(df, location, source=source,
                              plan_month_label=_plan_label, recovered=_recovered,
                              unscheduled=_unscheduled, location_recovered=_location,
                              foreign_location=_foreign,
                              sheet=st.session_state.get("_active_schedule_sheet"),
                              row_counts=_row_counts,
                              name=st.session_state.get("_active_schedule_name"),
                              auto_loaded=bool(
                                  st.session_state.get("_active_schedule_auto_for")))
        return df

    # Resolution order. NOTE: the previously-loaded schedule must survive
    # unrelated reruns (e.g. the planner nudging the efficiency slider). We
    # persist the active schedule's CSV bytes in ``_active_schedule_csv`` so a
    # rerun re-resolves to the *same* schedule instead of falling back to the
    # uploader / bundled sample — that fallback was the "save schedule keeps
    # resetting" bug.

    # 1) One-shot action buffer — a saved-schedule load, a Sequencer Accept,
    #    a 🗓 Sequence Apply, a Bulk Move, or a 🧹 Suffix Strip just happened.
    #    Consume it and promote it to the persistent active schedule.
    buf = st.session_state.pop("_loaded_schedule_buffer", None)
    if buf is not None:
        csv_bytes = buf.getvalue()
        kind = st.session_state.pop("_loaded_schedule_kind", None)
        label = st.session_state.pop("_loaded_schedule_label", None)
        # Parse FIRST: a buffer the loader can't read (a saved file that
        # went bad) must not replace the schedule that is active now.
        df = load_schedule(
            io.BytesIO(csv_bytes), location=location, machine_skus=machine_skus,
            placeholder_map=placeholder_map, acc_skus=acc_skus,
            absorb_foreign_locations=_absorb_foreign,
        )
        prev_csv = st.session_state.get("_active_schedule_csv")
        prev_source = st.session_state.get("_active_schedule_source")
        prev_name = st.session_state.get("_active_schedule_name")
        edits = _schedule_edit_count()
        if kind == "reshape":
            # A reshape keeps the source and name of the schedule it reshaped
            # (it is not a "saved schedule" just because it came through the
            # buffer) and leaves the previous bytes behind as the undo step.
            source = prev_source or "saved"
            name = prev_name
            st.session_state.pop("_loaded_schedule_source", None)
            st.session_state.pop("_loaded_schedule_name", None)
            if prev_csv is not None:
                st.session_state["_schedule_undo"] = {
                    "csv": prev_csv, "label": label, "source": prev_source,
                    "name": prev_name,
                    "sheet": st.session_state.get("_active_schedule_sheet"),
                }
            st.session_state["_schedule_edit_count"] = edits + 1
        elif kind == "undo":
            source = st.session_state.pop("_loaded_schedule_source", prev_source or "saved")
            name = st.session_state.pop("_loaded_schedule_name", prev_name)
            st.session_state.pop("_schedule_undo", None)
            st.session_state["_schedule_edit_count"] = max(0, edits - 1)
        elif kind == "restore":
            source = st.session_state.pop("_loaded_schedule_source", prev_source or "saved")
            name = st.session_state.pop("_loaded_schedule_name", prev_name)
            st.session_state.pop("_schedule_undo", None)
            st.session_state["_schedule_edit_count"] = 0
        else:
            # A fresh load — 📥 Load, "load newest", "use the sample": names
            # the schedule, starts the edit history over, and is what "⏮ back
            # to as loaded" returns to.
            source = st.session_state.pop("_loaded_schedule_source", "saved")
            name = st.session_state.pop("_loaded_schedule_name", prev_name)
            st.session_state.pop("_schedule_undo", None)
            st.session_state["_schedule_edit_count"] = 0
            st.session_state["_schedule_original"] = {
                "csv": csv_bytes, "source": source, "name": name, "sheet": None,
            }
        st.session_state["_active_schedule_csv"] = csv_bytes
        st.session_state["_active_schedule_source"] = source
        # The planner chose this schedule (or chose to go back to it), so it
        # is no longer the facility-bound automatic first pick (step 3c).
        st.session_state["_active_schedule_name"] = name
        st.session_state.pop("_active_schedule_auto_for", None)
        # A programmatic load is always CSV — no workbook sheet to report,
        # and any earlier failed-upload banner is superseded.
        st.session_state["_active_schedule_sheet"] = None
        st.session_state.pop("_active_schedule_error", None)
        st.session_state.pop("_schedule_before_error", None)
        # A programmatic load supersedes whatever file is still sitting in the
        # uploader widget — mark it "seen" so the next rerun doesn't treat it
        # as a fresh upload and clobber what we just loaded.
        if uploaded_file is not None:
            st.session_state["_seen_upload_sig"] = _upload_sig(uploaded_file)
        return _finalize(df, source)

    # 2) Live file upload — but only treat it as authoritative when it's a
    #    *new* file. The uploader keeps returning the same object across
    #    reruns, so without the signature check a stale upload would override
    #    a saved schedule the planner loaded afterwards.
    if uploaded_file is not None:
        sig = _upload_sig(uploaded_file)
        if sig != st.session_state.get("_seen_upload_sig"):
            st.session_state["_seen_upload_sig"] = sig
            # An Excel workbook is converted to CSV bytes here, once, so every
            # downstream consumer (the persisted active schedule, 💾 Save
            # schedule, Sequence / Calendar) keeps seeing plain CSV. A CSV
            # passes through untouched.
            try:
                csv_bytes, sheet = upload_to_schedule_csv(
                    getattr(uploaded_file, "name", ""), uploaded_file.getvalue(),
                )
                # Parse before promoting: bytes the loader can't read never
                # become the active schedule (they used to, and the same
                # exception then came back on every rerun).
                df = load_schedule(
                    io.BytesIO(csv_bytes), location=location, machine_skus=machine_skus,
                    placeholder_map=placeholder_map, acc_skus=acc_skus,
                    absorb_foreign_locations=_absorb_foreign,
                )
            except ScheduleColumnError as exc:
                # The file is unusable. Remember the failure so later reruns
                # (any widget nudge) keep showing the banner instead of
                # silently reviving the previous schedule — but keep that
                # previous schedule aside: "⏮ Back to the previous schedule"
                # and emptying the uploader both put it back.
                if st.session_state.get("_active_schedule_csv") is not None:
                    st.session_state["_schedule_before_error"] = {
                        "csv": st.session_state.get("_active_schedule_csv"),
                        "source": st.session_state.get("_active_schedule_source"),
                        "name": st.session_state.get("_active_schedule_name"),
                        "sheet": st.session_state.get("_active_schedule_sheet"),
                        "auto_for": st.session_state.get("_active_schedule_auto_for"),
                        "_schedule_undo": st.session_state.get("_schedule_undo"),
                        "_schedule_original": st.session_state.get("_schedule_original"),
                        "_schedule_edit_count": st.session_state.get("_schedule_edit_count"),
                    }
                for _k in ("_active_schedule_csv", "_active_schedule_name",
                           "_active_schedule_auto_for", "_schedule_undo",
                           "_schedule_original", "_schedule_edit_count"):
                    st.session_state.pop(_k, None)
                st.session_state["_active_schedule_sheet"] = None
                st.session_state["_active_schedule_error"] = str(exc)
                raise
            st.session_state.pop("_active_schedule_error", None)
            st.session_state.pop("_schedule_before_error", None)
            st.session_state["_active_schedule_csv"] = csv_bytes
            st.session_state["_active_schedule_source"] = "upload"
            st.session_state["_active_schedule_sheet"] = sheet
            _upload_name = getattr(uploaded_file, "name", None) or None
            st.session_state["_active_schedule_name"] = _upload_name
            st.session_state.pop("_active_schedule_auto_for", None)
            st.session_state.pop("_schedule_undo", None)
            st.session_state["_schedule_edit_count"] = 0
            st.session_state["_schedule_original"] = {
                "csv": csv_bytes, "source": "upload", "name": _upload_name,
                "sheet": sheet,
            }
            return _finalize(df, "upload")
        # Same file as before → fall through to the persisted active schedule.

    # 2b) The failed upload has left the uploader: the error has nothing to
    #     describe any more. Put back what was active before it (or let 3c /
    #     4 resolve afresh) instead of keeping the banner up for a file that
    #     is gone while the sidebar talks about the bundled sample.
    if uploaded_file is None and st.session_state.get("_active_schedule_error"):
        st.session_state.pop("_seen_upload_sig", None)
        _restore_schedule_before_error()

    # 3) Persisted active schedule — survives reruns from slider/widget changes.
    #    An automatic first pick (3c) belongs to the facility it was picked
    #    for: switching facility drops it so the new facility gets its own
    #    newest save (or the sample). A schedule the planner chose stays put
    #    across a facility switch, as it always has.
    active = st.session_state.get("_active_schedule_csv")
    _auto_for = st.session_state.get("_active_schedule_auto_for")
    if active is not None and _auto_for and _auto_for != location:
        for _k in ("_active_schedule_csv", "_active_schedule_source",
                   "_active_schedule_name", "_active_schedule_auto_for",
                   "_schedule_undo", "_schedule_original", "_schedule_edit_count"):
            st.session_state.pop(_k, None)
        active = None
    if active is not None:
        df = load_schedule(
            io.BytesIO(active), location=location, machine_skus=machine_skus,
            placeholder_map=placeholder_map, acc_skus=acc_skus,
            absorb_foreign_locations=_absorb_foreign,
        )
        return _finalize(df, st.session_state.get("_active_schedule_source", "saved"))

    # 3b) The last upload was a workbook with no usable sheet — keep saying so
    #     (the banner is the planner's only cue to fix the file) rather than
    #     quietly falling back to the bundled sample.
    _pending_error = st.session_state.get("_active_schedule_error")
    if _pending_error:
        raise ScheduleColumnError(_pending_error)

    # 3c) First load for this facility — nothing uploaded, nothing loaded yet.
    #     Start from the facility's newest saved schedule (the 📂 picker's
    #     default) rather than the bundled sample, promoted to the active
    #     schedule exactly as a 📥 Load would be, and tagged as this
    #     facility's automatic pick. Happens once: from here on step 3 serves
    #     it, so a mode switch never re-triggers it, and an upload / 📥 Load /
    #     reshape replaces it for good. The Plan month is NOT touched — an
    #     August file opened in October gets the off-month note and the
    #     "📆 Plan August 2026 instead" button, never a silent switch.
    if uploaded_file is None:
        _newest = _newest_saved_schedule(location)
        if _newest is not None:
            _name, _csv_text = _newest
            csv_bytes = _csv_text.encode("utf-8")
            st.session_state["_active_schedule_csv"] = csv_bytes
            st.session_state["_active_schedule_source"] = "saved"
            st.session_state["_active_schedule_sheet"] = None
            st.session_state["_active_schedule_name"] = _name
            st.session_state["_active_schedule_auto_for"] = location
            st.session_state.pop("_active_schedule_error", None)
            st.session_state.pop("_schedule_undo", None)
            st.session_state["_schedule_edit_count"] = 0
            st.session_state["_schedule_original"] = {
                "csv": csv_bytes, "source": "saved", "name": _name, "sheet": None,
            }
            try:
                df = load_schedule(
                    io.BytesIO(csv_bytes), location=location,
                    machine_skus=machine_skus, placeholder_map=placeholder_map,
                    acc_skus=acc_skus, absorb_foreign_locations=_absorb_foreign,
                )
            except ScheduleColumnError:
                # A saved file the loader can't read must not poison the
                # session the way a malformed upload deliberately does — the
                # planner never chose it. Fall back to the sample.
                for _k in ("_active_schedule_csv", "_active_schedule_source",
                           "_active_schedule_sheet", "_active_schedule_name",
                           "_active_schedule_auto_for", "_schedule_original",
                           "_schedule_edit_count"):
                    st.session_state.pop(_k, None)
            else:
                _track_and_flush(
                    "schedule_autoload_saved", facility=location, name=_name,
                )
                return _finalize(df, "saved")

    # 4) Bundled sample — a facility with no saved schedules.
    df = load_schedule(
        location=location, machine_skus=machine_skus,
        placeholder_map=placeholder_map, acc_skus=acc_skus,
        absorb_foreign_locations=_absorb_foreign,
    )
    return _finalize(df, "bundled")


def _newest_saved_schedule(location: str) -> "tuple[str, str] | None":
    """``(name, csv_text)`` of the facility's most recently saved schedule —
    the 📂 picker's default — or None when there is none worth loading."""
    all_schedules = _load_uploaded_schedules_dict(_csv_mtime("uploaded_schedules.json"))
    by_loc = all_schedules.get(location, {}) if isinstance(all_schedules, dict) else {}
    for name in names_newest_first(by_loc):
        block = by_loc.get(name)
        csv_text = block.get("csv_content", "") if isinstance(block, dict) else ""
        if isinstance(csv_text, str) and csv_text.strip():
            return name, csv_text
    return None


def _stash_upload_summary(df: pd.DataFrame, location: str, source: str,
                          plan_month_label: str = "", recovered: dict | None = None,
                          unscheduled: dict | None = None,
                          location_recovered: dict | None = None,
                          foreign_location: dict | None = None,
                          sheet: str | None = None,
                          row_counts: dict | None = None,
                          name: str | None = None,
                          auto_loaded: bool = False) -> None:
    """Save a one-line description of the just-loaded schedule into session
    state so the sidebar can echo it back to the planner as a green
    confirmation pill. No-op when the DataFrame is empty (caller already
    surfaces an empty-state banner).

    ``plan_month_label`` is the authoritative Plan-month chosen in the sidebar
    (e.g. "June 2026"). When set it's what the pill shows, since that month
    drives the Calendar + auto-spread — even if the CSV's own PRODUCTION MONTH
    column says something else (which we keep as ``csv_month`` for context).

    ``row_counts`` carries the loader's ``source_rows`` / ``no_qty_rows`` /
    ``other_location_rows`` so the pill can say "Loaded 277 of 288 rows" and
    account for the gap. Also records the schedule's own months under
    :data:`SCHEDULE_MONTHS_KEY` for the Plan-month picker. ``name`` is the
    saved schedule's name or the uploaded file's name, shown on the pill;
    ``auto_loaded`` marks the facility's newest save picked automatically on
    first load (step 3c of :func:`_load_schedule_df`)."""
    try:
        if df is None or df.empty:
            st.session_state.pop("_last_upload_summary", None)
            st.session_state.pop(SCHEDULE_MONTHS_KEY, None)
            return
        # The schedule's own month(s): PRODUCTION MONTH weighted by units, not
        # PRODUCTION DAY (auto-spread has just filled that with the Plan
        # month). The dominant one is the pill's "CSV says …" and what the
        # "📆 Plan <month> instead" button offers; every month the file is
        # dated in stays selectable in the picker.
        by_month = _schedule_month_units(df)
        dominant = _schedule_dominant_month(df)
        st.session_state[SCHEDULE_MONTHS_KEY] = sorted(by_month)
        if dominant is not None:
            csv_month = _month_label(dominant)
        else:
            # Nothing parsed as a month — show the first raw value so the
            # planner still sees what the file says.
            csv_month = (
                str(df["PRODUCTION MONTH"].iloc[0])
                if "PRODUCTION MONTH" in df.columns else ""
            )
        _rc = row_counts if isinstance(row_counts, dict) else {}
        _src_rows = _rc.get("source_rows")
        st.session_state["_last_upload_summary"] = {
            "rows": int(len(df)),
            "source_rows": int(_src_rows) if _src_rows is not None else None,
            "no_qty_rows": int(_rc.get("no_qty_rows") or 0),
            "other_location_rows": int(_rc.get("other_location_rows") or 0),
            "location": str(location),
            "month": str(plan_month_label or csv_month or "—"),
            "csv_month": str(csv_month or ""),
            "schedule_month": dominant,
            "source": source,
            "auto_spread": (df.attrs.get("auto_spread")
                            if hasattr(df, "attrs") else None),
            "recovered": recovered,
            "unscheduled": unscheduled,
            "location_recovered": location_recovered,
            "foreign_location": foreign_location,
            # Workbook sheet the upload was read from (None for a CSV) — shown
            # on the confirmation pill so the planner can spot a wrong tab.
            "sheet": sheet,
            "name": str(name) if name else None,
            "auto_loaded": bool(auto_loaded),
        }
    except Exception:
        st.session_state.pop("_last_upload_summary", None)


# =============================================================
# Sequencer — Suggest button + Accept/Discard proposal flow
# =============================================================
def _render_sequencer_controls(
    schedule_df: pd.DataFrame, machine_df, acc_df, inputs: dict,
) -> pd.DataFrame:
    """Top-of-page control bar for the Sequencer.

    Provides a 💡 Suggest button (level-loaded re-spread that respects
    per-station weekly capacity) plus accept/discard buttons when a
    proposal is pending. Returns the schedule_df that should drive the
    rest of main() — either the original, or the accepted proposal.

    Pending proposal state lives in ``st.session_state["_proposed_schedule"]``
    and ``st.session_state["_proposed_schedule_overloads"]``.
    """
    # "💡 Level-load the month" in the plan guide leaves this flag and reruns.
    _wants_proposal = bool(st.session_state.pop("_guide_generate_proposal", False))
    if schedule_df is None or schedule_df.empty:
        return schedule_df

    # If a proposal is pending, render the accept/discard bar and the
    # diagnosis. Otherwise generate one when the guide asked for it.
    proposal = st.session_state.get("_proposed_schedule")
    overloads = st.session_state.get("_proposed_schedule_overloads", [])

    if isinstance(proposal, pd.DataFrame) and not proposal.empty:
        meta = proposal.attrs.get("suggested", {})
        with st.container():
            st.info(
                f"💡 **Proposed level-loaded schedule** — {meta.get('total_rows', '?')} "
                f"units across {meta.get('working_days', '?')} working days of "
                f"**{meta.get('target_month_label', '?')}**. "
                "Review the comparison heatmap on the **🏠 Overview** tab. "
                "Accept to switch the analysis to this sequence, or Discard to keep your current one."
            )
            if overloads:
                _ol_summary = ", ".join(
                    f"{o['fg']} (Wk {o['week']}: {', '.join(o['stations'])})"
                    for o in overloads[:5]
                )
                if len(overloads) > 5:
                    _ol_summary += f" … (+{len(overloads) - 5} more)"
                st.warning(
                    f"⚠️ **{len(overloads)} unit(s) couldn't fit without overload:** "
                    f"{_ol_summary}."
                )

                # Real-talk diagnosis: name the binding station(s),
                # quantify the gap, propose concrete fixes.
                diagnoses = _diagnose_overloads(
                    proposal, machine_df, acc_df, inputs, overloads,
                )
                if diagnoses:
                    st.markdown("##### 🛠 How to fix this")
                    for d in diagnoses:
                        with st.container():
                            st.markdown(
                                f"**Bottleneck: {d['station_display']}** "
                                f"— short **{_fmt_min_hr(d['shortfall'])}** "
                                f"({d['shortfall_pct']*100:+.0f}% over capacity)"
                            )
                            st.caption(
                                f"Monthly demand: **{_fmt_min_hr(d['demand'])}** "
                                f"· Capacity at current settings: "
                                f"**{_fmt_min_hr(d['capacity'])}**"
                            )

                            colA, colB, colC = st.columns(3)
                            with colA:
                                st.markdown(
                                    f"🧑 **Hire** &nbsp; +{d['hire_hc']} HC"
                                )
                                st.caption(
                                    f"at {d['station_display']}. Bring weekly "
                                    "capacity up by enough to absorb the gap."
                                )
                            with colB:
                                st.markdown(
                                    f"📅 **Extend** &nbsp; +{d['extend_days']} day(s)"
                                )
                                st.caption(
                                    "Run overtime / Saturdays. Keeps current HC."
                                )
                            with colC:
                                defer_label = "🚚 **Defer** "
                                if d["defer_units"]:
                                    deferred_total = sum(u["qty"] for u in d["defer_units"])
                                    defer_label += f"&nbsp; {deferred_total} unit(s)"
                                    st.markdown(defer_label)
                                    _names = ", ".join(
                                        f"{u['fg']} ×{u['qty']}"
                                        for u in d["defer_units"][:5]
                                    )
                                    if len(d["defer_units"]) > 5:
                                        _names += f" … (+{len(d['defer_units']) - 5} more)"
                                    st.caption(
                                        f"Push heaviest units to next month: "
                                        f"{_names}. Frees "
                                        f"~{_fmt_min_hr(d['freed_by_defer'])}."
                                    )
                                else:
                                    st.markdown(defer_label + "&nbsp; —")
                                    st.caption("No clear deferral candidates.")
                            st.markdown("")  # spacer between stations
                    st.caption(
                        "Pick one of the three options above, change the "
                        "matching sidebar setting (or the schedule), and "
                        "click **💡 Level-load the month** in the plan guide "
                        "again. Or hit ✅ Accept to live with the overload."
                    )
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("✅ Accept proposal", width="stretch", type="primary"):
                # Replace the working schedule and clear proposal state.
                accepted = st.session_state.pop("_proposed_schedule")
                st.session_state.pop("_proposed_schedule_overloads", None)
                # Push the accepted CSV through the upload buffer so the
                # standard loader path picks it up on the next rerun.
                _commit_reshape(
                    accepted, "Accepted the level-loaded proposal",
                    event="sequencer_accept",
                    facility=inputs.get("location"),
                    rows=int(len(accepted)),
                    overloads=len(overloads),
                )
                st.rerun()
            if c2.button("🚫 Discard", width="stretch"):
                st.session_state.pop("_proposed_schedule", None)
                st.session_state.pop("_proposed_schedule_overloads", None)
                _track_and_flush("sequencer_discard", facility=inputs.get("location"))
                st.rerun()
        return schedule_df

    # No proposal pending. The entry point is the plan guide's "💡 Level-load
    # the month" (step 4); it leaves a one-shot flag, and the proposal is
    # generated here — the same place its Accept / Discard panel is drawn —
    # so the whole Sequencer flow lives in one spot.
    if _wants_proposal and _generate_proposal(schedule_df, machine_df, acc_df, inputs):
        st.rerun()
    return schedule_df


def _generate_proposal(schedule_df, machine_df, acc_df, inputs: dict) -> bool:
    """Run the constraint-aware LPT scheduler over ``schedule_df`` and store
    the result as the pending proposal (``_proposed_schedule`` and its
    overloads). False, with the error shown, when the suggester fails."""
    try:
        with st.spinner("Running constraint-aware LPT scheduler…"):
            new_df, ol = _suggest_schedule_dates(schedule_df, machine_df, acc_df, inputs)
    except Exception as e:
        st.error(f"❌ Suggester failed: {e}")
        return False
    st.session_state["_proposed_schedule"] = new_df
    st.session_state["_proposed_schedule_overloads"] = ol
    _track_and_flush(
        "sequencer_suggest",
        facility=inputs.get("location"), rows=int(len(new_df)), overloads=len(ol),
    )
    return True


def _render_sequencer_view(
    schedule_df: pd.DataFrame,
    plan_month: "tuple[int, int] | None" = None,
    plan_month_label: str = "",
    manual_mode: bool = False,
) -> None:
    """The 🗓 Sequence sub-section under Data & Setup.

    Two side-by-side controls that together form the manual-reshape UX:
      • Editable table with PRODUCTION DAY as a date-picker column. Edit
        rows inline, click Apply to write back into the session schedule.
      • Bulk move form: shift N units of SKU X from Week A → Week B of the
        plan month in one operation.

    Both write the reshaped CSV through ``st.session_state["_loaded_schedule_buffer"]``
    so the active-schedule resolver of the current sidebar mode picks it up
    on the next rerun and the analysis tabs immediately reflect the new
    sequence. ``manual_mode`` only changes the captions: in ✏️ manual mode the
    reshape is session-only (a saved scenario keeps the SKU list, not dates).
    """
    st.subheader("🗓 Sequence — manual reshape")
    if schedule_df is None or schedule_df.empty:
        st.info(
            "No schedule loaded yet. Upload a CSV in the sidebar, then come "
            "back here to reshape the dates."
        )
        return

    if manual_mode:
        st.caption(
            "Edit individual `PRODUCTION DAY` cells in the table, OR use the "
            "**Bulk move** form below to shift many units at once. In "
            "**✏️ Paste or type SKUs** mode the reshaped dates live in your "
            "browser session only — a saved scenario keeps the SKU list, not "
            "the dates — and they're dropped if you change the SKU rows in the "
            "sidebar. Use **📥 Download** to keep a copy; to save it for the "
            "team, switch to **📤 Upload schedule file** mode, upload the "
            "download, and click 💾 Save schedule."
        )
    else:
        st.caption(
            "Edit individual `PRODUCTION DAY` cells in the table, OR use the "
            "**Bulk move** form below to shift many units at once. Changes "
            "live in your browser session until you save — **💾 Save schedule** "
            "in the sidebar's 📂 panel saves the reshaped plan for the team, "
            "or use **📥 Download** for a copy you can re-upload later."
        )

    # Reset button — toss any reshape edits so the table reflects the
    # canonical schedule again. The edits live in the data_editor's widget
    # state (keyed below), NOT in a separate session key, so the only way to
    # clear them is to give the editor a NEW key. Bump a revision nonce that
    # feeds the editor key, forcing a fresh widget seeded from the canonical
    # schedule. (The old code popped "_sequencer_edits", a key nothing ever
    # wrote — so Revert silently did nothing.)
    if st.button("↩️ Revert to current sequence", help="Drops any unsaved reshape edits."):
        st.session_state["_seq_rev"] = int(st.session_state.get("_seq_rev", 0)) + 1
        st.rerun()

    # Choose which columns to expose in the editor. Keep the editable surface
    # narrow (date + qty) so we don't accidentally let the planner mangle
    # the FG/Accessory SKUs from this view (those have their own catalogs).
    display_cols = [
        c for c in ["FG SKU ID", "FG ACCRY SKU ID", "BUILD QTY",
                    "PRODUCTION DAY", "WEEK_OF_MONTH",
                    "CUSTOMER NAME", "PRODUCTION MONTH", "LOCATION"]
        if c in schedule_df.columns
    ]
    work = schedule_df[display_cols].copy()
    # Make sure PRODUCTION DAY is a real datetime so st.column_config.DateColumn
    # gets it as a Date.
    if "PRODUCTION DAY" in work.columns:
        work["PRODUCTION DAY"] = pd.to_datetime(work["PRODUCTION DAY"], errors="coerce")

    # Editor key = manual Revert nonce + a signature of the canonical schedule.
    # The signature auto-resets the editor whenever the underlying schedule is
    # REPLACED (a saved schedule loaded, a Sequencer proposal accepted, an Apply
    # committed) — without it, the constant key let stale positional edits
    # overlay onto different rows of a new schedule. Editing a cell doesn't
    # change `work`, so the key stays stable and in-progress edits persist.
    _seq_rev = int(st.session_state.get("_seq_rev", 0))
    try:
        _seq_sig = int(pd.util.hash_pandas_object(work, index=False).sum() & 0xFFFFFFFF)
    except Exception:
        _seq_sig = len(work)
    _seq_key = f"sequencer_editor_v{_seq_rev}_{_seq_sig}"

    col_cfg = {}
    if "FG SKU ID" in work.columns:
        col_cfg["FG SKU ID"] = st.column_config.TextColumn("FG SKU", disabled=True)
    if "FG ACCRY SKU ID" in work.columns:
        col_cfg["FG ACCRY SKU ID"] = st.column_config.TextColumn(
            "Accessory SKU", disabled=True,
        )
    if "BUILD QTY" in work.columns:
        col_cfg["BUILD QTY"] = st.column_config.NumberColumn(
            "Qty", min_value=0, step=1,
            help="Edit if you want to scale a row up/down.",
        )
    if "PRODUCTION DAY" in work.columns:
        col_cfg["PRODUCTION DAY"] = st.column_config.DateColumn(
            "Production Day",
            help="Click the cell and pick a new date.",
        )
    if "WEEK_OF_MONTH" in work.columns:
        col_cfg["WEEK_OF_MONTH"] = st.column_config.NumberColumn(
            "Week", disabled=True,
            help="Derived from Production Day after you click Apply.",
        )
    if "CUSTOMER NAME" in work.columns:
        col_cfg["CUSTOMER NAME"] = st.column_config.TextColumn(
            "Customer", disabled=True,
        )
    if "PRODUCTION MONTH" in work.columns:
        col_cfg["PRODUCTION MONTH"] = st.column_config.TextColumn(
            "Prod Month", disabled=True,
        )
    if "LOCATION" in work.columns:
        col_cfg["LOCATION"] = st.column_config.TextColumn(
            "Location", disabled=True,
        )

    edited = st.data_editor(
        work,
        width="stretch",
        num_rows="fixed",
        column_config=col_cfg,
        key=_seq_key,
        height=480,
    )

    # Apply / Download
    ca, cb = st.columns([1, 1])
    if ca.button(
        "🔄 Apply edits",
        width="stretch",
        type="primary",
        help=(
            "Writes the edited rows back into the session schedule so all "
            "analysis tabs use the new sequence."
        ),
    ):
        _apply_sequencer_edits(schedule_df, edited)

    # Build the downloadable CSV with the edits applied (without committing
    # to session state until Apply is clicked).
    try:
        download_df = _merge_sequencer_edits_into_full(schedule_df, edited)
        cb.download_button(
            "📥 Download reshaped schedule",
            download_df.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_reshaped_{today_local_str()}.csv",
            mime="text/csv",
            width="stretch",
            help=(
                "Saves the current view (including unsaved edits) as a CSV "
                "you can re-upload later."
                + (
                    " In manual mode this is how you keep a reshape."
                    if manual_mode else
                    " (💾 Save schedule in the 📂 panel saves the applied "
                    "plan directly.)"
                )
            ),
        )
    except Exception:
        # If merging fails for any reason, fall back to the canonical schedule.
        cb.download_button(
            "📥 Download current schedule",
            schedule_df.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_{today_local_str()}.csv",
            mime="text/csv",
            width="stretch",
        )

    st.markdown("---")
    _render_bulk_move_form(
        schedule_df, plan_month=plan_month, plan_month_label=plan_month_label,
    )


def _merge_sequencer_edits_into_full(
    base_df: pd.DataFrame, edited: pd.DataFrame,
) -> pd.DataFrame:
    """Overlay the columns the editor allowed onto ``base_df`` and return
    the result. Used both by the Apply path and the Download button."""
    out = base_df.copy()
    overlay_cols = [
        c for c in ["PRODUCTION DAY", "BUILD QTY"]
        if c in edited.columns and c in out.columns
    ]
    for c in overlay_cols:
        out[c] = edited[c].values  # editor row-count is fixed; alignment is safe
    # Recompute WEEK_OF_MONTH from the new PRODUCTION DAY
    if "PRODUCTION DAY" in out.columns:
        new_days = pd.to_datetime(out["PRODUCTION DAY"], errors="coerce")
        out["PRODUCTION DAY"] = new_days
        out["WEEK_OF_MONTH"] = week_of_month_series(new_days)
    return out


def _apply_sequencer_edits(base_df: pd.DataFrame, edited: pd.DataFrame) -> None:
    """Commit the editor's changes back into the session schedule.

    The mechanism: serialize the merged DataFrame to CSV bytes and stash
    them in ``_loaded_schedule_buffer`` — the same one-shot path the
    Sequencer's Accept proposal flow uses. Next rerun, the standard loader
    picks the buffer up and the analysis tabs pick up the new dates.
    """
    try:
        merged = _merge_sequencer_edits_into_full(base_df, edited)
        _commit_reshape(
            merged, "Applied the sequence edits",
            event="sequencer_apply", rows=int(len(merged)),
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Couldn't apply edits: {e}")


def _bulk_move_units(
    schedule_df: pd.DataFrame, sku: str, from_week: int, to_week: int, qty: int,
    year: int, month: int,
) -> "tuple[pd.DataFrame, int]":
    """Pure core of 🔀 Bulk move: move ``qty`` units of ``sku`` from week
    ``from_week`` to week ``to_week`` of (year, month). Returns
    ``(new_df, moved)``.

    Only rows DATED in that month's ``from_week`` are eligible — a carryover
    row from an earlier month that happens to share the week number is left
    alone — and the moved units land on the first working day of ``to_week``
    in that same month. Per-row BUILD QTY can be >1, so rows are consumed
    until the requested count is covered, splitting the last one when partial.
    """
    mask = (
        (schedule_df["FG SKU ID"].astype(str).str.strip() == str(sku).strip())
        & _month_week_mask(schedule_df, year, month, from_week)
    )
    eligible = schedule_df[mask]
    if eligible.empty:
        return schedule_df, 0

    target_day = _first_working_day_of_week(year, month, to_week)
    target_week = week_of_month(target_day)
    moved = 0
    new_df = schedule_df.copy()
    new_rows = []  # for any split (partial-move) cases
    for idx, row in eligible.iterrows():
        if moved >= qty:
            break
        row_qty = int(row.get("BUILD QTY", 0) or 0)
        if row_qty <= 0:
            continue
        take = min(row_qty, qty - moved)
        if take == row_qty:
            # Move the whole row by re-dating it to the target week.
            new_df.at[idx, "PRODUCTION DAY"] = target_day
            if "WEEK_OF_MONTH" in new_df.columns:
                new_df.at[idx, "WEEK_OF_MONTH"] = target_week
        else:
            # Split: keep `row_qty - take` in the original row, add a new
            # row with `take` units dated to the target week.
            new_df.at[idx, "BUILD QTY"] = row_qty - take
            split_row = row.copy()
            split_row["BUILD QTY"] = take
            split_row["PRODUCTION DAY"] = target_day
            if "WEEK_OF_MONTH" in new_df.columns:
                split_row["WEEK_OF_MONTH"] = target_week
            new_rows.append(split_row)
        moved += take

    if new_rows:
        new_df = pd.concat([new_df, pd.DataFrame(new_rows)], ignore_index=True)
    return new_df, moved


def _render_bulk_move_form(
    schedule_df: pd.DataFrame,
    plan_month: "tuple[int, int] | None" = None,
    plan_month_label: str = "",
) -> None:
    """Bulk move N units of an FG SKU from one week of the plan month to
    another. Weeks are the plan month's Monday-anchored calendar weeks (a
    month that starts on a weekend has six), and only rows dated in the plan
    month are movable — an off-month / carryover row goes through 📅 Move into
    plan month first."""
    st.markdown("**🔀 Bulk move**")

    if "PRODUCTION DAY" not in schedule_df.columns:
        st.info("Week info isn't available — your schedule has no PRODUCTION DAY.")
        return

    days = pd.to_datetime(schedule_df["PRODUCTION DAY"], errors="coerce")
    if plan_month is None:
        # Defensive — main() always passes the sidebar's Plan month. Fall back
        # to the month most dated rows are in, so weeks never mix months.
        dated = days.dropna()
        if dated.empty:
            st.info("No dated rows to move.")
            return
        _p = dated.dt.to_period("M").mode().iloc[0]
        plan_month = (int(_p.year), int(_p.month))
    year, month = int(plan_month[0]), int(plan_month[1])
    label = plan_month_label or f"{calendar.month_name[month]} {year}"

    st.caption(
        f"Shift a batch of units of the same FG SKU from one week of **{label}** "
        "to another. Useful when you want to even out a single problem week "
        "without editing rows one at a time. Weeks are the plan month's "
        "Monday-anchored calendar weeks; rows dated in another month aren't "
        f"movable here — use **📅 Move … into {label}** at the top of the page "
        "first."
    )

    in_month = (days.dt.year == year) & (days.dt.month == month)
    week_col = pd.Series(week_of_month_series(days), index=schedule_df.index)
    weeks_in_use = sorted(int(w) for w in week_col[in_month].dropna().unique())
    if not weeks_in_use:
        st.info(f"No rows are dated in {label} — nothing to move by week.")
        return
    # Target weeks follow the plan month's calendar (4–6 weeks), not a fixed 1–5.
    to_week_opts = sorted(set(_month_weeks(year, month)) | set(weeks_in_use))

    skus = sorted(
        schedule_df.loc[in_month, "FG SKU ID"].astype(str).str.strip().unique()
    ) if "FG SKU ID" in schedule_df.columns else []
    if not skus:
        return

    with st.form("bulk_move_form", clear_on_submit=False):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        chosen_sku = c1.selectbox("FG SKU", options=skus, key="bulk_move_sku")
        from_week = c2.selectbox(
            "From week", options=weeks_in_use, key="bulk_move_from",
        )
        to_week = c3.selectbox(
            "To week", options=to_week_opts, key="bulk_move_to",
        )
        qty = c4.number_input(
            "Units", min_value=1, step=1, value=1, key="bulk_move_qty",
        )
        submitted = st.form_submit_button(
            "🔀 Move",
            width="stretch",
        )

    if not submitted:
        return

    new_df, moved = _bulk_move_units(
        schedule_df, chosen_sku, int(from_week), int(to_week), int(qty), year, month,
    )
    if moved == 0:
        st.warning(
            f"No units of **{chosen_sku}** found in week {from_week} of {label}."
        )
        return

    # Commit through the same buffer the editor uses.
    _commit_reshape(
        new_df,
        f"Moved {moved} unit(s) of {chosen_sku} from week {from_week} to "
        f"week {to_week} of {label}",
        event="sequencer_bulk_move",
        sku=chosen_sku, from_week=from_week, to_week=to_week, units=moved,
    )
    st.rerun()


def _strip_one_sku(sku: str, valid_bases: set) -> "tuple[str, str]":
    """Return (cleaned_sku, suffix) for a single SKU.

    Strips a customer suffix from `sku` only when the resulting base
    exists in `valid_bases`. Mirrors `core/data_loader.collapse_customer_suffix`
    but takes an arbitrary base set so it works for FG *and* accessory
    catalogs.

    Returns the original SKU + empty suffix if no clean strip is possible.
    """
    s = str(sku or "").strip()
    if not s:
        return s, ""
    for sfx in CUSTOMER_SUFFIXES:
        if s.endswith(sfx):
            base = s[:-len(sfx)]
            if base in valid_bases:
                return base, sfx
    return s, ""


def _find_customer_suffixed_skus(
    schedule_df: pd.DataFrame, machine_skus: set, acc_skus: set,
) -> pd.DataFrame:
    """Detect FG / Accessory SKUs in the loaded schedule whose tail
    matches a CUSTOMER_SUFFIXES entry.

    Returns a small DataFrame with one row per unique (column, original-
    SKU) pair. Columns:
      Original  · Suffix  · Base  · Type (FG / Accessory)  · In catalog  · Rows
    """
    if schedule_df is None or schedule_df.empty:
        return pd.DataFrame()

    fg_col_candidates = ["FG SKU ID", "FG_RAW", "FG_BASE"]
    acc_col_candidates = ["FG ACCRY SKU ID", "ACC"]

    def _detect(col_candidates, valid_bases, type_label):
        out = []
        col = next((c for c in col_candidates if c in schedule_df.columns), None)
        if col is None:
            return out
        for sku, count in schedule_df[col].astype(str).str.strip().value_counts().items():
            base, sfx = _strip_one_sku(sku, valid_bases)
            if not sfx:
                continue  # no suffix detected → skip
            out.append({
                "Original": sku,
                "Suffix": sfx,
                "Base": base,
                "Type": type_label,
                "In catalog": "✓",  # _strip_one_sku only returns sfx when base is in valid_bases
                "Rows": int(count),
            })
        return out

    # Also catch the "suffix present but base NOT in catalog" case so the
    # planner sees it (and we leave those alone during the strip).
    def _detect_orphans(col_candidates, valid_bases, type_label):
        out = []
        col = next((c for c in col_candidates if c in schedule_df.columns), None)
        if col is None:
            return out
        for sku, count in schedule_df[col].astype(str).str.strip().value_counts().items():
            s = str(sku).strip()
            if not s:
                continue
            # A catalog SKU that merely ends in a suffix token (BOSS25-018ES
            # is its own catalog row) is already canonical: the analysis
            # matches it as-is, so it is neither an orphan nor something to
            # strip. Flagging it told the planner to "add the base SKU to the
            # catalog first" for a SKU that was never at risk.
            if s in valid_bases:
                continue
            for sfx in CUSTOMER_SUFFIXES:
                if s.endswith(sfx):
                    base = s[:-len(sfx)]
                    if base not in valid_bases:
                        out.append({
                            "Original": s,
                            "Suffix": sfx,
                            "Base": base,
                            "Type": type_label,
                            "In catalog": "✗",
                            "Rows": int(count),
                        })
                    break  # only flag once per SKU
        return out

    rows = []
    rows.extend(_detect(fg_col_candidates, machine_skus, "FG"))
    rows.extend(_detect(acc_col_candidates, acc_skus, "Accessory"))
    rows.extend(_detect_orphans(fg_col_candidates, machine_skus, "FG"))
    rows.extend(_detect_orphans(acc_col_candidates, acc_skus, "Accessory"))
    if not rows:
        return pd.DataFrame(columns=["Original", "Suffix", "Base", "Type", "In catalog", "Rows"])
    return pd.DataFrame(rows).sort_values(
        by=["In catalog", "Type", "Rows"], ascending=[False, True, False],
    ).reset_index(drop=True)


def _strip_customer_suffixes_from_schedule(
    schedule_df: pd.DataFrame, machine_skus: set, acc_skus: set,
) -> "tuple[pd.DataFrame, int, int]":
    """Return a copy of schedule_df with customer suffixes stripped from
    FG and Accessory SKU columns. Also returns (n_fg_stripped, n_acc_stripped)
    row counts for the UI.

    Only strips when the base SKU exists in the matching catalog — orphan
    suffixed SKUs (no catalog base) are left untouched so the analysis
    doesn't silently lose them.
    """
    if schedule_df is None or schedule_df.empty:
        return schedule_df, 0, 0

    out = schedule_df.copy()
    n_fg = 0
    n_acc = 0

    # FG side — there can be several FG-shaped columns; strip whichever exist.
    for col in ("FG SKU ID", "FG_RAW", "FG_BASE"):
        if col not in out.columns:
            continue
        new_vals = []
        for v in out[col].astype(str).str.strip():
            base, sfx = _strip_one_sku(v, machine_skus)
            if sfx:
                new_vals.append(base)
                if col == "FG SKU ID":
                    n_fg += 1
            else:
                new_vals.append(v)
        out[col] = new_vals

    # Accessory side
    for col in ("FG ACCRY SKU ID", "ACC"):
        if col not in out.columns:
            continue
        new_vals = []
        for v in out[col].astype(str).str.strip():
            base, sfx = _strip_one_sku(v, acc_skus)
            if sfx:
                new_vals.append(base)
                if col == "FG ACCRY SKU ID":
                    n_acc += 1
            else:
                new_vals.append(v)
        out[col] = new_vals

    return out, n_fg, n_acc


def _render_suffix_cleanup_view(
    schedule_df: pd.DataFrame, machine_df, acc_df, manual_mode: bool = False,
) -> None:
    """🧹 Customer suffix cleanup — sub-section UI.

    Surfaces suffixed FG / Accessory SKUs in the loaded schedule with
    their detected bases, plus one-click Strip / Download actions. Catalog
    rows are not touched — this is a *schedule display cleanup*, not a
    catalog editor.
    """
    st.subheader("🧹 Customer suffix cleanup")
    st.caption(
        "ERP exports often carry customer-decal suffixes "
        f"(**{', '.join(CUSTOMER_SUFFIXES)}**) on FG SKUs — e.g. "
        "`BOSS70-001HRC`, `BOSS25-006ES`. The labor analysis already "
        "strips these internally, but the suffixed text shows up in the "
        "Sequence editor, Calendar, and downloaded CSV. This tool lets "
        "you rewrite the loaded schedule to canonical base SKUs in one "
        "click. **Catalogs are not modified.**"
    )

    if schedule_df is None or schedule_df.empty:
        st.info(
            "No schedule loaded yet. Upload a CSV in the sidebar, then "
            "come back here to check for customer suffixes."
        )
        return

    # Build lookup sets from the catalogs.
    machine_skus = set(machine_df["SKU"].astype(str).str.strip()) if (
        machine_df is not None and "SKU" in machine_df.columns
    ) else set()
    acc_skus = set(acc_df["SKU"].astype(str).str.strip()) if (
        acc_df is not None and "SKU" in acc_df.columns
    ) else set()

    findings = _find_customer_suffixed_skus(schedule_df, machine_skus, acc_skus)
    _track_and_flush("suffix_cleanup_view", rows=int(len(findings)))

    if findings.empty:
        st.success(
            "✅ **Schedule already uses canonical SKUs.** No customer "
            "suffixes detected in either the FG or Accessory columns."
        )
        return

    # Show the detection panel.
    st.markdown("**Detected customer-suffixed SKUs:**")
    st.dataframe(findings, width="stretch", hide_index=True)

    in_catalog_count = int((findings["In catalog"] == "✓").sum())
    orphan_count = int((findings["In catalog"] == "✗").sum())
    if orphan_count:
        st.warning(
            f"⚠️ **{orphan_count} row(s) have a customer suffix but the "
            "base SKU isn't in the catalog** — those will be left alone "
            "by the Strip action so the analysis doesn't silently lose "
            "them. Add the base SKU to the catalog first if you want "
            "them stripped too."
        )

    cleaned, n_fg_preview, n_acc_preview = _strip_customer_suffixes_from_schedule(
        schedule_df, machine_skus, acc_skus,
    )

    c1, c2 = st.columns(2)
    if c1.button(
        f"🔄 Strip suffixes in loaded schedule "
        f"({n_fg_preview} FG, {n_acc_preview} accessory cells)",
        width="stretch", type="primary",
        disabled=(in_catalog_count == 0),
        help=(
            "Rewrites FG / Accessory SKU columns in your active schedule "
            "to their canonical base form. Pushes the cleaned schedule "
            "through the analysis pipeline so every tab refreshes on the "
            "next rerun. No GitHub commit."
        ),
    ):
        try:
            _commit_reshape(
                cleaned,
                f"Stripped suffixes from {n_fg_preview} FG and "
                f"{n_acc_preview} accessory cells",
                event="suffix_cleanup_strip",
                n_fg=n_fg_preview, n_acc=n_acc_preview,
            )
            st.rerun()
        except Exception as e:
            st.error(f"❌ Strip failed: {e}")

    try:
        c2.download_button(
            "📥 Download cleaned schedule",
            cleaned.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_cleaned_{today_local_str()}.csv",
            mime="text/csv",
            width="stretch",
            on_click=lambda: _track_and_flush(
                "suffix_cleanup_download",
                n_fg=n_fg_preview, n_acc=n_acc_preview,
            ),
            help=(
                "Saves the cleaned schedule as a CSV you can re-upload later."
                + (
                    " In manual mode this is how you keep the cleanup."
                    if manual_mode else
                    " (After 🔄 Strip, 💾 Save schedule in the 📂 panel saves "
                    "the cleaned plan directly.)"
                )
            ),
        )
    except Exception:
        # Defensive — the on_click track-and-flush may have edge cases.
        c2.download_button(
            "📥 Download cleaned schedule",
            cleaned.to_csv(index=False).encode("utf-8"),
            file_name=f"schedule_cleaned_{today_local_str()}.csv",
            mime="text/csv",
            width="stretch",
        )


def _render_calendar_view(
    schedule_df: pd.DataFrame, machine_df, acc_df,
    time_window: str = "Whole month",
    plan_month: "tuple[int, int] | None" = None,
    plan_month_label: str = "",
) -> None:
    """📆 Calendar — week & day rollups of the schedule.

    Renders one expandable card per **Monday–Friday week** of the selected
    Plan month: total units, total labor, top FG SKUs. Inside each card: a
    daily breakdown table (units + labor per day) and a per-station labor-mix
    bar.

    Weeks are real calendar weeks (Monday-anchored) of the Plan month chosen
    in the sidebar — the first / last week may be partial. Only rows dated
    inside the Plan month appear here; rows in other months are flagged by a
    top-of-page warning in main().

    Designed to answer the planner's question *"what's being built each
    week / day?"* without forcing them to scroll a 156-row table.

    Calendar **always** shows the entire month — even when the sidebar's
    Time window is set to "This week" or "Remaining (from today)" (which
    filter the *analysis* tabs). A small note is shown when the planner is
    in a filtered window so they understand the relationship.
    """
    from core.constants import STATION_KEY_TO_DISPLAY

    _heading = f"📆 Calendar — {plan_month_label}" if plan_month_label else "📆 Calendar"
    st.subheader(_heading)
    if schedule_df is None or schedule_df.empty:
        st.info(
            "No schedule loaded yet. Upload a CSV in the sidebar, then come "
            "back here for the week / day rollup."
        )
        return

    if time_window != "Whole month":
        st.caption(
            f"ℹ️ Showing the **full month** below. Your sidebar Time window "
            f"is set to **{time_window}** — that filter only affects the "
            "analysis tabs (Overview / Capacity / etc.), not this Calendar view."
        )

    if "PRODUCTION DAY" not in schedule_df.columns:
        st.info(
            "📅 This view needs `PRODUCTION DAY` data. Your CSV doesn't have "
            "one, and auto-spread didn't fill it in (uncommon). Add a "
            "PRODUCTION DAY column to your schedule and re-upload."
        )
        return

    # Normalize: real datetime, drop rows the loader couldn't date.
    work = schedule_df.copy()
    work["PRODUCTION DAY"] = pd.to_datetime(work["PRODUCTION DAY"], errors="coerce")
    work = work.dropna(subset=["PRODUCTION DAY"])
    if work.empty:
        st.info("No rows have a valid PRODUCTION DAY value.")
        return

    # Restrict to ONE month — the selected Plan month (the picker is
    # authoritative, so the Calendar only divides / shows that month; out-of-
    # month rows get a top-of-page warning in main() and are intentionally not
    # shown here), or, with no picker value, the month most rows fall in. The
    # week cards below are keyed on week-of-month, so two months must never
    # reach them together.
    work, _ym = _calendar_month_slice(work, plan_month)
    if work.empty:
        st.info(
            f"No units are dated in **{plan_month_label}**. Either pick a "
            "different **Plan month** in the sidebar, or add dates in that "
            "month to your schedule."
        )
        return

    # Recompute WEEK_OF_MONTH on the filtered frame so it matches the
    # Monday-anchored scheme used for the week cards below.
    work["WEEK_OF_MONTH"] = week_of_month_series(work["PRODUCTION DAY"])

    # Map each Monday-anchored week of the Plan month → its Mon–Fri working
    # days (clamped to the month) so every card shows a real calendar span,
    # e.g. "Jun 1 – Jun 5", even for partial first / last weeks.
    _week_days: dict[int, list] = {}
    for _d in _working_days_in_month(_ym[0], _ym[1]):
        _week_days.setdefault(week_of_month(_d), []).append(_d)

    # Per-row labor breakdown (station-level p-min × qty). Cache once so
    # we can sum it both by week and by day cheaply.
    breakdowns: list[dict] = []
    totals: list[float] = []
    for idx in work.index:
        row = work.loc[idx]
        fg = row.get("FG_BASE") or row.get("FG SKU ID")
        acc = row.get("ACC") or row.get("FG ACCRY SKU ID") or None
        try:
            qty = float(row.get("BUILD QTY", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            labor = compute_unit_labor(fg, acc, machine_df, acc_df)
        except Exception:
            labor = None
        if labor is None:
            br = {s: 0.0 for s in STATION_KEYS}
            total = max(qty, 1.0) * 100.0  # crude fallback (unknown SKU)
        else:
            br = {s: float(labor.get(s, 0) or 0) * qty for s in STATION_KEYS}
            total = sum(br.values())
        breakdowns.append(br)
        totals.append(total)
    work["_labor"] = totals
    # Attach per-station columns prefixed with __st_ so we can sum them.
    for s in STATION_KEYS:
        work[f"__st_{s}"] = [b.get(s, 0.0) for b in breakdowns]

    # Glance metrics at the top.
    total_units = int(work["BUILD QTY"].sum())
    total_labor = float(work["_labor"].sum())
    n_weeks = int(work["WEEK_OF_MONTH"].nunique())
    c1, c2, c3 = st.columns(3)
    c1.metric("Units in plan", f"{total_units:,}")
    c2.metric("Total labor", _fmt_min_hr(total_labor))
    c3.metric("Weeks", n_weeks)
    st.caption(
        "Each week below is an expander. Open one to see the day-by-day "
        "breakdown and the per-station labor mix."
    )
    st.markdown("---")

    # Iterate weeks in order.
    weeks = sorted(int(w) for w in work["WEEK_OF_MONTH"].dropna().unique())
    for week_num in weeks:
        wk_df = work[work["WEEK_OF_MONTH"] == week_num]
        if wk_df.empty:
            continue
        wk_units = int(wk_df["BUILD QTY"].sum())
        wk_labor = float(wk_df["_labor"].sum())
        # Day range — the week's Monday–Friday span in the month (clamped),
        # so the label reads as a real calendar week regardless of which days
        # actually have units. Falls back to the days used if the week isn't
        # in the month map (defensive).
        _wk_span = _week_days.get(week_num) or sorted(
            wk_df["PRODUCTION DAY"].dt.date.unique()
        )
        date_label = (
            f"{_wk_span[0].strftime('%b')} {_wk_span[0].day} – "
            f"{_wk_span[-1].strftime('%b')} {_wk_span[-1].day}"
            if len(_wk_span) > 1 else
            (f"{_wk_span[0].strftime('%b')} {_wk_span[0].day}" if _wk_span else "—")
        )
        # Top 3 FG SKUs by qty
        top_skus = (
            wk_df.groupby(wk_df["FG SKU ID"].astype(str))["BUILD QTY"]
            .sum()
            .sort_values(ascending=False)
            .head(3)
        )
        top_label = " · ".join(
            f"{sku} ×{int(q)}" for sku, q in top_skus.items()
        ) or "—"

        with st.expander(
            f"📆 **Week {week_num}** ({date_label}) — "
            f"{wk_units} units · {_fmt_min_hr(wk_labor)}  ·  Top: {top_label}",
            expanded=False,
        ):
            # ---- Daily rollup ----
            st.markdown("**Daily breakdown**")
            daily = (
                wk_df.assign(_d=wk_df["PRODUCTION DAY"].dt.date)
                .groupby("_d")
                .agg(
                    units=("BUILD QTY", "sum"),
                    labor=("_labor", "sum"),
                )
                .reset_index()
                .rename(columns={"_d": "Day"})
            )
            daily["Day"] = daily["Day"].apply(
                lambda d: f"{d.strftime('%a %b')} {d.day}"
            )
            daily["units"] = daily["units"].astype(int)
            daily["labor"] = daily["labor"].round(0).astype(int)
            daily["Labor (p-hr)"] = (daily["labor"] / 60.0).round(1)
            daily = daily.rename(columns={
                "units": "Units",
                "labor": "Labor (p-min)",
            })
            st.dataframe(
                daily, width="stretch", hide_index=True,
            )

            # ---- Per-station labor mix ----
            st.markdown("**Per-station labor mix this week**")
            station_totals = {
                STATION_KEY_TO_DISPLAY.get(s, s): float(wk_df[f"__st_{s}"].sum())
                for s in STATION_KEYS
            }
            # Drop zero-stations to keep the chart focused.
            mix_df = pd.DataFrame(
                [
                    {"Station": st_disp, "Labor (p-min)": v}
                    for st_disp, v in station_totals.items()
                    if v > 0
                ],
                columns=["Station", "Labor (p-min)"],
            )
            if mix_df.empty:
                # Every station at zero — e.g. the week's SKUs aren't in the
                # catalog yet. Sorting an empty frame by a column it doesn't
                # have would raise, so the guard comes first.
                st.info("ℹ️ No labor recorded for this week's units.")
            else:
                mix_df = mix_df.sort_values("Labor (p-min)", ascending=True)
                _bar = px.bar(
                    mix_df, x="Labor (p-min)", y="Station",
                    orientation="h", text_auto=",.0f",
                )
                _bar.update_layout(
                    height=min(360, 30 * len(mix_df) + 80),
                    margin=dict(l=4, r=4, t=4, b=4),
                    yaxis_title=None,
                )
                _bar.update_traces(marker_color="#4F46E5")
                # Unique key per week so Streamlit doesn't collide IDs
                # when multiple weeks' bars have similar shapes.
                st.plotly_chart(
                    _bar, width="stretch",
                    key=f"calendar_week_{week_num}_mix",
                )

            # ---- SKU detail table (collapsed) ----
            with st.expander(f"📋 Units in week {week_num} ({wk_units})", expanded=False):
                detail = wk_df.copy()
                detail["Day"] = detail["PRODUCTION DAY"].apply(
                    lambda d: f"{d.strftime('%a %b')} {d.day}"
                )
                cols = [
                    c for c in ["Day", "FG SKU ID", "FG ACCRY SKU ID",
                                "BUILD QTY", "CUSTOMER NAME", "LOCATION"]
                    if c in detail.columns
                ]
                renames = {
                    "FG SKU ID": "FG SKU",
                    "FG ACCRY SKU ID": "Accessory",
                    "BUILD QTY": "Qty",
                    "CUSTOMER NAME": "Customer",
                    "LOCATION": "Location",
                }
                st.dataframe(
                    detail[cols].rename(columns=renames),
                    width="stretch",
                    hide_index=True,
                )


# =============================================================
# SKU browser — supports the manual-entry "Browse catalog" panel
# =============================================================
def _render_scenarios_panel(location: str, seed_key: str, rev_key: str) -> None:
    """Sidebar expander for saving / loading named build-plan scenarios for the
    current facility. Persists to data/scenarios.json on GitHub."""
    # Always reload the latest scenarios from the cache (keyed on file mtime).
    scenarios = _load_scenarios_dict(_csv_mtime("scenarios.json")).get(location, {})
    scenario_names = sorted(scenarios.keys()) if isinstance(scenarios, dict) else []

    with st.sidebar.expander("📂 Save / load scenarios", expanded=False):
        # Replay any one-shot success / info / error message stashed before rerun
        toast_key = f"_scenario_toast_{location}"
        toast = st.session_state.pop(toast_key, None)
        if toast:
            level, msg = toast
            if level == "success":
                st.success(msg)
            elif level == "info":
                st.info(msg)
            else:
                st.error(msg)

        st.caption(
            f"Scenarios for **{location}** only. "
            "Saved to `data/scenarios.json` on GitHub — shared across all users."
        )

        # ------------------------------------------------------------------
        # SAVE
        # ------------------------------------------------------------------
        st.markdown("**Save current as**")
        save_name = st.text_input(
            "Scenario name",
            key=f"scenario_save_name_{location}",
            placeholder="e.g. May 2026 push",
            label_visibility="collapsed",
        )
        if st.button(
            "💾 Save scenario",
            key=f"scenario_save_btn_{location}",
            width="stretch",
        ):
            _scenario_save(location, save_name, seed_key, scenario_names)

        # ------------------------------------------------------------------
        # LOAD / DELETE
        # ------------------------------------------------------------------
        st.markdown("---")
        st.markdown("**Load a saved scenario**")
        if not scenario_names:
            st.caption(f"_No saved scenarios for {location} yet._")
            return

        chosen = st.selectbox(
            "Scenario",
            options=scenario_names,
            key=f"scenario_load_pick_{location}",
            label_visibility="collapsed",
        )
        # Show metadata for the chosen scenario
        meta = scenarios.get(chosen, {}) if isinstance(scenarios, dict) else {}
        n_entries = len(meta.get("entries", []) or [])
        saved_at = meta.get("saved_at", "")
        st.caption(f"📋 {n_entries} row(s)  ·  saved {saved_at}")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("📥 Load", key=f"scenario_load_btn_{location}",
                         width="stretch"):
                _scenario_load(location, chosen, seed_key, rev_key)
        with c2:
            if st.button("🗑 Delete", key=f"scenario_del_btn_{location}",
                         width="stretch"):
                _scenario_delete(location, chosen)


def _scenario_save(location: str, name: str, seed_key: str, existing_names: list) -> None:
    """Handler for the Save button — validates and pushes the current
    manual-entry rows as a named scenario."""
    name = (name or "").strip()
    if not name:
        st.warning("Please enter a scenario name before saving.")
        return

    # Build the entries DataFrame from the latest manual-editor state. Prefer
    # the current data_editor key (so any in-flight edits are captured);
    # fall back to the seed if the editor hasn't rendered yet.
    rev = st.session_state.get(f"manual_rev_{location}", 0)
    editor_key = f"manual_entries_{location}_v{rev}"
    df = st.session_state.get(editor_key)
    if not isinstance(df, pd.DataFrame):
        df = st.session_state.get(seed_key, pd.DataFrame(
            columns=["FG SKU", "Accessory SKU", "Qty"]
        ))

    # Check empty after stripping blank rows — let save handler do the cleaning
    non_empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if not non_empty.empty:
        non_empty["__fg"] = non_empty.get("FG SKU", "").astype(str).str.strip()
        non_empty["__qty"] = pd.to_numeric(
            non_empty.get("Qty", 0), errors="coerce",
        ).fillna(0).astype(int)
        non_empty = non_empty[(non_empty["__fg"] != "") & (non_empty["__qty"] > 0)]
    if non_empty.empty:
        st.warning(
            "The manual-entry table is empty. Fill in at least one row (FG SKU + Qty > 0) "
            "before saving the scenario."
        )
        return

    # Single per-location confirm key — stores the name the user was warned
    # about on the previous click. We only honor it if the name STILL matches
    # the current attempt, so a stale flag for an old name (left over after
    # the user typed a new name and saved cleanly) can't fire an unconfirmed
    # overwrite of a different scenario later.
    confirm_key = f"_scenario_overwrite_pending_{location}"
    if name in existing_names:
        if st.session_state.get(confirm_key) != name:
            st.session_state[confirm_key] = name
            st.warning(
                f"A scenario named **{name}** already exists for {location}. "
                "Click **💾 Save scenario** again to overwrite it."
            )
            return
        # Second click on the SAME name — clear the flag and fall through.
        st.session_state.pop(confirm_key, None)
    else:
        # Saving a different (non-existing) name — drop any stale warning state
        # so a later attempt at an existing name re-triggers the confirmation.
        st.session_state.pop(confirm_key, None)

    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    try:
        with st.spinner(f"Saving scenario '{name}'..."):
            response = save_scenario_to_github(location, name, df, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            _load_scenarios_dict.clear()
        except Exception:
            pass
        _track_and_flush("scenario_save", facility=location, name=name)
        # Belt and braces — clear the confirm flag after a successful save.
        st.session_state.pop(confirm_key, None)
        # Stash a one-shot success message that survives the upcoming rerun
        st.session_state[f"_scenario_toast_{location}"] = (
            "success",
            f"✅ Saved scenario **{name}** for {location}"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        _defer_rerun()
    except Exception as e:
        _show_github_error(e, "Save")


def _scenario_load(location: str, name: str, seed_key: str, rev_key: str) -> None:
    """Handler for the Load button — replaces the manual-editor's seed with
    the saved scenario's rows and forces the editor to re-render."""
    block = get_scenario(location, name) or {}
    rows = block.get("entries", []) or []
    if not rows:
        st.warning(f"Scenario '{name}' has no entries.")
        return
    seed = pd.DataFrame(rows, columns=["FG SKU", "Accessory SKU", "Qty"])
    # Coerce Qty to int (json round-trip can produce floats)
    seed["Qty"] = pd.to_numeric(seed["Qty"], errors="coerce").fillna(0).astype(int)
    st.session_state[seed_key] = seed
    st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
    # Stash the scenario name so manual-mode auto-spread can parse a
    # target month from it (e.g. "Hen 2026 June" → June 2026 instead of
    # defaulting to today's month).
    st.session_state[f"_last_loaded_scenario_name_{location}"] = name
    # Don't force-flush here — load is a read; let buffer accumulate.
    track_event("scenario_load", facility=location, name=name)
    # Shown at the top of the panel on the rerun — a message drawn here would
    # be thrown away with the rest of this run.
    st.session_state[f"_scenario_toast_{location}"] = (
        "success", f"📥 Loaded scenario **{name}** — {len(seed)} row(s).",
    )
    _defer_rerun()


def _show_github_error(e, action: str = "Save") -> None:
    """Render a planner-friendly GitHub error + a collapsed technical detail.

    Maps the common failure modes to plain guidance so a planner isn't shown a
    raw '401 Client Error: ... for url: https://api.github.com/...' string. The
    exact exception text is preserved under "Technical details" for admins.
    """
    msg = str(e)
    low = msg.lower()
    if "401" in msg or "403" in msg or "bad credentials" in low:
        why = ("the GitHub token is missing, expired, or lacks write access. "
               "Ask your admin to refresh `github_token` in Streamlit Secrets.")
    elif "404" in msg or "not found" in low:
        why = ("the file or repo path wasn't found — the repo may have moved. "
               "Ask your admin to check the GitHub settings.")
    elif "409" in msg or "422" in msg or "sha" in low or "conflict" in low:
        why = ("someone else just saved a change. Reload the page to pick up "
               "their version, then try again.")
    elif any(s in low for s in ("connection", "timed out", "timeout", "network",
                                "max retries", "name resolution", "unreachable")):
        why = ("GitHub couldn't be reached. Check your connection and try again "
               "in a minute.")
    else:
        why = "something went wrong — see the details below and retry."
    st.error(f"❌ {action} failed — {why}")
    with st.expander("Technical details"):
        st.code(msg)


def _post_save_redeploy_notice(key_suffix: str) -> None:
    """Standard line after a GitHub-writing save.

    Every store mirrors what it pushed into this container's ``data/`` folder,
    so the saved values are in use in this session at once; other browsers
    pick them up after the ~1–2 min Streamlit Cloud rebuild. Saying exactly
    that stops a planner from waiting and refreshing for a value that already
    drives the numbers on screen. (It used to promise a 🔄 Refresh banner that
    never fires for your own commit and carry a reload button whose click was
    a plain rerun; ``key_suffix`` is kept so call sites don't change.)
    """
    del key_suffix
    st.caption(
        "In use now. Teammates see the saved values after the app redeploys "
        "(~1–2 minutes)."
    )


def _confirm_destructive(confirm_key: str, token: str, warn_msg: str) -> bool:
    """Two-click guard for an irreversible, team-shared action (delete/reset).

    These actions hit GitHub and affect every user, so we require a second click
    to confirm — mirroring the save-overwrite pattern. The pending flag stores
    ``token`` (e.g. the scenario name) and we only treat a click as confirmed
    when the stashed token STILL matches the current target, so a leftover flag
    from a previously-selected item can't silently confirm a delete of a
    different one. Returns True on the confirming (second) click; on the first
    click it stashes the flag, shows ``warn_msg``, and returns False so the
    caller aborts.
    """
    if st.session_state.get(confirm_key) == token:
        st.session_state.pop(confirm_key, None)
        return True
    st.session_state[confirm_key] = token
    st.warning(warn_msg)
    return False


def _scenario_delete(location: str, name: str) -> None:
    """Handler for the Delete button."""
    if not _confirm_destructive(
        f"_scenario_delete_pending_{location}", name,
        f"⚠️ Delete scenario **{name}** for everyone? This can't be undone — "
        "click **🗑 Delete** again to confirm.",
    ):
        return
    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner(f"Deleting scenario '{name}'..."):
            delete_scenario_on_github(location, name, token)
        try:
            _load_scenarios_dict.clear()
        except Exception:
            pass
        _track_and_flush("scenario_delete", facility=location, name=name)
        st.session_state[f"_scenario_toast_{location}"] = (
            "success", f"🗑 Deleted scenario **{name}** for {location}.",
        )
        _defer_rerun()
    except Exception as e:
        _show_github_error(e, "Delete")


# =============================================================
# Uploaded-schedule save/load/delete — sidebar expander.
# Mirrors the scenarios pattern (_scenario_save/load/delete) but stores
# the raw CSV bytes in data/uploaded_schedules.json so a teammate can
# reload the exact same schedule later. Per-facility keyed.
# =============================================================
def _render_uploaded_schedules_panel(location: str, uploaded_file) -> None:
    """Sidebar expander for saving / loading named uploaded schedules.

    ``uploaded_file`` is the live ``st.file_uploader`` widget value (or None).
    When the planner clicks Save, we serialize those bytes to text and push
    to GitHub. When they click Load, we stash a BytesIO in session_state
    that ``_load_schedule_df`` consumes one-shot on the next rerun.
    """
    all_schedules = _load_uploaded_schedules_dict(_csv_mtime("uploaded_schedules.json"))
    by_loc = all_schedules.get(location, {}) if isinstance(all_schedules, dict) else {}
    # Newest save first, so the default pick is the schedule a teammate saved
    # most recently — not whichever name happens to sort first.
    schedule_names = names_newest_first(by_loc)

    with st.sidebar.expander("📂 Save / load uploaded schedules", expanded=False):
        # Replay any one-shot toast stashed before rerun
        toast_key = f"_uploaded_schedule_toast_{location}"
        toast = st.session_state.pop(toast_key, None)
        if toast:
            level, msg = toast
            if level == "success":
                st.success(msg)
            elif level == "info":
                st.info(msg)
            else:
                st.error(msg)

        st.caption(
            f"Saved schedules for **{location}** only. "
            "Pushed to `data/uploaded_schedules.json` on GitHub — shared "
            "with the whole team."
        )

        # ------------------------------------------------------------------
        # SAVE — what's saved is the schedule the app is analysing right now
        # (see _active_schedule_bytes): the upload as converted to CSV, or,
        # after a Sequencer ✅ Accept / 🗓 Apply / 🔀 Bulk move / 🧹 Strip /
        # 📅 Move into plan month, the reshaped plan. A schedule loaded from
        # the list below can therefore be edited and re-saved without the
        # uploader holding a file. (It used to push the uploader's raw file,
        # so none of those edits ever reached GitHub.)
        # ------------------------------------------------------------------
        st.markdown("**Save the active schedule as**")
        save_name = st.text_input(
            "Schedule name",
            key=f"uploaded_schedule_save_name_{location}",
            placeholder="e.g. May 2026 production plan",
            label_visibility="collapsed",
        )
        _can_save = _has_active_schedule(uploaded_file)
        if st.button(
            "💾 Save schedule",
            key=f"uploaded_schedule_save_btn_{location}",
            width="stretch",
            disabled=not _can_save,
            help=(
                "Nothing to save yet — upload a schedule file, or load one "
                "from the list below, first."
                if not _can_save else
                "Pushes the schedule the app is analysing right now — your "
                "upload, including any Sequencer / 🗓 Sequence / 🧹 cleanup "
                "changes you applied — to GitHub under this facility. "
                "Teammates can reload it from the dropdown below."
            ),
        ):
            _uploaded_schedule_save(location, save_name, uploaded_file, schedule_names)

        # ------------------------------------------------------------------
        # LOAD + DELETE
        # ------------------------------------------------------------------
        st.markdown("**Load saved**")
        if not schedule_names:
            st.caption("_No saved schedules for this facility yet._")
        else:
            chosen = st.selectbox(
                "Saved schedule",
                schedule_names,
                key=f"uploaded_schedule_load_pick_{location}",
                label_visibility="collapsed",
            )
            block = by_loc.get(chosen) or {}
            saved_at = block.get("saved_at", "?")
            # Recount from the CSV itself (~2 ms): blocks saved before 2026-10
            # stored a line-break count, which a quoted multi-line cell
            # inflates ("296 rows" for a 288-row file), so it never matched
            # the "Loaded N of M rows" pill after a 📥 Load.
            try:
                rows = count_csv_data_rows(block.get("csv_content", ""))
            except Exception:
                rows = block.get("rows", "?")
            st.caption(f"Last saved: **{saved_at}** · **{rows}** rows")
            c1, c2 = st.columns([3, 1])
            if c1.button(
                "📥 Load", key=f"uploaded_schedule_load_btn_{location}",
                width="stretch",
            ):
                _uploaded_schedule_load(location, chosen)
            if c2.button(
                "🗑 Delete", key=f"uploaded_schedule_del_btn_{location}",
                width="stretch",
            ):
                _uploaded_schedule_delete(location, chosen)


def _uploaded_schedule_save(
    location: str, name: str, uploaded_file, existing_names: list,
    *, rerun=None,
) -> None:
    """Save the schedule the app is analysing under ``name`` to GitHub.

    ``rerun`` is how the success is followed up: the sidebar's button leaves
    it unset (``_defer_rerun`` — a sidebar handler never reruns on the spot),
    the plan guide's inline save passes ``st.rerun`` (a main-panel button).

    The bytes come from :func:`_active_schedule_bytes` — the persisted active
    schedule (with every reshape applied this session), not the uploader's
    raw file — so what teammates reload is what the planner was looking at.
    ``uploaded_file`` only matters on the one run where a brand-new upload
    hasn't been promoted by the loader yet.

    Mirrors ``_scenario_save`` — same overwrite-confirmation pattern (per-
    location key carrying the name we warned about, cleared on save) so a
    stale flag can't fire an unconfirmed overwrite later.
    """
    name = (name or "").strip()
    if not name:
        st.warning("Please enter a schedule name before saving.")
        return
    try:
        raw = _active_schedule_bytes(uploaded_file)
    except Exception as e:
        st.error(f"❌ Couldn't read the uploaded file: {e}")
        return
    if raw is None:
        st.warning(
            "Nothing to save yet — upload a schedule file, or load one from "
            "the list below, first."
        )
        return

    confirm_key = f"_uploaded_schedule_overwrite_pending_{location}"
    if name in existing_names:
        if st.session_state.get(confirm_key) != name:
            st.session_state[confirm_key] = name
            st.warning(
                f"A schedule named **{name}** already exists for {location}. "
                "Click **💾 Save schedule** again to overwrite it."
            )
            return
        st.session_state.pop(confirm_key, None)
    else:
        st.session_state.pop(confirm_key, None)

    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    # Decode the active CSV bytes to text. (An Excel upload was already
    # converted to CSV when it became the active schedule — same sheet
    # detection as the loader — so what's saved is always a CSV the loader can
    # re-read.) The loader handles latin-1 / UTF-8 / UTF-8-with-BOM
    # transparently; here we just need *some* text encoding we can re-parse.
    # Try utf-8 first, fall back to latin-1.
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        csv_text = raw.decode("latin-1")

    try:
        with st.spinner(f"Saving schedule '{name}' for {location}..."):
            response = save_uploaded_schedule_to_github(location, name, csv_text, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            _load_uploaded_schedules_dict.clear()
        except Exception:
            pass
        _track_and_flush(
            "schedule_save", facility=location, name=name,
            rows=count_csv_data_rows(csv_text),
        )
        st.session_state.pop(confirm_key, None)
        st.session_state[f"_uploaded_schedule_toast_{location}"] = (
            "success",
            f"✅ Saved schedule **{name}** for {location}"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        (rerun or _defer_rerun)()
    except Exception as e:
        _show_github_error(e, "Save")


def _uploaded_schedule_load(location: str, name: str) -> None:
    """Load a saved schedule by stashing its CSV bytes into session_state.

    ``_load_schedule_df`` picks the buffer up on the next rerun and feeds it
    to ``load_schedule`` as if the planner had just uploaded the file. The
    buffer is cleared once consumed, so a fresh upload still wins on the
    rerun after that.
    """
    if _queue_saved_schedule_load(location, name):
        _defer_rerun()


def _queue_saved_schedule_load(location: str, name: str) -> bool:
    """Queue the saved schedule ``name`` for the resolver (a fresh load: it
    names the schedule and starts the edit history over). Shared by the
    sidebar's 📥 Load (which defers its rerun) and the main panel's way-back
    buttons (which rerun at once). False when the save is empty."""
    block = get_uploaded_schedule(location, name) or {}
    csv_text = block.get("csv_content", "")
    if not csv_text:
        st.warning(f"Saved schedule '{name}' is empty.")
        return False
    _queue_schedule_bytes(
        csv_text.encode("utf-8"), kind=None, source="saved", name=name,
        toast=f"📥 Loaded saved schedule **{name}** for {location}.",
    )
    _track_and_flush("schedule_load_saved", facility=location, name=name)
    # Shown at the top of the panel on the rerun — a message drawn here would
    # be thrown away with the rest of this run.
    st.session_state[f"_uploaded_schedule_toast_{location}"] = (
        "success", f"📥 Loaded saved schedule **{name}** for {location}.",
    )
    return True


def _queue_newest_saved_load(location: str) -> bool:
    """Queue the facility's newest saved schedule — the 📂 picker's default."""
    newest = _newest_saved_schedule(location)
    if newest is None:
        return False
    return _queue_saved_schedule_load(location, newest[0])


def _uploaded_schedule_delete(location: str, name: str) -> None:
    """Handler for the Delete button."""
    if not _confirm_destructive(
        f"_uploaded_schedule_delete_pending_{location}", name,
        f"⚠️ Delete saved schedule **{name}** for everyone? This can't be undone "
        "— click **🗑 Delete** again to confirm.",
    ):
        return
    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner(f"Deleting saved schedule '{name}'..."):
            delete_uploaded_schedule_on_github(location, name, token)
        try:
            _load_uploaded_schedules_dict.clear()
        except Exception:
            pass
        _track_and_flush("schedule_delete", facility=location, name=name)
        st.session_state[f"_uploaded_schedule_toast_{location}"] = (
            "success", f"🗑 Deleted saved schedule **{name}** for {location}.",
        )
        _defer_rerun()
    except Exception as e:
        _show_github_error(e, "Delete")


# =============================================================
# PASTE FROM EXCEL — bulk SKU import for manual entry mode
# =============================================================
# Planners normally already have the build list sitting in a spreadsheet: a
# column of FG SKUs, sometimes an accessory column, sometimes a quantity.
# Exporting that to a full schedule CSV (LOCATION / PRODUCTION MONTH headers
# and all) just to run a what-if is heavy, and retyping row by row is worse.
# These helpers let the planner copy the cells straight out of Excel and paste
# them into the sidebar: the parser works out which column is which, then the
# rows land in the manual-entry table where they can still be edited or
# extended by hand.

# Header aliases, checked per cell in this order: qty → accessory → FG.
# Accessory before FG matters — "ACCESSORY SKU" would otherwise be claimed by
# the FG list's bare "SKU".
_PASTE_QTY_HEADERS = (
    "BUILD QTY", "BUILD QUANTITY", "QUANTITY", "QTY", "UNITS", "UNIT COUNT",
    "COUNT", "EACH", "EA",
)
_PASTE_ACC_HEADERS = (
    "FG ACCRY SKU ID", "FG ACCRY SKU", "FG ACCRY", "ACCESSORY SKU ID",
    "ACCESSORY SKU", "ACCRY SKU", "ACC SKU", "ACCESSORY KIT", "ACCESSORY",
    "ACCRY", "ACC KIT", "KIT", "ACC",
)
_PASTE_FG_HEADERS = (
    "FG SKU ID", "FG SKU", "FINISHED GOOD", "MACHINE SKU", "SKU ID",
    "MACHINE", "MODEL", "ITEM", "PART", "SKU", "FG", "UNIT",
)

# Qty cells: a plain number, optionally with a unit suffix. Deliberately a
# full match so a PRODUCTION DAY column like `5/4/2026` can never be mistaken
# for a quantity when columns are being guessed positionally.
_PASTE_QTY_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(?:EA|EACH|PC|PCS|UNIT|UNITS|X)?$", re.IGNORECASE
)
# Accessory SKUs carry an -A### / -AXXX / " AXXX" tail (BOSS25-A016,
# PDS100 AXXX). Only consulted when catalog membership can't answer.
_PASTE_ACC_SKU_RE = re.compile(r"[-\s]A(?:\d|XXX)", re.IGNORECASE)
# Shapes that pass the cheap "looks like a SKU" test but aren't one — dates
# and month labels ride along in real Excel exports.
_PASTE_DATE_RE = re.compile(r"^\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}$")
_PASTE_MONTH_RE = re.compile(r"^[A-Z]{3,9}[ -]\d{2,4}$|^\d{2,4}[ -][A-Z]{3,9}$", re.IGNORECASE)

_PASTE_COLUMNS = ["FG SKU", "Accessory SKU", "Qty"]

# Display-only line number on the manual-entry table. It's regenerated from
# row content every render — never read back out of the editor — so pasting,
# adding, or deleting rows always leaves the count correct.
_MANUAL_ROW_NO = "#"


def _manual_row_has_content(df) -> pd.Series:
    """True for rows carrying an FG SKU or an Accessory SKU.

    Quantity deliberately doesn't count: a row someone has half-filled is
    still a row, and a blank one shouldn't take a line number.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series([], dtype=bool)

    def _filled(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series(False, index=df.index)
        return df[col].fillna("").astype(str).str.strip() != ""

    return _filled("FG SKU") | _filled("Accessory SKU")


def _manual_add_row_numbers(df) -> pd.DataFrame:
    """Return a display copy of the manual entries with a leading `#` column.

    Only rows with a SKU in them are numbered — blank rows (the trailing
    spares, or a gap left mid-table) get an empty cell and don't consume a
    number, so the sequence always matches the rows that will actually build.
    Values are strings so an unnumbered row renders empty instead of as a
    `None` the editor would flag.
    """
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(columns=_PASTE_COLUMNS)
    out = out.drop(columns=[_MANUAL_ROW_NO], errors="ignore")
    # Guarantee the editor is handed a RangeIndex whoever wrote the seed —
    # anything else makes Streamlit un-hide the index column and mark it
    # required, which stops rows added in the grid from ever reaching us.
    out = out.reset_index(drop=True)

    numbers: list[str] = []
    seq = 0
    for has_content in _manual_row_has_content(out):
        if has_content:
            seq += 1
            numbers.append(str(seq))
        else:
            numbers.append("")
    out.insert(0, _MANUAL_ROW_NO, numbers)
    return out


def _manual_drop_row_numbers(df):
    """Strip the display-only `#` column before rows are stored or used."""
    if isinstance(df, pd.DataFrame) and _MANUAL_ROW_NO in df.columns:
        return df.drop(columns=[_MANUAL_ROW_NO])
    return df


def _paste_clean_cell(value) -> str:
    """Strip the debris Excel copies along with a cell (NBSP, stray quotes,
    the leading apostrophe Excel uses to force text)."""
    s = str(value or "").replace("\xa0", " ").strip()
    s = s.strip('"').strip("'").strip()
    return re.sub(r"\s+", " ", s)


def _paste_norm_header(cell) -> str:
    """Uppercase a header cell and reduce punctuation to single spaces."""
    return re.sub(r"[^A-Z0-9]+", " ", str(cell or "").upper()).strip()


def _paste_header_role(cell) -> "str | None":
    """Map one header cell to `Qty` / `Accessory SKU` / `FG SKU`, else None.

    Matches whole words only, so a header like `SEAT` can't match `EA`.
    """
    norm = _paste_norm_header(cell)
    if not norm:
        return None
    padded = f" {norm} "
    for role, patterns in (
        ("Qty", _PASTE_QTY_HEADERS),
        ("Accessory SKU", _PASTE_ACC_HEADERS),
        ("FG SKU", _PASTE_FG_HEADERS),
    ):
        for pattern in patterns:
            if f" {_paste_norm_header(pattern)} " in padded:
                return role
    return None


def _paste_parse_qty(cell) -> "float | None":
    """Read a quantity cell. None when it isn't a plain number."""
    s = str(cell or "").strip().replace(",", "")  # 1,200 → 1200
    m = _PASTE_QTY_RE.match(s)
    return float(m.group(1)) if m else None


def _paste_is_sku_like(value) -> bool:
    """Cheap shape test — SKUs are short, carry a digit, and have no prose."""
    s = str(value or "").strip().upper()
    if not (2 < len(s) <= 24) or len(s.split()) > 2:
        return False
    if not re.search(r"\d", s):
        return False
    # A bare quantity ("5", "2.0", "5 EA") is a number, not a SKU — every SKU
    # in these catalogs carries an alpha family prefix.
    if _paste_parse_qty(s) is not None:
        return False
    if _PASTE_DATE_RE.match(s) or _PASTE_MONTH_RE.match(s):
        return False
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9 ./_-]*", s))


def _paste_split_grid(text) -> "tuple[list[tuple[int, list[str]]], str]":
    """Split pasted spreadsheet text into `(line_no, cells)` rows.

    Returns the rows plus the delimiter name we settled on, so the UI can tell
    the planner how their paste was read.
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [(i + 1, ln) for i, ln in enumerate(raw.split("\n")) if ln.strip()]
    if not lines:
        return [], "none"

    body = "\n".join(ln for _, ln in lines)
    if "\t" in body:
        delim = "tab"
    elif "," in body:
        delim = "comma"
    elif ";" in body:
        delim = "semicolon"
    elif re.search(r"\S {2,}\S", body):
        delim = "spaces"
    else:
        delim = "none"

    char = {"tab": "\t", "comma": ",", "semicolon": ";"}.get(delim)
    grid: list[tuple[int, list[str]]] = []
    for no, ln in lines:
        if char:
            # csv.reader so a quoted cell ("BOSS25-010, spare") survives intact
            try:
                cells = next(csv.reader([ln], delimiter=char), [ln])
            except csv.Error:
                cells = ln.split(char)
        elif delim == "spaces":
            cells = re.split(r" {2,}", ln)
        else:
            cells = [ln]
        grid.append((no, [_paste_clean_cell(c) for c in cells]))

    width = max(len(cells) for _, cells in grid)
    grid = [(no, cells + [""] * (width - len(cells))) for no, cells in grid]

    # A single pasted line of 3+ SKU-shaped cells with no quantity anywhere is
    # a horizontal list — someone copied a row instead of a column. Stand it up.
    if len(grid) == 1 and width >= 3:
        no, cells = grid[0]
        filled = [c for c in cells if c]
        if not any(_PASTE_QTY_RE.match(c) for c in filled):
            grid = [(no, [c]) for c in filled]

    return grid, delim


def _paste_detect_header(cells, known_up: set) -> "dict[int, str] | None":
    """Return `{col_index: role}` when the first pasted row is a header row."""
    if any(c and c.upper() in known_up for c in cells):
        return None  # a real catalog SKU sits in row 1 — that's data
    if any(_PASTE_QTY_RE.match(c) for c in cells if c):
        return None  # header rows don't carry quantities
    roles = {i: _paste_header_role(c) for i, c in enumerate(cells) if c}
    roles = {i: r for i, r in roles.items() if r}
    return roles or None


def _paste_assign_columns(rows, header_roles, machine_up: dict, acc_up: dict) -> dict:
    """Decide which column holds FG SKU / Accessory SKU / Qty.

    Header names win where they exist; everything left over is inferred from
    the data itself — catalog membership first, then shape (numeric vs
    SKU-like). Columns we can't place (LOCATION, CUSTOMER, dates…) are simply
    left unclaimed and ignored.
    """
    width = max((len(cells) for _, cells in rows), default=0)
    cols: dict[str, "int | None"] = {c: None for c in _PASTE_COLUMNS}
    for idx, role in (header_roles or {}).items():
        if idx < width and role in cols and cols[role] is None:
            cols[role] = idx

    free = [i for i in range(width) if i not in {v for v in cols.values() if v is not None}]

    def _frac(idx, pred) -> float:
        vals = [cells[idx] for _, cells in rows if idx < len(cells) and cells[idx]]
        return (sum(1 for v in vals if pred(v)) / len(vals)) if vals else 0.0

    numeric = {i: _frac(i, lambda v: _paste_parse_qty(v) is not None) for i in free}
    in_fg = {i: _frac(i, lambda v: v.upper() in machine_up) for i in free}
    in_acc = {i: _frac(i, lambda v: v.upper() in acc_up) for i in free}
    acc_like = {i: _frac(i, lambda v: bool(_PASTE_ACC_SKU_RE.search(v))) for i in free}
    sku_like = {i: _frac(i, _paste_is_sku_like) for i in free}

    def _claim(role: str, idx: int) -> None:
        cols[role] = idx
        free.remove(idx)

    # Qty — a column of plain numbers that doesn't read as a SKU.
    if cols["Qty"] is None:
        cands = [i for i in free if numeric[i] >= 0.6 and sku_like[i] < 0.5]
        if cands:
            # Excel puts qty last more often than not; break ties to the right.
            _claim("Qty", max(cands, key=lambda i: (numeric[i], i)))

    # FG SKU — a catalog hit first, then the leftmost SKU-shaped column.
    if cols["FG SKU"] is None and free:
        hits = [i for i in free if in_fg[i] > 0 and in_fg[i] >= in_acc[i]]
        if hits:
            _claim("FG SKU", max(hits, key=lambda i: (in_fg[i], -i)))
        else:
            shaped = [i for i in free if sku_like[i] >= 0.5 and acc_like[i] < 0.5]
            _claim("FG SKU", shaped[0] if shaped else free[0])

    # Accessory SKU — a catalog hit or the -A### shape, else the next
    # SKU-shaped leftover. Prose columns (customer, description) never qualify.
    if cols["Accessory SKU"] is None and free:
        hits = [i for i in free if in_acc[i] > 0 or acc_like[i] >= 0.5]
        if hits:
            _claim("Accessory SKU",
                   max(hits, key=lambda i: (max(in_acc[i], acc_like[i]), -i)))
        else:
            shaped = [i for i in free if sku_like[i] >= 0.5]
            if shaped:
                _claim("Accessory SKU", shaped[0])

    return cols


def _paste_canonical(sku, catalog_up: dict) -> str:
    """Uppercase a pasted SKU, preferring the catalog's own spelling when known."""
    s = str(sku or "").strip()
    if not s:
        return ""
    return catalog_up.get(s.upper(), s.upper())


def _parse_pasted_skus(
    text,
    machine_skus=None,
    acc_skus=None,
    combine_duplicates: bool = True,
) -> "tuple[pd.DataFrame, dict]":
    """Turn text copied out of a spreadsheet into manual-entry rows.

    Handles what actually comes off a clipboard: tab-separated Excel cells,
    CSV, semicolon exports, space-aligned text, or a bare one-SKU-per-line
    list. A header row is optional; so are the accessory and quantity columns
    (a missing quantity counts as 1).

    Returns ``(DataFrame[FG SKU, Accessory SKU, Qty], report)`` where *report*
    carries everything the UI needs to explain the import: the delimiter and
    columns it settled on, per-line skip reasons, and any SKUs missing from
    the catalogs.
    """
    machine_up = {str(s).strip().upper(): str(s).strip()
                  for s in (machine_skus or []) if str(s).strip()}
    acc_up = {str(s).strip().upper(): str(s).strip()
              for s in (acc_skus or []) if str(s).strip()}
    known_up = set(machine_up) | set(acc_up)

    grid, delim = _paste_split_grid(text)
    header_roles = _paste_detect_header(grid[0][1], known_up) if grid else None
    rows = grid[1:] if header_roles else grid
    cols = _paste_assign_columns(rows, header_roles, machine_up, acc_up) if rows else {
        c: None for c in _PASTE_COLUMNS
    }

    entries: list[dict] = []
    skipped: list[dict] = []
    qty_defaulted = qty_rounded = dropped_acc = 0

    for no, cells in rows:
        def _cell(idx) -> str:
            return cells[idx] if idx is not None and idx < len(cells) else ""

        shown = " | ".join(c for c in cells if c)
        fg_raw = _cell(cols["FG SKU"])
        if not fg_raw:
            skipped.append({"line": no, "text": shown, "reason": "no FG SKU in this row"})
            continue
        # Spreadsheet formula-injection guard — same rule as the catalog forms.
        if fg_raw[:1] in ("=", "+", "-", "@"):
            skipped.append({"line": no, "text": shown,
                            "reason": "FG SKU starts with =, +, - or @"})
            continue

        qty_cell = _cell(cols["Qty"])
        if not qty_cell:
            qty = 1
            qty_defaulted += 1
        else:
            parsed_qty = _paste_parse_qty(qty_cell)
            if parsed_qty is None:
                skipped.append({"line": no, "text": shown,
                                "reason": f"quantity “{qty_cell}” isn't a number"})
                continue
            qty = int(round(parsed_qty))
            if qty <= 0:
                skipped.append({"line": no, "text": shown,
                                "reason": f"quantity is {qty_cell}"})
                continue
            if float(qty) != parsed_qty:
                qty_rounded += 1

        acc_raw = _cell(cols["Accessory SKU"])
        if acc_raw[:1] in ("=", "+", "-", "@"):
            acc_raw = ""
            dropped_acc += 1

        entries.append({
            "FG SKU": _paste_canonical(fg_raw, machine_up),
            "Accessory SKU": _paste_canonical(acc_raw, acc_up),
            "Qty": qty,
        })

    merged = 0
    if combine_duplicates and entries:
        combined: dict[tuple, dict] = {}
        order: list[tuple] = []
        for e in entries:
            key = (e["FG SKU"], e["Accessory SKU"])
            if key in combined:
                combined[key]["Qty"] += e["Qty"]
            else:
                combined[key] = dict(e)
                order.append(key)
        merged = len(entries) - len(order)
        entries = [combined[k] for k in order]

    def _unknown(field: str, catalog_up: dict) -> list[str]:
        """SKUs the catalog has never heard of — reported, never dropped."""
        if not catalog_up:
            return []
        out, seen = [], set()
        for e in entries:
            sku = e[field]
            if sku and sku.upper() not in catalog_up and sku not in seen:
                seen.add(sku)
                out.append(sku)
        return out

    df = pd.DataFrame(entries, columns=_PASTE_COLUMNS)
    if not df.empty:
        df["Qty"] = df["Qty"].astype(int)

    report = {
        "delimiter": delim,
        "header": header_roles is not None,
        "columns": dict(cols),
        "rows": int(len(df)),
        "units": int(df["Qty"].sum()) if not df.empty else 0,
        "skipped": skipped,
        "qty_defaulted": qty_defaulted,
        "qty_rounded": qty_rounded,
        "merged": merged,
        "dropped_acc": dropped_acc,
        "unknown_fg": _unknown("FG SKU", machine_up),
        "unknown_acc": _unknown("Accessory SKU", acc_up),
    }
    return df, report


_PASTE_DELIM_LABEL = {
    "tab": "tab-separated (straight from Excel)",
    "comma": "comma-separated",
    "semicolon": "semicolon-separated",
    "spaces": "space-aligned columns",
    "none": "one SKU per line",
}


def _paste_commit(parsed: pd.DataFrame, location: str, seed_key: str,
                  rev_key: str, nonce_key: str, *, replace: bool) -> None:
    """Push the parsed rows into the manual entries table, then clear the box.

    Same session-state contract as the catalog browser's ➕ Add: write the new
    seed, bump the revision so the data_editor picks it up, rerun.
    """
    if replace:
        current = pd.DataFrame(columns=_PASTE_COLUMNS)
    else:
        current = st.session_state.get(
            seed_key, pd.DataFrame(columns=_PASTE_COLUMNS),
        ).copy()
        # Drop the untouched blank rows so pasted rows don't land under a gap.
        # "Blank" has to mean the same thing here as it does to the row
        # numberer, or an accessory-only row it numbered gets thrown away by
        # the next ➕ Add.
        if not current.empty:
            qty = pd.to_numeric(current.get("Qty", 0), errors="coerce").fillna(0)
            current = current[_manual_row_has_content(current) | (qty > 0)]

    # A couple of empty rows on the end so "add a few more by hand" stays a
    # one-click affair in the editor.
    blanks = pd.DataFrame({"FG SKU": [""] * 2, "Accessory SKU": [""] * 2, "Qty": [0] * 2})
    st.session_state[seed_key] = pd.concat(
        [current, parsed[_PASTE_COLUMNS], blanks], ignore_index=True,
    )
    st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
    st.session_state[nonce_key] = st.session_state.get(nonce_key, 0) + 1

    _track_and_flush(
        "paste_import", facility=location, rows=int(len(parsed)),
        units=int(parsed["Qty"].sum()), mode="replace" if replace else "append",
    )
    verb = "Replaced the table with" if replace else "Added"
    st.session_state[f"_paste_toast_{location}"] = (
        "success",
        f"✅ {verb} **{len(parsed)}** row(s) · "
        f"**{int(parsed['Qty'].sum())}** unit(s).",
    )
    _defer_rerun()


def _render_paste_import(machine_df, acc_df, location: str,
                         seed_key: str, rev_key: str) -> None:
    """Sidebar expander: paste rows copied from a spreadsheet straight into the
    manual entries table (via session_state[seed_key])."""
    machine_skus = (
        machine_df["SKU"].astype(str).tolist()
        if machine_df is not None and not machine_df.empty else []
    )
    acc_skus = (
        acc_df["SKU"].astype(str).tolist()
        if acc_df is not None and not acc_df.empty else []
    )

    nonce_key = f"paste_nonce_{location}"
    st.session_state.setdefault(nonce_key, 0)

    with st.sidebar.expander("📋 Paste a list from Excel", expanded=False):
        # Replay the one-shot confirmation stashed before the rerun
        toast = st.session_state.pop(f"_paste_toast_{location}", None)
        if toast:
            level, msg = toast
            (st.success if level == "success" else st.info)(msg)

        st.caption(
            "Copy the cells out of your spreadsheet and paste them here — "
            "one row per SKU. **Accessory** and **Qty** columns are optional "
            "(no quantity ⇒ 1 each), and a header row is fine. The rows land "
            "in the table below, where you can still edit them or add more."
        )
        text = st.text_area(
            "Pasted rows",
            key=f"paste_text_{location}_v{st.session_state[nonce_key]}",
            height=140,
            placeholder=(
                "BOSS25-010\tBOSS25-A016\t5\n"
                "BOSS70-002\t\t2\n"
                "SDG25\t\t1"
            ),
            label_visibility="collapsed",
            help=(
                "Works with anything a spreadsheet puts on the clipboard: "
                "tab-separated cells, CSV, or a plain list of SKUs one per "
                "line. Column order doesn't matter — the SKUs are matched "
                "against the catalogs to work out which column is which."
            ),
        )
        combine = st.checkbox(
            "Combine duplicate SKUs",
            value=True,
            key=f"paste_combine_{location}",
            help=(
                "Rows with the same FG + accessory pair are merged into one "
                "row with the quantities added up. Uncheck to keep them "
                "as separate lines."
            ),
        )

        if not str(text or "").strip():
            return

        parsed, report = _parse_pasted_skus(
            text, machine_skus=machine_skus, acc_skus=acc_skus,
            combine_duplicates=combine,
        )

        def _render_skipped() -> None:
            if not report["skipped"]:
                return
            with st.expander(f"⚠️ {len(report['skipped'])} line(s) skipped", expanded=False):
                for s in report["skipped"][:25]:
                    st.markdown(f"- **Line {s['line']}** — {s['reason']}  \n  `{s['text']}`")
                if len(report["skipped"]) > 25:
                    st.caption(f"…and {len(report['skipped']) - 25} more.")

        if parsed.empty:
            st.warning(
                "Couldn't read any usable rows out of that. Each line needs an "
                "**FG SKU**; quantity is optional and defaults to 1."
            )
            _render_skipped()
            return

        # How the paste was read — so a mis-detected column is obvious before
        # the rows are committed, not after.
        col_bits = []
        for role in _PASTE_COLUMNS:
            idx = report["columns"].get(role)
            col_bits.append(f"{role} ← col {idx + 1}" if idx is not None else f"{role} ← —")
        st.caption(
            f"**{report['rows']}** row(s) · **{report['units']}** unit(s) · "
            f"read as {_PASTE_DELIM_LABEL.get(report['delimiter'], report['delimiter'])}"
            + (" · header row detected" if report["header"] else "")
            + "  \n" + " · ".join(col_bits)
        )

        # Short headers so all three columns fit the sidebar's width
        st.dataframe(
            parsed.head(25).rename(columns={"FG SKU": "FG", "Accessory SKU": "Acc"}),
            width="stretch",
            hide_index=True,
        )
        if len(parsed) > 25:
            st.caption(f"…and {len(parsed) - 25} more row(s) not shown.")

        notes = []
        if report["qty_defaulted"]:
            notes.append(f"{report['qty_defaulted']} row(s) had no quantity → counted as 1 each.")
        if report["merged"]:
            notes.append(f"{report['merged']} duplicate row(s) combined.")
        if report["qty_rounded"]:
            notes.append(f"{report['qty_rounded']} fractional quantity(ies) rounded to whole units.")
        if report["dropped_acc"]:
            notes.append(f"{report['dropped_acc']} accessory cell(s) dropped (formula-like value).")
        if notes:
            st.caption("ℹ️ " + "  \n".join(notes))

        for label, missing, where in (
            ("machine catalog", report["unknown_fg"], "🏭 Machine catalog"),
            ("accessory catalog", report["unknown_acc"], "🔌 Accessory catalog"),
        ):
            if missing:
                shown = ", ".join(f"`{s}`" for s in missing[:8])
                more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
                st.warning(
                    f"⚠️ Not in the {label}: {shown}{more}. They'll still import, "
                    f"but their labor won't be counted until you add them in "
                    f"**📁 Data & Setup → {where} → ➕ Add a new SKU**."
                )

        _render_skipped()

        c1, c2 = st.columns(2)
        with c1:
            add_clicked = st.button(
                "➕ Add", width="stretch",
                key=f"paste_add_{location}",
                help="Append these rows to whatever is already in the table below.",
            )
        with c2:
            replace_clicked = st.button(
                "🔁 Replace", width="stretch",
                key=f"paste_replace_{location}",
                help="Clear the table below and use only these rows.",
            )
        if add_clicked or replace_clicked:
            _paste_commit(parsed, location, seed_key, rev_key, nonce_key,
                          replace=replace_clicked)


def _render_sku_browser(machine_df, acc_df, location: str, seed_key: str, rev_key: str) -> None:
    """Sidebar expander that lets the user filter the machine catalog by
    family + class and add a chosen FG SKU + accessory + qty into the
    manual entries table (via session_state[seed_key])."""
    # Build a working frame with classification columns we can filter on.
    # reset_index(drop=True) avoids the SKU index/column ambiguity that
    # bites sort_values later.
    work = machine_df.reset_index(drop=True).copy()
    work["__family"] = work["SKU"].astype(str).apply(_machine_family)
    work["__class"] = work.apply(
        lambda r: _machine_class(r["SKU"], r.get("Description", "")), axis=1,
    )

    # Helpers used by both directions
    def _fg_label_for_row(row):
        bat = int(row.get("Bat", 0) or 0)
        cls = row.get("__class", "")
        bat_part = f" · {bat} batt" if bat > 0 else ""
        placeholder = " ⚠" if cls == "⚠ Placeholder" else ""
        return f"{row['SKU']} — {cls}{bat_part}{placeholder}"

    def _acc_label(sku: str) -> str:
        if sku == "(none)":
            return "(no accessory)"
        placeholder = " ⚠ family placeholder" if "999" in sku or "XXX" in sku.upper() else ""
        return f"{sku}{placeholder}"

    def _commit_add(fg_choice: str, acc_choice: str, qty: int) -> None:
        """Append the chosen row to the manual entries DataFrame and bump the rev."""
        editor_key = f"manual_entries_{location}_v{st.session_state.get(rev_key, 0)}"
        latest = st.session_state.get(editor_key)
        if isinstance(latest, pd.DataFrame):
            current = latest.copy()
        else:
            current = st.session_state.get(
                seed_key, pd.DataFrame(columns=["FG SKU", "Accessory SKU", "Qty"]),
            ).copy()
        current = pd.concat(
            [current, pd.DataFrame([{
                "FG SKU": fg_choice,
                "Accessory SKU": "" if acc_choice == "(none)" else acc_choice,
                "Qty": int(qty),
            }])],
            ignore_index=True,
        )
        st.session_state[seed_key] = current
        st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
        st.session_state[f"_browse_toast_{location}"] = (
            f"✅ Added {fg_choice} × {int(qty)} to manual entries."
        )
        _defer_rerun()

    with st.sidebar.expander("📚 Browse catalog & add SKU", expanded=False):
        _toast = st.session_state.pop(f"_browse_toast_{location}", None)
        if _toast:
            st.success(_toast)
        direction = st.radio(
            "Start from",
            options=["FG SKU first", "Accessory SKU first"],
            horizontal=True,
            help=(
                "**FG SKU first** — pick a finished good, then match accessory.  \n"
                "**Accessory SKU first** — pick an accessory, then match a finished good."
            ),
            key=f"browse_direction_{location}",
        )

        if direction == "FG SKU first":
            _render_fg_first(work, acc_df, location, _fg_label_for_row, _acc_label, _commit_add)
        else:
            _render_acc_first(work, acc_df, location, _fg_label_for_row, _acc_label, _commit_add)


def _render_fg_first(work, acc_df, location, fg_label_for_row, acc_label, commit_add):
    """FG-first browse flow: Family → Class → FG SKU → Accessory → Qty."""
    family_counts = work["__family"].value_counts()
    ordered_families = [f for f in KNOWN_FAMILIES if f in family_counts.index]
    if "OTHER" in family_counts.index:
        ordered_families.append("OTHER")
    family_choice = st.selectbox(
        "Family",
        options=ordered_families,
        format_func=lambda f: f"{f} ({family_counts.get(f, 0)})",
        key=f"browse_family_{location}",
    )

    in_family = work[work["__family"] == family_choice]
    class_counts = in_family["__class"].value_counts()
    class_order_pref = ["Standard Trailer", "Hybrid", "Power Module",
                        "Head Trailer", "Standard", "⚠ Placeholder"]
    ordered_classes = [c for c in class_order_pref if c in class_counts.index]
    for c in class_counts.index:
        if c not in ordered_classes:
            ordered_classes.append(c)
    class_choice = st.selectbox(
        "Class",
        options=["All classes"] + ordered_classes,
        key=f"browse_class_{location}",
    )

    if class_choice == "All classes":
        sku_rows = in_family
    else:
        sku_rows = in_family[in_family["__class"] == class_choice]
    sku_rows = sku_rows.sort_values(by="SKU")
    sku_options = sku_rows["SKU"].astype(str).tolist()
    if not sku_options:
        st.info("No SKUs match this family + class combination.")
        return

    fg_choice = st.selectbox(
        "FG SKU",
        options=sku_options,
        format_func=lambda s: fg_label_for_row(sku_rows[sku_rows["SKU"] == s].iloc[0]),
        key=f"browse_fg_{location}",
    )

    acc_options = ["(none)"]
    if acc_df is not None and not acc_df.empty:
        acc_work = acc_df.reset_index(drop=True).copy()
        acc_skus = acc_work["SKU"].astype(str)
        acc_mask = acc_skus.apply(_accessory_family_hint).str.upper() == family_choice.upper()
        acc_subset = acc_work[acc_mask].sort_values(by="SKU")
        acc_options.extend(acc_subset["SKU"].astype(str).tolist())

    acc_choice = st.selectbox(
        "Accessory",
        options=acc_options,
        format_func=acc_label,
        key=f"browse_acc_{location}",
    )
    qty = st.number_input(
        "Qty", min_value=1, max_value=999, value=1, step=1,
        key=f"browse_qty_{location}",
    )
    if st.button("➕ Add to manual entries", width="stretch",
                 key=f"browse_add_{location}"):
        commit_add(fg_choice, acc_choice, qty)


def _render_acc_first(work, acc_df, location, fg_label_for_row, acc_label, commit_add):
    """Accessory-first browse flow: Family → Accessory SKU → matching FG SKUs → Qty.

    Useful when the planner knows which accessory kit a customer ordered and
    wants the system to suggest a compatible finished-good unit.
    """
    if acc_df is None or acc_df.empty:
        st.info("No accessory catalog loaded.")
        return

    acc_work = acc_df.reset_index(drop=True).copy()
    acc_work["__family"] = acc_work["SKU"].astype(str).apply(_accessory_family_hint).str.upper()

    # Family selector — uses accessory-side family counts
    fam_counts = acc_work["__family"].value_counts()
    ordered_families = [f for f in KNOWN_FAMILIES if f.upper() in fam_counts.index]
    if not ordered_families:
        st.info("No accessories grouped by known families.")
        return
    family_choice = st.selectbox(
        "Family (accessories)",
        options=ordered_families,
        format_func=lambda f: f"{f} ({int(fam_counts.get(f.upper(), 0))})",
        key=f"browse_acc_family_{location}",
    )

    # Accessory SKU dropdown (filtered to chosen family)
    family_up = family_choice.upper()
    in_family_acc = acc_work[acc_work["__family"] == family_up].sort_values(by="SKU")
    acc_options = in_family_acc["SKU"].astype(str).tolist()
    if not acc_options:
        st.info("No accessories in this family.")
        return
    acc_choice = st.selectbox(
        "Accessory SKU",
        options=acc_options,
        format_func=acc_label,
        key=f"browse_acc_pick_{location}",
    )

    # Show description hint for the chosen accessory
    chosen_row = in_family_acc[in_family_acc["SKU"] == acc_choice]
    if not chosen_row.empty:
        desc = str(chosen_row.iloc[0].get("Description", "") or "").strip()
        if desc:
            st.caption(f"📋 {desc}")

    # Suggested FG SKUs — machines in the SAME family as the accessory
    matching_fg = work[work["__family"] == family_choice]
    # Allow optional class filter for the FG suggestion too
    fg_class_counts = matching_fg["__class"].value_counts()
    class_order_pref = ["Standard Trailer", "Hybrid", "Power Module",
                        "Head Trailer", "Standard", "⚠ Placeholder"]
    ordered_classes = [c for c in class_order_pref if c in fg_class_counts.index]
    for c in fg_class_counts.index:
        if c not in ordered_classes:
            ordered_classes.append(c)
    class_choice = st.selectbox(
        "FG class filter",
        options=["All classes"] + ordered_classes,
        key=f"browse_acc_fgclass_{location}",
    )
    if class_choice != "All classes":
        matching_fg = matching_fg[matching_fg["__class"] == class_choice]
    matching_fg = matching_fg.sort_values(by="SKU")
    fg_options = matching_fg["SKU"].astype(str).tolist()

    if not fg_options:
        st.warning(
            f"No machine SKUs match the **{family_choice}** family for accessory "
            f"`{acc_choice}`. Add the accessory by itself or pick a different "
            "accessory family."
        )
        return

    fg_choice = st.selectbox(
        "Suggested FG SKU",
        options=fg_options,
        format_func=lambda s: fg_label_for_row(matching_fg[matching_fg["SKU"] == s].iloc[0]),
        key=f"browse_acc_fg_{location}",
    )

    qty = st.number_input(
        "Qty", min_value=1, max_value=999, value=1, step=1,
        key=f"browse_acc_qty_{location}",
    )
    if st.button("➕ Add to manual entries", width="stretch",
                 key=f"browse_acc_add_{location}"):
        commit_add(fg_choice, acc_choice, qty)


# =============================================================
# Sidebar — global inputs
# =============================================================
def _render_admin_dashboard() -> None:
    """Hidden analytics view — KPIs from data/usage_log.jsonl.

    Rendered inside an ``⚙️ Admin`` expander at the bottom of the sidebar.
    No password gate: the expander label is intentionally generic so regular
    users don't notice it; only read-only charts/tables are shown, so there's
    nothing destructive even if a curious user opens it.
    """
    # Pull the on-disk log + any in-session un-flushed events
    try:
        mtime = _USAGE_LOG_PATH.stat().st_mtime if _USAGE_LOG_PATH.exists() else 0.0
    except Exception:
        mtime = 0.0
    events: list = list(_load_usage_log_cached(mtime) or [])
    events.extend(_usage_buffer_snapshot())

    if not events:
        st.caption("No usage events yet. Once people start using the tool, you'll see KPIs here.")
        if st.button("🔄 Refresh", key="_admin_refresh_empty"):
            try:
                _load_usage_log_cached.clear()
            except Exception:
                pass
            st.rerun()
        return

    # ---- Build dataframe ----------------------------------------------
    df = pd.DataFrame(events)
    if "ts" not in df.columns:
        st.caption("Usage log is malformed — no `ts` column.")
        return
    # Parse timestamps with utc=True so old naive entries (which were UTC
    # wall-clock from Streamlit Cloud) and new tz-aware LA entries land in
    # the same column. Convert to Las Vegas so the rendered "When" matches
    # the planner's clock regardless of which generation wrote the row.
    df["ts"] = (
        pd.to_datetime(df["ts"], errors="coerce", utc=True)
          .dt.tz_convert("America/Los_Angeles")
    )
    df = df.dropna(subset=["ts"]).copy()
    if df.empty:
        st.caption("No parseable events yet.")
        return
    df["date"] = df["ts"].dt.date
    df["type"] = df.get("type", "").astype(str)
    df["facility"] = df.get("facility", "").astype(str) if "facility" in df.columns else ""
    df["uid"] = df.get("uid", "").astype(str) if "uid" in df.columns else ""
    df["file"] = df.get("file", "").astype(str) if "file" in df.columns else ""

    # Admin time buckets in Las Vegas wall-clock so they match the timestamps
    # the rest of the app writes (Streamlit Cloud's servers run UTC).
    today = now_local().date()
    week_ago = today - pd.Timedelta(days=6)
    fortnight_ago = today - pd.Timedelta(days=13)

    sessions = df[df["type"] == "session_start"].copy()
    sessions_today = int(((sessions["date"] == today)).sum())
    sessions_week = int(((sessions["date"] >= week_ago)).sum())
    sessions_all = int(len(sessions))

    # ---- KPIs (compact metric row) ------------------------------------
    st.markdown("**📈 Sessions**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Today", sessions_today)
    c2.metric("This week", sessions_week)
    c3.metric("All-time", sessions_all)

    # Last 14 days line chart
    if not sessions.empty:
        daily = (
            sessions.assign(d=sessions["ts"].dt.date)
                    .groupby("d").size().reset_index(name="sessions")
        )
        # Re-index over a full 14-day window so empty days appear as zero
        full_range = pd.date_range(end=today, periods=14, freq="D").date
        full_df = pd.DataFrame({"d": full_range})
        merged = full_df.merge(daily, on="d", how="left").fillna({"sessions": 0})
        fig = px.line(
            merged, x="d", y="sessions", markers=True,
            labels={"d": "", "sessions": "Sessions"},
        )
        fig.update_layout(
            height=160, margin=dict(l=4, r=4, t=8, b=4),
            showlegend=False,
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.caption("_No session events yet._")

    # ---- Facility breakdown -------------------------------------------
    st.markdown("**🏭 Sessions by facility**")
    if sessions.empty or sessions["facility"].eq("").all():
        st.caption("_No facility data yet._")
    else:
        fac = (
            sessions[sessions["facility"] != ""]
            .groupby("facility").size().reset_index(name="sessions")
            .sort_values("sessions", ascending=False)
        )
        cols = st.columns(min(len(fac), 3) or 1)
        for i, row in enumerate(fac.itertuples(index=False)):
            cols[i % len(cols)].metric(row.facility, int(row.sessions))

    # ---- Saves (last 14 days) -----------------------------------------
    st.markdown("**💾 Saves (last 14 days)**")
    # Every event that writes to GitHub (or applies a plan-level change). Keep
    # in step with the _track_and_flush("...") calls across app.py.
    save_types = {
        "catalog_save", "catalog_add_sku", "catalog_add_placeholder",
        "recon_apply", "scenario_save", "scenario_delete",
        "schedule_save", "schedule_delete", "exec_scenario_save",
        "assumptions_save", "missing_sku_import", "plan_month_transfer",
        "flow_save", "flow_reset", "simple_save", "crew_save",
    }
    saves = df[(df["type"].isin(save_types)) & (df["date"] >= fortnight_ago)].copy()
    if saves.empty:
        st.caption("_No saves in the last 14 days._")
    else:
        # Group by file when present, else by event type
        saves["bucket"] = saves["file"].where(saves["file"] != "", saves["type"])
        agg = (
            saves.groupby(["bucket", "type"]).size()
            .reset_index(name="count").sort_values("count", ascending=False)
        )
        agg.columns = ["File / target", "Event", "Count"]
        st.dataframe(agg, width="stretch", hide_index=True)

    # ---- Top actions ---------------------------------------------------
    st.markdown("**🎯 Top actions (last 14 days)**")
    recent = df[df["date"] >= fortnight_ago].copy()
    if recent.empty:
        st.caption("_No events in the last 14 days._")
    else:
        top = (
            recent.groupby("type").size().reset_index(name="count")
                  .sort_values("count", ascending=False).head(10)
        )
        top.columns = ["Event type", "Count"]
        st.dataframe(top, width="stretch", hide_index=True)

    # ---- Recent activity table ----------------------------------------
    st.markdown("**📋 Recent activity (last 20 events)**")
    last20 = df.sort_values("ts", ascending=False).head(20).copy()
    last20["When"] = last20["ts"].dt.strftime("%m-%d %H:%M")
    last20["UID"] = last20["uid"].str[-6:]
    cols_to_show = ["When", "UID", "type", "facility", "file"]
    cols_to_show = [c for c in cols_to_show if c in last20.columns]
    st.dataframe(
        last20[cols_to_show].rename(
            columns={"type": "Event", "facility": "Facility", "file": "File"}
        ),
        width="stretch", hide_index=True,
    )

    # ---- Refresh button ------------------------------------------------
    st.markdown("---")
    cc1, cc2 = st.columns([3, 2])
    cc1.caption(
        f"Buffered (un-flushed) this session: {len(_usage_buffer_snapshot())} event(s)"
    )
    if cc2.button("🔄 Refresh", key="_admin_refresh", width="stretch"):
        try:
            _load_usage_log_cached.clear()
        except Exception:
            pass
        st.rerun()


@st.fragment
def _render_help_glossary() -> None:
    """Body of the sidebar "ℹ️ Help & glossary" expander.

    A collapsed st.expander still executes its body on every rerun, so the
    five help tabs (a few hundred lines of markdown) used to be built on
    every click. They now render only once the planner flips the toggle, and
    the toggle lives in a fragment so flipping it reruns just this expander,
    not main().
    """
    if not st.toggle(
        "Show help", key="_show_help",
        help="Loads the quick start, model notes, catalog guide, saving tips and admin notes.",
    ):
        st.caption("Flip the switch to load the help pages.")
        return
    with st.container():
        # Five focused tabs replace the previous 85-line wall of markdown.
        # Tabs render cleanly inside expanders and give planners a way to
        # jump straight to the section they need.
        _help_quick, _help_model, _help_catalog, _help_save, _help_admin = st.tabs([
            "🚀 Quick start",
            "📐 How the model works",
            "🏷 Catalog & stations",
            "💾 Saving your work",
            "🔧 For admins",
        ])

        with _help_quick:
            st.markdown(
                """
**Status colors**
🟢 OK · 🟡 Tight · 🟠 Near capacity · 🔴 Over capacity · ⚪ Not at this facility

**Sidebar in four steps**
1. **Pick a facility** — Henderson, Spartanburg, or Cypress. Each has its own crew config.
2. **Tell us what to build** — upload your monthly schedule (CSV or Excel workbook), or paste / type a few SKUs for a quick what-if. The newest schedule saved for the facility loads on its own.
3. **Plan month & window** — the month this plan is for, and whether to look at the whole month, this week, or the rest of the month.
4. **Capacity settings** — working days, shift length, safety, efficiency, workforce availability, and per-station headcount under the two expanders.

The **🧭 Plan guide** at the top of the page walks the same order — what's loaded, which month, whether every SKU is in the catalogs, whether the plan fits, and saving it — and carries the button for whatever needs doing next.

**Pasting a list from Excel**
Pick **✏️ Paste or type SKUs** in step 2, open **📋 Paste a list from Excel**, and paste the cells you copied. You don't need the full schedule layout — just SKUs, with an accessory column and a quantity column if you have them:

| What you paste | How it's read |
|---|---|
| One SKU per line | Qty 1 each |
| `SKU` + number | FG SKU + quantity |
| `SKU` + `SKU` | FG SKU + accessory |
| `SKU` + `SKU` + number | FG SKU + accessory + quantity |

Column order doesn't matter — pasted SKUs are matched against the catalogs to work out which column is which, and a header row (`FG SKU ID`, `Qty`, …) is understood if you copied one. Extra columns like LOCATION or CUSTOMER are ignored. The preview shows what was read before anything is committed; then **➕ Add** appends to what's already there and **🔁 Replace** starts over. Either way the rows land in the editable table below, so you can still fix a SKU or add a few more by hand.

The table's **`#`** column numbers the rows for you. A row takes the next number as soon as it has an FG SKU or an Accessory SKU in it — however it got there (pasted through the panel, pasted straight into the table, or typed by hand). Blank rows stay unnumbered and don't consume a number, and deleting a row closes the gap. It's display-only: it isn't part of the schedule and isn't saved with a scenario.

**Where to find common features**

| Want to… | Go to |
|---|---|
| Paste a SKU list out of Excel | Sidebar → **✏️ Paste or type SKUs** → **📋 Paste a list from Excel** |
| Save your manual build plan | Sidebar → **📂 Save / load scenarios** (when in "Paste or type SKUs" mode) |
| Save your uploaded schedule — including any Sequencer / 🗓 Sequence / 🧹 cleanup changes | Sidebar → **📂 Save / load uploaded schedules** → **💾 Save schedule** (when in "Upload schedule file" mode) |
| Get a blank upload template | Sidebar → **📥 Download blank template** (under the file uploader) |
| Edit labor times | **📁 Data & Setup** tab → Machine / Accessory catalog |
| Add a missing SKU | **📁 Data & Setup** → catalog → **➕ Add a new SKU** |
| See what gets built each week / day | **📆 Calendar** tab |
| Reshape the schedule (move SKUs between dates) | **📁 Data & Setup** → **🗓 Sequence** |
| See bottlenecks + suggested fixes | **🏠 Overview** → 🎯 Recommended actions, or **🔧 Recommendations** tab |
| Get the whole database (or this analysis) into Excel | **📁 Data & Setup** → **📤 Export & Excel link** |
| Link an Excel sheet to the live catalogs | **📁 Data & Setup** → **📤 Export & Excel link** → step 3 |
| Push labor edits made in Excel back into the catalogs | **📁 Data & Setup** → **📥 Import from Excel** |

**Tip:** hover any column header in a table to see its specific tooltip.
                """
            )

        with _help_model:
            st.markdown(
                """
### Time units
- **Person-minutes (p-min)** — labor *effort*. 1 person working 1 min = 1 p-min. 2 people working 30 min = 60 p-min. Independent of how many people you assign.
- **Calendar minutes (cal-min)** — wall-clock *elapsed* time. With 1 person doing 60 p-min, cycle time is 60 cal-min. With 2 people working in parallel, the cycle is 30 cal-min.
- **Cycle time (processing time)** — calendar minutes a unit physically occupies a station = `p-min ÷ Crew per unit`. Strictly this is the per-unit *processing time*; with several parallel cells the true line cycle (gap between finished units) is shorter (≈ processing ÷ cells).
- **Lead time (days)** — calendar days for **one** person to do the whole unit's labor alone = `Total p-min ÷ (shift × efficiency)`. A single-operator *labor-content* yardstick for comparing units — **not** a delivery lead time (it excludes queue, move, and wait between stations).

### People & stations
- **Headcount (HC) / People** — number of people assigned to a station for the whole shift.
- **Stations/Cells (Conc)** — how many identical bays/cells run in parallel at one station (units worked on at the same time) — *not* the number of process steps.
- **Crew per unit** — number of people working together on one unit at a station. Drives processing (cycle) time.
- **Required HC** — people needed to meet the schedule given safety + efficiency, rounded up per station. The facility total sums those per-station round-ups (assumes dedicated crews; shared floaters could need fewer).

### Safety & efficiency
- **Safety factor** — planning buffer applied to capacity. `0.85` = leave 15% slack.
- **Efficiency factor** — share of the paid shift that becomes productive work after breaks, setup, rework, and line-balance loss; behaves like OEE availability × performance. `1.00` = no loss; `0.625` ≈ 5 productive hours in an 8-hr shift (a planning convention, not an external standard). Typical assembly plans run `0.80–0.90`.
- **Effective capacity** = `HC × shift × days × efficiency × safety` person-minutes per period.

### Labor vs Throughput utilization

Each station has **two** utilization checks. The overall flag (🟢/🟡/🟠/🔴) is the worst of the two — because either one can be the actual bottleneck, and the fix is different.

*These are **load ratios** — demand ÷ the **buffered** capacity (already cut by efficiency × safety). So 100% means "demand exactly fills the buffered allowance," not "the resource is 100% clock-busy," and the value can exceed 100%.*

**Labor utilization** — *"Do I have enough people?"*
- `labor_util = labor_demand ÷ labor_capacity`
- `labor_capacity = HC × shift × days × efficiency × safety` (person-minutes available).
- **Fix when red:** hire more, run overtime, extend the work week. Tuned by the **HC** sidebar control.

**Throughput utilization** — *"Do I have enough stations/fixtures/bays?"*
- `thru_util = units_per_day ÷ thru_capacity`
- `thru_capacity = Stations/Cells × (shift ÷ avg_cycle) × efficiency × safety` (units/day the station can physically push through).
- **Fix when red:** add more cells, shorten cycle time, extend working days. **Adding people only helps if the step is labor-paced** (more crew lowers `p-min ÷ Crew`, so each cell clears units faster); for **machine/process-paced** steps like Battery, crew size can't change the per-cell time — you need more cells. Tuned by the **Stations/Cells** sidebar control.

**The classic example: Battery Assembly.** Each battery cell takes ~170 calendar minutes to build, regardless of how many people you put around it. If you have 4 cells but the plan needs 50 batteries/day, throughput maxes out at ~11/day — hiring more people does nothing. You need more cells.

The Overview's **🎯 Recommended actions** splits these two cases so you don't waste a hiring request on a throughput bottleneck.

**Takt time** (the demand beat = available productive time ÷ units needed/day) isn't shown directly — the **Need/day vs. cell capacity** check is the equivalent: a station keeps up when its processing time per unit ÷ parallel cells stays at or below takt.

| If this is red | What's pinching | Where to look |
|---|---|---|
| Labor util | Headcount | Sidebar → **People (HC)** |
| Throughput util | Physical capacity | Sidebar → **Stations/Cells** |
| Both | Both — fix the cells first, hire to fill them | Both controls |
                """
            )

        with _help_catalog:
            st.markdown(
                """
### Unit classes
- **STD** — Standard trailer (full assembly with marry)
- **HS** — Head Skid only (no trailer)
- **HT** — Head + Trailer (mount only, no marry)

### SKU naming
- **FG SKU** — finished-good SKU from `machine_clean.csv` (e.g. `BOSS25-006`).
- **Accessory SKU** — accessory kit from `acc_clean.csv` (e.g. `BOSS25-A016`).
- **Bat** — battery count for that FG SKU. PDS/SDG = 0.
- **Last Modified** — date the row was last edited through the app (blank = never).

### Placeholders
- **`XXX` in a SKU** — estimation placeholder. Labor is an estimate, not measured. E.g. `BOSS220PM XXX`, `BOSS25 AXXX`. The Data Quality view flags any `XXX` row automatically.
- Older `-A999` style placeholders may still appear in a few legacy catalog rows; they've been superseded by `XXX` for any new entries.

### Stations (key → display name)
- `Warehouse` = Warehouse (Pick) — pull parts
- `Wire` = Wire Assembly — pre-wiring sub-assembly
- `Battery` = Battery Assembly — battery cells (BOSS only)
- `PMAcc` = PM Acc (Headunit) — PM/Head-unit accessories
- `GenAcc` = Gen Accessories — generator-side accessories
- `Undercarriage` = Undercarriage Assembly — PDS (compressor) chassis step, runs before Com Accessories
- `ComAcc` = Com Accessories — compressor-side accessories
- `Trailer` = Trailer Assembly — trailer sub-assembly (STD/HT only)
- `AccKIT` = Accessories KIT — kit prep
- `ETO` = Engineering To Order — special engineering (BOSS220/BOSS400)
- `Final` = Final Assembly — mount + marry (where used)
- `PDI / QC / Ship` — Pre-Delivery Inspection · Quality Check · Shipping
                """
            )

        with _help_save:
            st.markdown(
                """
Answers the question planners ask once they care: *"will the team see what I just did?"*

### Save / load scenarios (manual mode)
When you're in **✏️ Paste or type SKUs** mode, the sidebar's **📂 Save / load scenarios** expander lets you name and save your manual SKU list. Saved per facility to `data/scenarios.json` on GitHub — anyone planning for the same facility can reload them.

### Save / load uploaded schedules (upload mode)
When you're in **📤 Upload schedule file** mode, the sidebar's **📂 Save / load uploaded schedules** expander lets you save the schedule the app is analysing under a name — your upload as loaded, or, after a Sequencer **✅ Accept**, a 🗓 **🔄 Apply edits**, a **🔀 Bulk move**, a 🧹 **Strip**, or a **📅 Move into plan month**, the reshaped plan. A schedule loaded from the list can be edited and re-saved the same way (the uploader doesn't need to hold a file). Saved per facility to `data/uploaded_schedules.json` on GitHub. An untouched upload is saved as its CSV (an Excel upload as the CSV it was converted to), so any columns the app currently ignores still survive the round-trip for future use.

### Export & Excel link
**📁 Data & Setup → 📤 Export & Excel link** downloads the whole database (catalogs, crew, settings, scenarios, executive scenarios, saved schedules, process flow) as one `.xlsx`, or this session's schedule / per-unit labor / capacity tables. Step 3 there lists the raw GitHub URL of each CSV catalog plus a Power Query script, so an Excel sheet can read the live data and **Refresh All** whenever the team saves.

### Import from Excel
**📁 Data & Setup → 📥 Import from Excel** is the way back. Export the database workbook, edit labor minutes on the **Machine catalog** / **Accessory catalog** sheet (or add a SKU row at the bottom), save, and upload the file. The preview lists every cell that would change (SKU, column, old → new), every new SKU, and every catalog SKU that isn't on the sheet — those are left untouched; an import never deletes a row. Blank cells never change anything. A value that isn't a number, a negative, a routing value the dropdown wouldn't offer, or a SKU listed twice blocks the save until you fix the file. **💾 Save** then fetches the latest file from GitHub and merges only those cells on top — the same SHA-locked save the editors use, so a colleague's edit of another cell in the meantime survives.

### Catalog edits
Every **💾 Save** button in **📁 Data & Setup** (Machine catalog, Accessory catalog, Items, Item packages, Reconciliation, Process flow) pushes to GitHub. Other users see the new values after the next ~1 minute Streamlit Cloud redeploy.

### Confirmation pill
After every successful schedule load, the sidebar shows a green ✅ confirmation like *"Loaded 193 rows from HENDERSON · month May-26"* so you can verify the loader understood your file correctly.

### Unknown-SKU warning banner
If your schedule references FG or accessory SKUs that aren't in the catalogs, a yellow banner appears at the **top of the page** listing them. Add the missing SKUs via **📁 Data & Setup → 🏭 Machine catalog → ➕ Add a new machine SKU** (or the equivalent for accessories) and they'll be picked up on the next reload.

### Privacy
Uploaded CSVs stay **only in your browser** unless you explicitly click **💾 Save schedule**. They aren't written to disk on the server or pushed to GitHub by default.
                """
            )

        with _help_admin:
            st.markdown(
                """
### GitHub token
The 💾 Save buttons require a **fine-grained GitHub Personal Access Token** scoped to just this repo, with **Contents: Read and write** permission (see `MAINTAINING.md` → "Rotating the GitHub PAT"). Add it to **Streamlit Cloud → Settings → Secrets**:

```toml
github_token = "github_pat_xxxxxxxxxxxxxxxxxxxxxxxx"
```

If a planner sees *"GitHub token not configured"* when saving, the token is missing or expired. Read-only browsing works without it.

### Wild-CSV upload behavior
The loader auto-matches column names so the team can upload their normal Excel export without reformatting:
- **Required columns** (any of these naming patterns will match): `LOCATION` / `Facility` / `Site`; `FG SKU ID` / `FG SKU` / `Finished Good`; `FG ACCRY SKU ID` / `Accessory SKU` / `Acc SKU`; `BUILD QTY` / `Qty` / `Quantity`; `PRODUCTION MONTH` / `Prod Month` / `Production Date` / `Month`.
- **Optional:** `CUSTOMER NAME` / `Customer` / `Cust`; `PRODUCTION DAY` / `Prod Day` / `Build Date` / `Date`.
- **Extra columns are ignored** — pandas reads them all but the loader only uses the ones it recognises.
- **UTF-8 BOM is stripped** automatically — Excel's default save format on Windows just works.
- **Date formats:** `Mon-YY` (e.g. `May-26`), `YY-Mon` carryover (e.g. `26-Apr`), US slash (`5/8/2026`), and ISO (`2026-05-08`) all parse correctly.
- **Zero-qty rows** are dropped automatically.

If a required column truly isn't there, the planner sees a yellow banner naming the missing column and listing the aliases the loader tried.

### Auto-spread (level-loading dates)
When a planner uploads a schedule without `PRODUCTION DAY` values (or with some rows dated and others blank), the tool **auto-assigns dates** to the blanks so the weekly heatmap has a meaningful baseline.

The algorithm — Longest Processing Time (LPT) — sorts the blank rows by labor weight (sum of per-station p-min × `BUILD QTY`), largest first, then drops each onto the working day with the currently lightest running total. Result: weekly labor is roughly even, no week takes a disproportionate share of heavy units. Existing dated rows are preserved and their load seeds the day totals so the level-loading respects them.

A yellow banner at the top of the page tells the planner when auto-spread fired and how many rows it touched. The pill in the sidebar gets a `📅 Auto-leveled …` suffix.

**To override:** add real dates to your CSV's `PRODUCTION DAY` column and re-upload. Auto-spread won't touch rows that already have a date.

### Sequencer (Suggest + manual reshape)
The Sequencer turns the static schedule into something you can *re-balance* interactively.

**💡 Suggest a level-loaded schedule** — top-of-page expander. Click *Generate proposal* and the tool runs a constraint-aware LPT scheduler:
- Per-row labor weight = sum of per-station p-min × `BUILD QTY` (same path the rest of the analysis uses).
- For each blank row, sorted largest-first, walk candidate days from lightest to heaviest. For each day, check that placing the unit wouldn't tip *any* station above 100% weekly utilization (computed against your current HC, shift, safety, efficiency). Take the first day that passes.
- If no day satisfies the constraints, the row lands on the least-loaded day anyway and is flagged in an *overload diagnosis* showing which stations would be tipped over.

The proposal appears as a side-by-side preview heatmap on the Overview (Current vs Proposed). **✅ Accept** swaps the analysis to use the proposed sequence; **🚫 Discard** drops it.

Suggest / Accept work in both sidebar modes — with an uploaded schedule and with a pasted or typed SKU list.

**🗓 Sequence sub-section** (📁 Data & Setup) — manual reshape:
- *Editable table* — every row's `PRODUCTION DAY` is a date-picker cell. Edit, then click **🔄 Apply edits** to push the changes into the analysis.
- *🔀 Bulk move* form — shift N units of a chosen FG SKU from one week of the plan month to another in one click. Useful when you want to flatten a single problem week without per-row edits. The week list follows the plan month's calendar (a month that starts on a weekend has six Monday-anchored weeks); rows dated in another month aren't movable here — use **📅 Move … into plan month** at the top of the page first.
- *📥 Download* — reshaped schedule as CSV, for a copy you can re-upload later.

Reshape edits live in your browser session until you save. In **📤 Upload schedule file** mode, **💾 Save schedule** in the 📂 panel saves the reshaped plan for the team. In **✏️ Paste or type SKUs** mode the reshape is session-only — a saved scenario keeps the SKU list, not the dates — and changing the SKU rows in the sidebar grid drops it (it described a plan that no longer exists); download it to keep a copy, or upload the download in upload mode to save it. Switching modes discards a pending proposal.

**Week filters and carryover.** *This week*, the Overview's weekly heatmap, the Calendar and Bulk move all bucket rows by the **month and week** their `PRODUCTION DAY` falls in, so a carryover row dated in an earlier month never merges into this month's same-numbered week. When a schedule's dated rows span two months, the heatmap labels its columns `Sep Wk 5`, `Oct Wk 1`, ….

### 📆 Calendar (week & day rollups)
Top-level tab (sits between **🏠 Overview** and **📊 Capacity**). Bucketed view of *what gets built each week and each day* — one expandable card per week of the month showing total units, total labor (p-min), and the top FG SKUs. Open a card to see:
- **Daily breakdown** — one row per working day in that week: units built, labor p-min.
- **Per-station labor mix** — horizontal bar chart showing how the week's labor splits across Warehouse / Wire / Battery / … / Ship. Quick read on where the week's heat goes.
- **SKU detail** — collapsed table with every unit in the week (day, FG, accessory, qty, customer, location).

Use Calendar when you want the *what's being built when* answer; use Sequence when you want to *reshape* the dates.

### 🧹 Customer suffix cleanup
Lives under **📁 Data & Setup → 🧹 Customer suffix cleanup**. ERP exports often label SKUs with a customer-decal tail like `BOSS70-001HRC`, `BOSS25-006ES`, `BOSS70-012UR`. The labor analysis already collapses these to their canonical base internally, but the suffixed text shows in the Sequence editor, the Calendar's "Top SKUs" line, and the downloaded / saved CSV.

This sub-section detects every suffixed FG / Accessory SKU in your loaded schedule, lists them with their base (after stripping) and a ✓ / ✗ for whether the base exists in the catalog, and gives you two actions:
- **🔄 Strip suffixes in loaded schedule** — rewrites your active schedule's SKU columns to the canonical base form. The analysis numbers don't change; only the *displayed* SKUs do. Catalogs are not touched.
- **📥 Download cleaned schedule** — exports the cleaned CSV without committing the change.

Rows whose base isn't in the catalog (orphan suffixes) are left untouched so analysis doesn't silently lose them.

### Blank template
The **📥 Download blank template** button under the file uploader hands the planner an empty CSV with the 5 required headers + 2 example rows. Quickest way to ensure formatting is right.

### Usage analytics
At the very bottom of the sidebar, the **⚙️ Admin** expander shows session counts, top actions, save events, and recent activity pulled from `data/usage_log.jsonl`. Anonymous UUID per browser — no PII collected.

### Redeploy timing
Every catalog / scenario / schedule / flow save triggers a Streamlit Cloud rebuild (~1 minute). Other users see the new values when their browsers reload after that. The 🔄 Refresh banner at the top of the relevant tab tells you when there's an updated copy to pull in mid-session.
                """
            )


@st.fragment
def _render_admin_panel() -> None:
    """Body of the sidebar "⚙️ Admin" expander.

    Same reasoning as _render_help_glossary: the usage-log load and the
    plotly chart ran on every rerun even with the expander collapsed. The
    dashboard's own 🔄 Refresh button calls st.rerun(), which from inside a
    fragment still reruns the whole app (scope defaults to "app") — fine,
    it's a rare, deliberate click.
    """
    if not st.toggle(
        "Load analytics", key="_show_admin",
        help="Reads data/usage_log.jsonl and draws the usage KPIs.",
    ):
        st.caption("Flip the switch to load session counts and recent activity.")
        return
    _render_admin_dashboard()


def _schedule_detail_lines(summary: dict, location: str) -> list[str]:
    """Markdown lines explaining how the loaded file was read — the rows the
    loader skipped, the units it rescued or dropped and why, and the dates it
    auto-leveled. Shown once, in the plan guide's step 1 expander (they used
    to be five separate sidebar warnings plus two main-panel banners)."""
    lines: list[str] = []
    if not isinstance(summary, dict):
        return lines
    auto = summary.get("auto_spread")
    if isinstance(auto, dict) and auto.get("count"):
        month = auto.get("target_month_label", "")
        lines.append(
            f"📅 **{auto['count']} of {auto['total_rows']} rows had no PRODUCTION "
            f"DAY** and were auto-leveled across **{month}** ({auto['working_days']} "
            "working days) so weekly labor is roughly balanced — the weekly view "
            "reflects that synthetic sequence. Add a **PRODUCTION DAY** column to "
            "pin your own dates."
        )
    no_qty = int(summary.get("no_qty_rows") or 0)
    if no_qty:
        lines.append(f"⏭️ **{no_qty} row(s) had no BUILD QTY** and were skipped — 0 units lost.")
    other = int(summary.get("other_location_rows") or 0)
    if other:
        lines.append(
            f"🏭 **{other} row(s) tagged for another plant** were left out — tick "
            "**Count rows tagged for another plant** in the sidebar to include them."
        )
    rec = summary.get("recovered")
    if isinstance(rec, dict):
        by = rec.get("by_model") or {}
        if rec.get("count") and by:
            accm = rec.get("acc_by_model") or {}
            parts = []
            for m, (sku, n) in by.items():
                acc = accm.get(m)
                parts.append(f"{n}× **{m}** → `{sku}`" + (f" + `{acc[0]}`" if acc else ""))
            lines.append(
                f"🧩 **{rec['count']} unit(s) had no FG SKU ID** and run on the catalog's "
                f"estimation placeholder(s) ({'; '.join(parts)})"
                + (" — accessory-side labor estimated too" if rec.get("acc_count") else "")
                + ". Their labor is an **estimate**; replace with real SKUs when available."
            )
        unm = rec.get("unmatched") or {}
        if unm:
            ulines = "; ".join(f"{n}× **{m}**" for m, n in unm.items())
            lines.append(
                f"➖ **{sum(unm.values())} unit(s) are not counted**: no FG SKU ID and no "
                f"matching placeholder ({ulines}). Add an `XXX` placeholder for those "
                "models (📁 Data & Setup → 🏭 Machine catalog → ➕ Add a new machine "
                "SKU, with the model in the Description) or fill in the SKUs in the file."
            )
        nomodel = int(rec.get("no_model") or 0)
        if nomodel:
            lines.append(
                f"➖ **{nomodel} unit(s) are not counted**: neither an FG SKU ID nor a "
                "MODEL TYPE, so there was nothing to identify them by. Fill in either "
                "column in the file."
            )
    uns = summary.get("unscheduled")
    if isinstance(uns, dict) and uns.get("count"):
        rlist = "; ".join(f"{n}× {r}" for r, n in (uns.get("by_reason") or {}).items())
        rnote = f" ({rlist})" if rlist else ""
        amonth = uns.get("assigned_month")
        if amonth:
            lines.append(
                f"📆 **{uns['count']} unit(s) had no valid PRODUCTION MONTH** and were "
                f"counted in **{amonth}**{rnote}. Fill in their PRODUCTION MONTH to date "
                "them precisely."
            )
        else:
            lines.append(
                f"📆 **{uns['count']} unit(s) had no valid PRODUCTION MONTH** and no "
                f"current plan month to date them to{rnote}. They count in the totals "
                "but stay undated — fill in their PRODUCTION MONTH to place them."
            )
    fgn = summary.get("foreign_location")
    if isinstance(fgn, dict) and fgn.get("count"):
        plist = "; ".join(
            f"{n} tagged **{str(p).title()}**" for p, n in (fgn.get("by_location") or {}).items()
        )
        lines.append(
            f"🏭 **{fgn['count']} unit(s) tagged for another plant** were counted as "
            f"**{fgn.get('assigned_location') or location}** ({plist}). Untick **Count "
            "rows tagged for another plant** in the sidebar to exclude them."
        )
    loc = summary.get("location_recovered")
    if isinstance(loc, dict) and loc.get("count"):
        flist = "; ".join(f"{n}× {fg}" for fg, n in (loc.get("by_fg") or {}).items())
        fnote = f" ({flist})" if flist else ""
        lines.append(
            f"📍 **{loc['count']} unit(s) had a blank LOCATION** and were counted under "
            f"**{loc.get('assigned_location') or 'this facility'}**{fnote}. Set their "
            "LOCATION if any belong to a different plant."
        )
    return lines


def _render_upload_summary(slot, location: str, uploaded) -> None:
    """Draw the sidebar's "✅ Loaded N rows …" pill and the warnings about
    units the loader rescued or dropped, from ``_last_upload_summary``.

    Called by main() right after the schedule has loaded, into the container
    the sidebar reserved for it (``inputs["upload_summary_slot"]``) — so what
    the pill says is always the schedule the tabs are showing. ``uploaded`` is
    the live uploader value: with no summary and no upload, the bundled-sample
    note is shown instead. No-op in ✏️ manual mode (no slot).
    """
    if slot is None:
        return
    with slot:
        _summary = st.session_state.get("_last_upload_summary")
        if _summary and isinstance(_summary, dict):
            _src_label = {
                "upload":  "uploaded CSV",
                "saved":   "saved schedule",
                "bundled": "bundled sample",
            }.get(_summary.get("source", ""), "schedule")
            if _summary.get("source") == "upload" and _summary.get("sheet"):
                _src_label = f"uploaded Excel · sheet `{_summary['sheet']}`"
            # Note the CSV's own month when it differs from the Plan month, so
            # the planner understands why (e.g. a May export planned for June).
            _csv_month = _summary.get("csv_month", "")
            _shown_month = _summary.get("month", "—")
            _csv_note = ""
            if _csv_month and _csv_month not in ("—", _shown_month):
                _csv_note = f" _(CSV says {_csv_month})_"
            # "Loaded 277 of 288 rows", then one line per reason a row the
            # file had isn't in the plan, so the two numbers reconcile without
            # opening the CSV. (Rows with a quantity but no SKU are covered by
            # the placeholder warnings below, in units.)
            _rows = _summary.get("rows", "?")
            _src_rows = _summary.get("source_rows")
            _of = (
                f" of **{_src_rows}**"
                if isinstance(_src_rows, int) and _src_rows != _rows else ""
            )
            # Which file this is. A fresh session opens on the facility's
            # newest saved schedule, so say that it was picked automatically
            # and how to change it; a facility with no saved schedules gets
            # the sample, and says so.
            _name = _summary.get("name")
            _which = ""
            if _summary.get("source") == "saved" and _name:
                if _summary.get("auto_loaded"):
                    _which = (
                        f"  \n📂 _**{_name}** — the newest schedule saved for "
                        f"{_summary.get('location', 'this facility')}, loaded "
                        f"automatically. Upload a file, or pick another in "
                        f"**📂 Save / load uploaded schedules**._"
                    )
                else:
                    _which = f"  \n📂 _Saved schedule **{_name}**._"
            elif _summary.get("source") == "upload" and _name:
                _which = f"  \n📄 _**{_name}**_"
            elif _summary.get("source") == "bundled":
                _which = (
                    f"  \nℹ️ _No saved schedules for "
                    f"{_summary.get('location', 'this facility')} yet, so this is "
                    f"the bundled May 2026 sample schedule. Upload a file, or save "
                    f"one in **📂 Save / load uploaded schedules**._"
                )
            st.success(
                f"✅ Loaded **{_rows}**{_of} rows from "
                f"**{_summary.get('location', '?')}** "
                f"· month **{_shown_month}**{_csv_note} "
                f"({_src_label})"
                + _which
            )
            # Everything else about how the file was read — rows skipped,
            # units rescued or dropped, auto-leveled dates — is explained in
            # the plan guide's step 1 at the top of the page, once.
        elif uploaded is None and "_loaded_schedule_buffer" not in st.session_state:
            st.info("Using the bundled May 2026 sample schedule.")


def render_sidebar() -> dict:
    st.sidebar.title("🏭 Labor Planning")
    st.sidebar.caption("Set up your scenario, then review tabs on the right.")
    st.sidebar.markdown("---")

    # ----------------------------------------------------------------
    # STEP 1 — Facility (always visible)
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 1 · Pick a facility")
    location = st.sidebar.selectbox(
        "Facility",
        LOCATIONS,
        index=0,
        label_visibility="collapsed",
        help="Each facility has its own saved headcount and station setup.",
    )
    _note_facility(location)

    # ----------------------------------------------------------------
    # STEP 2 — Schedule (always visible)
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 2 · Tell us what to build")
    schedule_mode = st.sidebar.radio(
        "How would you like to enter the build plan?",
        [SCHEDULE_MODE_UPLOAD, SCHEDULE_MODE_MANUAL],
        index=0,
        help=(
            "Upload a full production schedule (CSV or Excel workbook) — best "
            "for monthly planning. Or paste a SKU list straight out of Excel "
            "(and add a few rows by hand) — best for quick what-if scenarios."
        ),
    )
    # A Sequencer proposal or a reshape waiting in the one-shot buffer was
    # computed against the OTHER mode's schedule; consuming it after a switch
    # would silently replace this mode's plan with the old mode's rows.
    _note_schedule_mode(schedule_mode)

    uploaded = None
    manual_entries = None
    upload_summary_slot = None

    if schedule_mode == SCHEDULE_MODE_UPLOAD:
        uploaded = st.sidebar.file_uploader(
            "Choose a CSV or Excel file",
            type=["csv", "xlsx", "xlsm"],
            help=(
                "Until you upload a file, the newest schedule saved for this "
                "facility is loaded (see the 📂 panel below). "
                "CSV or Excel (.xlsx / .xlsm). For a workbook, the first sheet "
                "whose header row carries the required columns is used — a "
                "title row above the header, or the plan living on a later "
                "tab, is fine. Required columns (any of these names will "
                "match): LOCATION / Facility, FG SKU ID / FG SKU, FG ACCRY SKU "
                "ID / Accessory SKU, BUILD QTY / Qty, PRODUCTION MONTH / "
                "Month. Extra columns are ignored. Optional: CUSTOMER NAME, "
                "PRODUCTION DAY."
            ),
            label_visibility="collapsed",
        )
        # Blank template — gives the planner a ready-to-fill CSV with exactly
        # the 5 columns the loader reads, plus a couple of example rows.
        # CUSTOMER NAME is optional and intentionally omitted so the template
        # stays minimal; the loader treats it as a `.get` with empty fallback.
        _template_csv = (
            "LOCATION,FG SKU ID,FG ACCRY SKU ID,BUILD QTY,PRODUCTION MONTH,PRODUCTION DAY\n"
            "HENDERSON,BOSS25-010,BOSS25-A016,5,May-26,\n"
            "CYPRESS,SDG25,,2,May-26,\n"
        )
        st.sidebar.download_button(
            "📥 Download blank template",
            _template_csv.encode("utf-8"),
            file_name="schedule_template.csv",
            mime="text/csv",
            width="stretch",
            help=(
                "Empty CSV with the columns the app needs. Fill in your "
                "schedule and re-upload. **PRODUCTION DAY** is optional — "
                "leave it blank to let the tool auto-spread units across "
                "the month's working days, or fill in dates like "
                "`5/8/2026` to pin your own sequence."
            ),
        )
        st.sidebar.caption(
            "📂 The newest schedule saved for this facility loads first; "
            "upload a file to analyse something else. "
            "🔒 Uploaded schedules stay in your browser by default — "
            "use the 📂 panel below to save one to GitHub for the team."
        )
        # A per-plant export can tag a row with the facility the unit is headed
        # to rather than the one building it (the Henderson sheet carries rows
        # marked Spartanburg that Henderson actually builds). Default on so
        # those units are counted; the warning below always names what moved,
        # and unticking restores strict LOCATION filtering for a genuinely
        # multi-plant file.
        st.sidebar.checkbox(
            f"Count rows tagged for another plant as **{location}**",
            value=True, key="absorb_foreign_loc",
            help=(
                "On: a row whose LOCATION names a different facility is still "
                f"built at {location} and counted here — use this when LOCATION "
                "records where the unit is going, not where it's assembled. "
                "Off: only rows tagged for this facility (or untagged) count."
            ),
        )

        # Save / load / delete uploaded schedules (per-facility, shared)
        _render_uploaded_schedules_panel(location, uploaded)

        # Confirmation pill + dropped / rescued-unit warnings. The sidebar runs
        # BEFORE main() loads the schedule, so drawing them here described the
        # previous run's schedule: right after an upload or a 📥 Load the pill
        # still showed the old file's row count, and the warnings that explain
        # a short total were one click behind. main() fills this slot once the
        # schedule for this run is known — see _render_upload_summary.
        upload_summary_slot = st.sidebar.container()
    else:
        # Manual mode — auto-spread runs in main() so dates land on the
        # manual rows automatically; the time-window radio below works
        # for both modes.
        st.sidebar.caption(
            "Enter FG SKU, Accessory SKU (optional), and Quantity for each row. "
            "Already have the list in Excel? Use **📋 Paste a list from Excel** "
            "below — or **📚 Browse catalog** to pick from the known SKUs."
        )

        # Cached loaders — same path as main(), no extra I/O
        machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
        acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))

        # Per-location state keys: a seed DataFrame and a revision counter
        # that we bump every time we programmatically inject a row.
        seed_key = f"manual_seed_{location}"
        rev_key = f"manual_rev_{location}"

        default_entries = pd.DataFrame({
            "FG SKU": [""] * 8,
            "Accessory SKU": [""] * 8,
            "Qty": [0] * 8,
        })
        st.session_state.setdefault(seed_key, default_entries)
        st.session_state.setdefault(rev_key, 0)

        # Bulk paste first (it may bump rev_key + rerun on commit), then the
        # one-at-a-time browse panel for topping the list up.
        _render_paste_import(machine_df, acc_df, location, seed_key, rev_key)

        # Render the Browse panel (it may bump rev_key + rerun on Add)
        _render_sku_browser(machine_df, acc_df, location, seed_key, rev_key)
        _render_scenarios_panel(location, seed_key, rev_key)

        # Render the data_editor with a rev-suffixed key so a fresh seed
        # is picked up after each programmatic add.
        shown_entries = _manual_add_row_numbers(st.session_state[seed_key])
        manual_entries = st.sidebar.data_editor(
            shown_entries,
            width="stretch",
            num_rows="dynamic",
            key=f"manual_entries_{location}_v{st.session_state[rev_key]}",
            column_config={
                _MANUAL_ROW_NO: st.column_config.TextColumn(
                    _MANUAL_ROW_NO,
                    width=44,
                    disabled=True,
                    help=(
                        "Line number — assigned automatically to every row "
                        "that has a SKU in it, whether you paste it or type "
                        "it. Blank rows aren't numbered, and deleting a row "
                        "renumbers the rest."
                    ),
                ),
                "FG SKU": st.column_config.TextColumn("FG SKU", help="e.g. BOSS25-006"),
                "Accessory SKU": st.column_config.TextColumn(
                    "Accessory SKU", help="e.g. BOSS25-A016 (optional)"
                ),
                "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            },
        )

        # Persist the latest in-flight edits so the next Add appends on top.
        # The `#` column is display-only — dropped here so it never reaches
        # the schedule builder, a saved scenario, or the next paste append.
        #
        # reset_index is load-bearing, not tidiness. st.data_editor materializes
        # a row the user added in the browser with `.loc` enlargement, which
        # turns the frame's RangeIndex into a plain Int64 index. Handed that
        # back on the next render, Streamlit stops hiding the index column and
        # marks it required — and the front end then silently drops every
        # further added row from the widget payload, because a row created in
        # the grid has no value for that required cell. The result was that the
        # FIRST grid paste worked (it's the one that poisons the index) and
        # every later one never reached the server at all: no renumbering, and
        # the rows themselves lost on the next rerun.
        manual_entries = _manual_drop_row_numbers(manual_entries)
        if isinstance(manual_entries, pd.DataFrame):
            manual_entries = manual_entries.reset_index(drop=True)
        st.session_state[seed_key] = manual_entries

        # Rows pasted, added, deleted, or typed straight into the grid live in
        # the editor's own widget state, which Streamlit replays over the frame
        # we pass in — the `#` column we numbered above describes the frame as
        # it was BEFORE those edits, so a new row would render unnumbered no
        # matter what we do here. Once the edits have been folded into the seed
        # above, retire that widget state by bumping the revision: the next
        # render builds a fresh editor from the seed alone and every row that
        # has a SKU in it gets its number.
        #
        # Keyed on which rows have content rather than on row count, so filling
        # a blank row in place renumbers too — and so editing a SKU that's
        # already there costs no extra rerun.
        if (_manual_row_has_content(shown_entries).tolist()
                != _manual_row_has_content(manual_entries).tolist()):
            st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
            _defer_rerun()

    # Plan month — the authoritative month for the whole tool. It drives:
    #   • where auto-spread drops undated rows (target month),
    #   • how the 📆 Calendar divides the month into Monday–Friday weeks,
    #   • the month label shown on the Overview + Calendar.
    # Rows whose PRODUCTION DAY falls in a different month still keep their
    # dates, but the page warns the planner they fall outside this month.
    _now = now_local()
    # Seed the picker's state BEFORE the selectbox is built — the one moment a
    # keyed widget's value may be set from code. A main-body button ("📆 Plan
    # August 2026 instead") that wants to switch the month leaves its request
    # in PLAN_MONTH_SEED_KEY and reruns; otherwise the current month is seeded
    # the first time, or whenever the widget's state is gone (the same way the
    # working-day count is seeded). The widget therefore takes no `index=`:
    # its state is always present, and always one of its options.
    _seed = st.session_state.pop(PLAN_MONTH_SEED_KEY, None)
    _cur = _seed if _seed is not None else st.session_state.get(PLAN_MONTH_KEY)
    try:
        _cur = (int(_cur[0]), int(_cur[1]))
        if not 1 <= _cur[1] <= 12:
            raise ValueError(_cur)
    except (TypeError, ValueError, IndexError):
        _cur = (_now.year, _now.month)
    if st.session_state.get(PLAN_MONTH_KEY) != _cur:
        st.session_state[PLAN_MONTH_KEY] = _cur
    # The rolling 2-back / 12-forward window is the default list; the loaded
    # schedule's own months and the current selection are always on it too,
    # so a schedule older than the window can still be planned as its month.
    _month_opts = _plan_month_options(
        _now,
        extra=[_cur, *(st.session_state.get(SCHEDULE_MONTHS_KEY) or [])],
    )
    st.sidebar.markdown("#### Step 3 · Plan month & window")
    plan_month = st.sidebar.selectbox(
        "Plan month",
        _month_opts,
        format_func=_month_label,
        key=PLAN_MONTH_KEY,
        help=(
            "The month this plan is for. Sets how the Calendar splits weeks "
            "(Monday–Friday) and where undated rows get auto-scheduled. "
            "Change this to plan a different month — e.g. pick *June 2026*."
        ),
    )
    plan_month_label = _month_label(plan_month)
    _plan_working_days = _working_days_in_month(plan_month[0], plan_month[1])
    st.sidebar.caption(
        f"📅 **{plan_month_label}** — {len(_plan_working_days)} working days "
        "(Mon–Fri)."
    )

    # Time window — applies to both upload and manual modes. Once the
    # schedule has PRODUCTION DAY values (either from the CSV or via
    # auto-spread on manual rows), this filter slices the analysis to
    # just the current week / the remaining days.
    time_window = st.sidebar.radio(
        "Time window",
        ["Whole month", "This week", "Remaining (from today)"],
        index=0,
        horizontal=True,
        help=(
            "Slice the analysis to a sub-week of the month so the "
            "recommendations stay relevant as the month progresses. "
            "'Working days' below auto-adjusts to match the window (5 for a "
            "single week), so capacity and demand cover the same period."
        ),
    )

    st.sidebar.markdown("---")

    # ----------------------------------------------------------------
    # STEP 4 — Capacity settings: working time + station headcount, each
    # behind an expander
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 4 · Capacity settings")
    with st.sidebar.expander("⏱️ Working-time settings", expanded=False):
        st.caption("How much capacity you have per person per day.")
        # Saved per-facility planning assumptions. Seeds the Safety / Efficiency
        # sliders (below) and the Workforce / Overtime sliders (further down) so
        # a facility reuses what was last saved. Read once here, before any of
        # those widgets are created.
        _fs_saved = get_facility_settings(location)
        # 'Working days' auto-follows BOTH the Plan month AND the Time window so
        # capacity is computed over the SAME period the demand is sliced to.
        # Otherwise "This week" demand (≈5 days) would be judged against a
        # full-month capacity and every utilization / Required-HC / verdict
        # would look ~4× too rosy.
        #   Whole month            → the plan month's working-day count
        #   This week              → 5 (one standard work week)
        #   Remaining (from today) → remaining M–F in the plan month from today
        # A "This week" / "Remaining" window only actually slices demand when
        # today falls inside the plan month (see main()); when it doesn't, the
        # demand stays full-month, so we keep the full-month day count. A manual
        # edit sticks until the month or window changes.
        _pm_working_days = len(_plan_working_days)
        _now = now_local()
        # Same rule main() uses to decide whether the window slices anything
        # (today inside the Plan month and, once the schedule is known, a
        # month its rows are DATED in — PRODUCTION DAY after auto-spread, not
        # the file's own PRODUCTION MONTH) — so demand and capacity always
        # cover the same period. The dated months come from the previous
        # run's load (main() records them); the first run of a new schedule
        # re-syncs on the next one.
        _today_in_plan_month = _window_applies(
            plan_month, st.session_state.get(SCHEDULE_DATED_MONTHS_KEY) or [],
        )
        if time_window == "This week" and _today_in_plan_month:
            _target_days = 5
        elif time_window == "Remaining (from today)" and _today_in_plan_month:
            _today = _now.date()
            _target_days = max(1, sum(1 for d in _plan_working_days if d >= _today))
        else:
            _target_days = _pm_working_days
        _sync_working_days(_target_days, (plan_month, time_window, _today_in_plan_month))
        days = st.number_input(
            "Working days in the period",
            min_value=1, max_value=31,
            key="work_days_input",
            help=(
                f"Auto-follows the Plan month ({_pm_working_days} for "
                f"{plan_month_label}) and the Time window (5 for a single week, "
                "remaining M–F for 'Remaining'). Override for a custom period — "
                "your value sticks until you change the month or window."
            ),
        )
        shift = st.number_input(
            "Shift length (minutes)",
            min_value=60, max_value=1440,
            value=DEFAULT_SHIFT_MINUTES, step=30,
            help="480 = 8 hours.",
        )
        safety = st.slider(
            "Planning buffer (safety factor)",
            min_value=0.50, max_value=1.00,
            value=max(0.50, min(1.00, float(_fs_saved.get("safety", DEFAULT_SAFETY_FACTOR)))),
            step=0.05,
            help="0.85 leaves a 15% buffer. 1.00 = use 100% of capacity (no buffer). "
                 "Saved per facility with the 💾 button below.",
        )
        efficiency = st.slider(
            "Productive time (efficiency factor)",
            min_value=0.40, max_value=1.00,
            value=max(0.40, min(1.00, float(_fs_saved.get("efficiency", DEFAULT_EFFICIENCY_FACTOR)))),
            step=0.025,
            help=(
                "Share of paid shift time that becomes productive work — breaks, "
                "setup, rework, and line-balance loss reduce it (like OEE "
                "availability × performance). 1.00 = no loss; typical assembly "
                "plans run 0.80–0.90. The 0.50 default is deliberately conservative "
                "for this shop. 0.625 ≈ 5 productive hours in an 8-hr shift "
                "(a planning convention, not an external standard)."
            ),
        )

        # ------------------------------------------------------------
        # Workforce availability — new-hire ramp + absenteeism. These
        # reduce LABOR capacity only (never physical throughput / cells),
        # so they shift Required-HC, labor utilization and buildable
        # units, but leave the Space-limits view untouched. Seeded from
        # this facility's saved planning assumptions and re-savable.
        # ------------------------------------------------------------
        st.markdown("**🧑‍🏭 Workforce availability**")
        # _fs_saved was read at the top of this expander.
        _nh_key = f"new_hire_pct_pct_{location}"
        _np_key = f"new_hire_prod_pct_{location}"
        _ab_key = f"absenteeism_pct_{location}"
        _ot_key = f"overtime_pct_{location}"
        # A scenario "Re-apply" (Executive Summary tab) stashes its assumptions
        # under a NON-widget key. Apply them HERE — before the sliders below are
        # instantiated — because Streamlit forbids writing a widget's
        # session_state key once that widget has been created in the same run.
        _pending = st.session_state.pop(f"_exec_reapply_{location}", None)
        if _pending is not None:
            st.session_state[_nh_key] = _pct100(_pending.get("new_hire_pct", 0))
            st.session_state[_np_key] = _pct100(_pending.get("new_hire_productivity", 0.5))
            st.session_state[_ab_key] = _pct100(_pending.get("absenteeism", 0.05))
            st.session_state[_ot_key] = _pct100(_pending.get("overtime", 0.0))
        for _k, _frac in ((_nh_key, _fs_saved["new_hire_pct"]),
                          (_np_key, _fs_saved["new_hire_productivity"]),
                          (_ab_key, _fs_saved["absenteeism"]),
                          (_ot_key, _fs_saved["overtime"])):
            if _k not in st.session_state:
                st.session_state[_k] = int(round(_frac * 100))

        new_hire_pct = st.slider(
            "New hire %", 0, 100, step=1, key=_nh_key,
            help="Share of the crew who are new hires (still ramping up).",
        ) / 100.0
        new_hire_productivity = st.slider(
            "New-hire productivity %", 0, 100, step=1, key=_np_key,
            help="A new hire's pace vs a seasoned worker (50% = half speed).",
        ) / 100.0
        absenteeism = st.slider(
            "Absenteeism %", 0, 30, step=1, key=_ab_key,
            help="Share of paid time not worked (absences) — reduces available labor.",
        ) / 100.0

        _adj_eff = efficiency * (1.0 - new_hire_pct * (1.0 - new_hire_productivity))
        st.caption(
            f"Adjusted efficiency **{_adj_eff*100:.1f}%** "
            f"(base {efficiency*100:.0f}% × new-hire drag), "
            f"absenteeism −{absenteeism*100:.0f}%. "
            "Affects labor / Required-HC & buildable — **not** Space-limits."
        )

        st.markdown("**⏱️ Overtime**")
        overtime = st.slider(
            "Overtime %", 0, 100, step=1, key=_ot_key,
            help="Extra working time vs the standard shift (e.g. +20% ≈ a 6th "
                 "day; +100% = double). Extends the effective shift, so it "
                 "raises BOTH labor capacity AND station throughput.",
        ) / 100.0
        if overtime > 0:
            st.caption(
                f"+{overtime*100:.0f}% working time → lifts labor **and** "
                "throughput / Space-limits (cells run longer)."
            )

        # ------------------------------------------------------------
        # Exclude stations from buildable-unit sizing. A station the model
        # over-constrains (e.g. Battery) can be set aside so it no longer
        # caps "Buildable units" or shows up as the binding bottleneck —
        # while STILL appearing (marked ⏸️) in the Capacity table so the
        # planner remembers to come back and fix the underlying model.
        # Persisted per facility alongside the other assumptions.
        # ------------------------------------------------------------
        from core.constants import STATION_KEY_TO_DISPLAY as _SKTD
        st.markdown("**🚫 Exclude stations from buildable units**")
        _station_options = [_SKTD[k] for k in STATION_KEYS]
        _excl_key = f"excluded_stations_{location}"
        if _excl_key not in st.session_state:
            _saved_excl = _fs_saved.get("excluded_stations", []) or []
            st.session_state[_excl_key] = [s for s in _saved_excl if s in _station_options]
        excluded_stations = st.multiselect(
            "Stations to ignore when sizing buildable units",
            options=_station_options,
            key=_excl_key,
            help=(
                "Skipped when computing **Buildable units** and the binding "
                "bottleneck — use when the model over-constrains a station that "
                "isn't really binding in reality (e.g. Battery). Excluded "
                "stations still appear in the Capacity table, marked ⏸️, so the "
                "issue stays visible until you fix it. Saved per facility with "
                "the 💾 button below."
            ),
        )
        if excluded_stations:
            st.caption(
                f"⏸️ Excluding **{', '.join(excluded_stations)}** from "
                "buildable-unit sizing (still shown in the Capacity table)."
            )

        if st.button(f"💾 Save planning assumptions for {location}",
                     width="stretch", key=f"save_assump_{location}"):
            token = _github_token()
            if not token:
                st.error(
                    "GitHub token not configured. Ask your admin to add "
                    "`github_token` to Streamlit Secrets."
                )
            else:
                try:
                    with st.spinner(f"Saving {location} assumptions to GitHub..."):
                        save_facility_settings_to_github(location, {
                            "new_hire_pct": new_hire_pct,
                            "new_hire_productivity": new_hire_productivity,
                            "absenteeism": absenteeism,
                            "overtime": overtime,
                            "efficiency": efficiency,
                            "safety": safety,
                            "excluded_stations": excluded_stations,
                        }, token)
                    _track_and_flush("assumptions_save", facility=location)
                    st.success(f"✅ Saved planning assumptions for {location}.")
                    _post_save_redeploy_notice(f"assump_{location}")
                except Exception as e:
                    _show_github_error(e, "Save")
        st.caption(
            "Saves **Workforce**, **Overtime**, **Safety buffer** & "
            "**Efficiency** for this facility. (Working days follows the Plan "
            "month / Time window; shift length isn't saved.)"
        )

    # ----------------------------------------------------------------
    # STEP 4 (cont.) — Station headcount (per facility), behind an expander
    # ----------------------------------------------------------------
    # "👥 Edit headcount" on the main panel leaves a seed; consumed here,
    # right before the (stateful) expander is built, so it opens on this run
    # and stays open until the planner closes it. A stateful expander costs
    # one rerun per manual toggle of this one expander.
    _crew_exp_key = f"crew_expander_{location}"
    if st.session_state.pop(CREW_EXPANDER_SEED_KEY, False):
        st.session_state[_crew_exp_key] = True
    with st.sidebar.expander(
        f"👥 Station headcount for {location}", expanded=False,
        key=_crew_exp_key, on_change="rerun",
    ):
        st.caption(
            "Edit how many people work at each station. Set **People = 0** "
            "for any station that doesn't exist at this facility. "
            "**Lead** and **Other** are support roles — only **People** counts; "
            "they add to total headcount & labor hours but aren't assigned to a "
            "production station (no demand / throughput)."
        )

        crew_default_df = load_facility_crew_df(location)
        # Friendly display: rename columns for the editor
        display_saved = crew_default_df.rename(
            columns={"HC": "People", "Conc": "Stations/Cells", "Crew": "Crew per unit"}
        )
        # Unsaved edits live in the editor's widget state, which Streamlit
        # drops the moment this facility's editor isn't drawn (a facility
        # switch and back). Keep a per-facility draft of the edited table and
        # seed the editor from it, so the planner's numbers come back with
        # them; 💾 Save crew is still what persists them for everyone.
        _crew_draft_key = f"_crew_draft_{location}"
        _draft = st.session_state.get(_crew_draft_key)
        display_crew = display_saved
        if (
            isinstance(_draft, pd.DataFrame)
            and list(_draft.index) == list(display_saved.index)
            and list(_draft.columns) == list(display_saved.columns)
        ):
            display_crew = _draft

        edited_display = st.data_editor(
            display_crew,
            width="stretch",
            num_rows="fixed",
            key=f"crew_editor_{location}",
            column_config={
                "People": st.column_config.NumberColumn(
                    "People", min_value=0, step=1,
                    help="Total headcount available at this station.",
                ),
                "Stations/Cells": st.column_config.NumberColumn(
                    "Stations/Cells", min_value=0, step=1,
                    help="How many units can be worked on at the same time (parallel bays/cells).",
                ),
                "Crew per unit": st.column_config.NumberColumn(
                    "Crew per unit", min_value=1, step=1,
                    help="People assigned to one unit at this station.",
                ),
            },
        )
        # Remember the edited table while it differs from what's saved; once
        # they match again (a save, or edits undone) the draft goes.
        try:
            if edited_display.equals(display_saved):
                st.session_state.pop(_crew_draft_key, None)
            else:
                st.session_state[_crew_draft_key] = edited_display.copy()
        except Exception:
            pass
        # Convert back to internal column names
        edited = edited_display.rename(
            columns={"People": "HC", "Stations/Cells": "Conc", "Crew per unit": "Crew"}
        )
        # Sanitize: clearing a cell in the editor (delete key) leaves NaN, which
        # would later hit int(NaN) in build_capacity_table / the Build Time
        # drill-down and red-screen every tab. Coerce each column back to a
        # clean int with a sensible fill (Crew floors at 1 — a 0-crew station is
        # an invalid divide). This persists across reruns since the editor frame
        # is re-derived each run.
        for _col, _fill in (("HC", 0), ("Conc", 0), ("Crew", 1)):
            edited[_col] = (
                pd.to_numeric(edited[_col], errors="coerce")
                .fillna(_fill).astype(int)
            )

        # Save button
        if st.button(f"💾 Save crew for {location}", width="stretch"):
            token = _github_token()
            if not token:
                st.error(
                    "GitHub token not configured. Ask your admin to add "
                    "`github_token` to Streamlit Secrets."
                )
            else:
                try:
                    with st.spinner(f"Saving {location} crew to GitHub..."):
                        save_facility_crew_to_github(location, edited, token)
                    _track_and_flush("crew_save", facility=location)
                    st.success(f"✅ Saved crew for {location}.")
                    _post_save_redeploy_notice(f"crew_{location}")
                except Exception as e:
                    _show_github_error(e, "Save")

        # Live totals — split production vs support (Lead/Other)
        prod_hc, supp_hc, total_hc = _split_crew_hc(edited)
        _prod_rows = edited.loc[~edited.index.isin(SUPPORT_ROLES)]
        active_stations = int((_prod_rows["HC"] > 0).sum())
        _supp_note = f" ({prod_hc} production + {supp_hc} support)" if supp_hc else ""
        st.markdown(
            f"**👥 Total headcount:** {total_hc}{_supp_note}  \n"
            f"**🏭 Active stations:** {active_stations} of {len(_prod_rows)}"
        )

    # ----------------------------------------------------------------
    # Help / Glossary — always last
    # ----------------------------------------------------------------
    with st.sidebar.expander("ℹ️ Help & glossary", expanded=False):
        _render_help_glossary()

    # ----------------------------------------------------------------
    # Hidden admin analytics — discoverable but visually unobtrusive.
    # Sits at the very bottom of the sidebar; regular users won't notice.
    # ----------------------------------------------------------------
    with st.sidebar.expander("⚙️ Admin", expanded=False):
        _render_admin_panel()

    # A sidebar button that asked for a rerun gets it now: every sidebar
    # widget above has been built in this run, so none of them loses its
    # state (see _defer_rerun).
    _flush_deferred_rerun()

    return {
        "uploaded": uploaded,
        "upload_summary_slot": upload_summary_slot,
        "manual_entries": manual_entries,
        "schedule_mode": schedule_mode,
        "location": location,
        "safety": safety,
        "efficiency": efficiency,
        "new_hire_pct": new_hire_pct,
        "new_hire_productivity": new_hire_productivity,
        "absenteeism": absenteeism,
        "overtime": overtime,
        "days": days,
        "shift": shift,
        "crew_config": edited,
        "time_window": time_window,
        "plan_month": plan_month,
        "plan_month_label": plan_month_label,
        "excluded_stations": excluded_stations,
    }


# =============================================================
# Tab renderers
# =============================================================
def _compute_weekly_utilization(schedule_df_full, machine_df, acc_df, inputs):
    """Return a station × week matrix of labor utilization %.

    Used by the Overview tab's weekly heatmap so a planner can spot which
    week of the month is the actual pressure point — even when the
    currently-selected Time window is "Whole month" and would otherwise
    average the pressure out across all 4 weeks.

    Returns None when the schedule has no PRODUCTION DAY data or no rows
    that successfully expand into units.
    """
    if schedule_df_full is None or schedule_df_full.empty:
        return None
    if "PRODUCTION DAY" not in schedule_df_full.columns:
        return None
    # One column per (year, month, week) bucket of PRODUCTION DAY — never per
    # bare WEEK_OF_MONTH, which merged a September carryover row's week 2 into
    # October's. Columns read "Wk 3" while every dated row is in one month and
    # "Sep Wk 5" / "Oct Wk 1" once they span two.
    buckets = _month_week_keys(schedule_df_full)
    if not buckets:
        return None
    with_month = len({(y, m) for y, m, _w in buckets}) > 1

    # Capacity for each week uses that week's TRUE count of M–F working days,
    # not a flat 5. A partial leading/trailing calendar week (week 1 or the last
    # week of most months) has only 1–4 working days; dividing its demand by a
    # 5-day capacity understates utilization by up to ~5× and paints a genuinely
    # overloaded short week green. The count comes from the bucket's own month,
    # so a multi-month schedule never applies one month's calendar to another
    # month's weeks. A week with no working days falls back to 5. We
    # deliberately don't pull the day count from inputs["days"] (that may be set
    # for the whole-month window).
    def _working_days_for_week(y, m, w):
        try:
            cnt = sum(
                1 for _d in _working_days_in_month(y, m) if week_of_month(_d) == w
            )
            return cnt if cnt > 0 else 5
        except Exception:
            return 5

    out = {}
    for (y, m, w) in buckets:
        slice_df = schedule_df_full[_month_week_mask(schedule_df_full, y, m, w)]
        if slice_df.empty:
            continue
        try:
            units_w = _expand_schedule(slice_df, machine_df, acc_df)
        except Exception:
            continue
        if units_w.empty:
            continue
        week_days = _working_days_for_week(y, m, w)
        try:
            cap_w = _build_capacity_table_cached(
                units_w, inputs["crew_config"], inputs["shift"], week_days,
                inputs["safety"], inputs["efficiency"],
                inputs.get("new_hire_pct", 0.0),
                inputs.get("new_hire_productivity", 1.0),
                inputs.get("absenteeism", 0.0),
                inputs.get("overtime", 0.0),
            )
        except Exception:
            continue
        out[_month_week_label(y, m, w, with_month)] = cap_w["labor_util_safe"]

    if not out:
        return None
    return pd.DataFrame(out).fillna(0)


@st.cache_data(show_spinner=False, max_entries=16)
def _compute_weekly_utilization_cached(schedule_df_full, machine_df, acc_df,
                                       crew_config, shift_minutes, safety_factor,
                                       efficiency_factor, new_hire_pct,
                                       new_hire_productivity, absenteeism,
                                       overtime):
    """Content-keyed cache around _compute_weekly_utilization.

    The helper expands the schedule once per week and prices each slice, so
    even with every inner call a cache hit it hashed the three frames five
    times over (~0.3 s per rerun, for an Overview chart whose inputs rarely
    change). Hashing them once here turns a rerun into a single lookup. The
    scalars are passed positionally (same shape as _build_capacity_table_cached)
    so every distinct sidebar setting is its own key."""
    return _compute_weekly_utilization(schedule_df_full, machine_df, acc_df, {
        "crew_config": crew_config,
        "shift": shift_minutes,
        "safety": safety_factor,
        "efficiency": efficiency_factor,
        "new_hire_pct": new_hire_pct,
        "new_hire_productivity": new_hire_productivity,
        "absenteeism": absenteeism,
        "overtime": overtime,
    })


def _weekly_utilization_for(schedule_df_full, machine_df, acc_df, inputs):
    """Unpack the sidebar dict into hashable scalars and go through the cache.
    (``inputs`` itself carries the upload handle and the manual-entry frame,
    which must not be part of the key.)"""
    return _compute_weekly_utilization_cached(
        schedule_df_full, machine_df, acc_df,
        inputs["crew_config"], inputs["shift"], inputs["safety"], inputs["efficiency"],
        inputs.get("new_hire_pct", 0.0),
        inputs.get("new_hire_productivity", 1.0),
        inputs.get("absenteeism", 0.0),
        inputs.get("overtime", 0.0),
    )


def _max_units_at_current_mix(capacity: pd.DataFrame, total_units: int,
                              excluded_stations=None) -> dict:
    """How many units of the *current* SKU mix the facility can build this
    month, given the available stations + sidebar settings.

    Scales the schedule proportionally until any station hits its safe
    capacity (HC × shift × days × efficiency × safety) OR its throughput cap
    (bays × cycle). Returns the floor of that scaling, plus the bottleneck
    station that dictated the limit. If any station the mix needs has HC = 0,
    the build is infeasible (returns max_units = 0 and names the station).

    ``excluded_stations`` is an optional iterable of station DISPLAY names to
    ignore when sizing the cap — neither counted as a bottleneck nor as a
    missing (HC=0) blocker — so a station the model over-constrains (e.g.
    Battery) can be set aside until it's fixed.

    Reuses ``build_capacity_table`` columns: HC, labor_demand, labor_util_safe,
    thru_util_safe, station_display.
    """
    excluded = {str(s).strip() for s in (excluded_stations or [])}
    if capacity is None or capacity.empty or total_units <= 0:
        # Return the full key set (matching the other branches) so any caller
        # that subscripts labor_/thru_/potential_ keys won't KeyError on the
        # empty/zero path. All callers currently use .get(), but keep the
        # contract uniform for safety.
        _z = int(max(total_units, 0))
        return {"max_units": _z, "bottleneck": "",
                "max_util_safe": 0.0, "infeasible": False, "missing_stations": [],
                "labor_max_units": _z, "labor_bottleneck": "", "labor_max_util_safe": 0.0,
                "thru_max_units": _z, "thru_bottleneck": "", "thru_max_util_safe": 0.0,
                "potential_max_units": _z, "potential_bottleneck": "",
                "potential_util_safe": 0.0}

    missing: list[str] = []
    max_util = 0.0
    bottleneck = ""
    # Track labor and throughput separately so we can tell the planner WHICH
    # constraint is biting (person-mins vs bays/cycle) at each binding station.
    labor_max_util = 0.0
    labor_bottleneck = ""
    thru_max_util = 0.0
    thru_bottleneck = ""
    for st_disp, row in capacity.iterrows():
        if str(st_disp) in excluded:
            continue  # planner excluded this station from buildable sizing
        labor_demand = float(row.get("labor_demand", 0) or 0)
        # "need_per_day" is only > 0 when units route through this station,
        # so it doubles as a "station is in the mix" signal alongside demand.
        need_per_day = float(row.get("need_per_day", 0) or 0)
        if labor_demand <= 0 and need_per_day <= 0:
            continue
        hc = int(row.get("HC", 0) or 0)
        if hc <= 0:
            missing.append(str(st_disp))
            continue
        util_labor = float(row.get("labor_util_safe", 0) or 0)
        util_thru = float(row.get("thru_util_safe", 0) or 0)
        if util_labor > labor_max_util:
            labor_max_util = util_labor
            labor_bottleneck = str(st_disp)
        if util_thru > thru_max_util:
            thru_max_util = util_thru
            thru_bottleneck = str(st_disp)
        util = max(util_labor, util_thru)
        if util > max_util:
            max_util = util
            bottleneck = str(st_disp)

    def _cap(util: float) -> int:
        return int(total_units // util) if util > 0 else int(total_units)

    # "Potential" = what the staffed stations alone could absorb, ignoring
    # the HC=0 gaps. Useful even in the infeasible case so planners see the
    # upper bound they'd unlock by staffing the missing stations.
    potential_max = _cap(max_util)
    labor_cap_units = _cap(labor_max_util)
    thru_cap_units = _cap(thru_max_util)

    extras = {
        "labor_max_units": labor_cap_units,
        "labor_bottleneck": labor_bottleneck,
        "labor_max_util_safe": labor_max_util,
        "thru_max_units": thru_cap_units,
        "thru_bottleneck": thru_bottleneck,
        "thru_max_util_safe": thru_max_util,
    }

    if missing:
        return {"max_units": 0, "bottleneck": missing[0],
                "max_util_safe": float("inf"), "infeasible": True,
                "missing_stations": missing,
                "potential_max_units": potential_max,
                "potential_bottleneck": bottleneck,
                "potential_util_safe": max_util,
                **extras}

    if max_util <= 0:
        # No binding station — capacity is effectively unbounded for this mix.
        return {"max_units": int(total_units), "bottleneck": "",
                "max_util_safe": 0.0, "infeasible": False, "missing_stations": [],
                "potential_max_units": int(total_units),
                "potential_bottleneck": "", "potential_util_safe": 0.0,
                **extras}

    return {
        "max_units": potential_max,
        "bottleneck": bottleneck,
        "max_util_safe": max_util,
        "infeasible": False,
        "missing_stations": [],
        "potential_max_units": potential_max,
        "potential_bottleneck": bottleneck,
        "potential_util_safe": max_util,
        **extras,
    }


def _area_space_cap(sub: pd.DataFrame, total_units: int,
                    excluded_stations=None) -> dict | None:
    """Throughput (space) cap for a group of stations — independent of HC.

    Unlike ``_max_units_at_current_mix`` (which is labor-aware and skips
    HC=0 stations), this looks ONLY at physical throughput: a station's
    cells exist whether or not it is staffed, so an unstaffed-but-celled
    station must still count toward the area's space bottleneck. Uses the
    same inclusion rule as the per-station Space-limits table
    (need_per_day > 0 AND thru_cap_safe > 0 AND thru_util_safe > 0), so the
    two views stay consistent.

    The binding cell is the station with the highest throughput utilization;
    the area cap scales the current mix until that cell saturates. Returns
    None when no station in the group is space-constrained.
    """
    if sub is None or sub.empty or total_units <= 0:
        return None
    excluded = {str(s).strip() for s in (excluded_stations or [])}
    best_util = 0.0
    limiting = ""
    for st_disp, row in sub.iterrows():
        if str(st_disp) in excluded:
            continue  # planner excluded this station from buildable sizing
        need = float(row.get("need_per_day", 0) or 0)
        cap = float(row.get("thru_cap_safe", 0) or 0)
        util = float(row.get("thru_util_safe", 0) or 0)
        if need <= 0 or cap <= 0 or util <= 0:
            continue
        if util > best_util:
            best_util = util
            limiting = str(st_disp)
    if best_util <= 0:
        return None
    return {
        "max_units": int(total_units // best_util),
        "limiting_cell": limiting or "—",
        "cells_used_pct": round(best_util * 100),
    }


def _render_buildable_by_line(bl: dict, total: int) -> None:
    """Render the per-line buildable breakdown (shared by Overview + Capacity).

    ``bl`` is the dict returned by ``labor_calculator.buildable_by_line``. No-op
    when the plan spans a single line (the headline number already says it all).
    """
    rows = (bl or {}).get("lines", [])
    if len(rows) <= 1:
        return

    def _bound(r):
        if r.get("infeasible"):
            return "⚠️ unstaffed"
        mu = r.get("max_util")
        if not mu:
            return "spare capacity"
        return f"{r.get('binding_kind', 'labor')} · {mu * 100:.0f}%"

    df = pd.DataFrame([{
        "Line": r["line"],
        "Units planned": r["planned"],
        "Buildable": r["buildable"],
        "Binding station": r["binding_station"],
        "Bound by": _bound(r),
    } for r in rows])
    with st.expander("🏭 Buildable by production line", expanded=False):
        st.caption(
            "The plant runs parallel lines (Compressor=PDS · Generator=SDG · "
            "EBOSS=full BOSS · PM=head/skid). Each scales to its OWN bottleneck "
            "and the outputs add up, so one line isn't throttled by another's "
            "constraint. A station shared by lines caps each line that uses it "
            "(by its combined utilization), so the per-line caps never "
            "over-subscribe a station."
        )
        st.dataframe(
            df, width="stretch", hide_index=True,
            column_config={
                "Line": st.column_config.TextColumn(
                    "Line", help="Compressor=PDS · Generator=SDG · EBOSS=full BOSS unit · PM=head/skid-only."),
                "Units planned": st.column_config.NumberColumn(
                    "Units planned", format="%d", help="Units of this line in the current plan."),
                "Buildable": st.column_config.NumberColumn(
                    "Buildable", format="%d",
                    help="Units of this line the plant can build at the current mix, scaled to this line's own bottleneck."),
                "Binding station": st.column_config.TextColumn(
                    "Binding station", help="The station that limits this line."),
                "Bound by": st.column_config.TextColumn(
                    "Bound by", help="Whether the binding station is labor- or throughput-bound, and its utilization (or 'unstaffed' / 'spare capacity')."),
            },
        )
        st.caption(f"Total buildable = **{total:,}** units (sum of the lines).")


# =============================================================
# Executive Summary tab
# =============================================================
def _pct100(frac) -> int:
    """Fraction in [0,1] → integer percent for seeding a 0–100 slider."""
    try:
        v = float(frac)
    except (TypeError, ValueError):
        v = 0.0
    if v != v:  # NaN
        v = 0.0
    v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    return int(round(v * 100))


def _exec_summary_sections(m: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Group an executive_summary() metrics dict into ordered, labeled sections
    so the table reads as a logical input→output narrative: what we're building →
    the levers → the resources → the result. Every metric is included; nothing
    is dropped. Returns [(section_title, [(label, value), ...]), ...]."""
    def _pct(x):
        return "—" if x is None else f"{x * 100:.0f}%"

    def _num(x, d=1):
        return "—" if x is None else f"{x:,.{d}f}"

    safety = m.get("safety", 1.0)
    hc_station = m.get("headcount_needed_station")
    missed_station = m.get("missed_units_station")
    return [
        ("📦 Plan & work content", [
            ("Units", f"{int(m.get('total_units', 0)):,}"),
            ("Units/day", _num(m.get("units_per_day", 0), 1)),
            ("Total Hours Earned", _num(m.get("total_earned_hours", 0), 0)),
            ("Man-hours / unit (standard)", _num(m.get("mh_per_unit", 0), 2)),
            ("Man-hours / unit (effective)", _num(m.get("mh_per_unit_loaded", 0), 2)),
            ("Avail hrs / unit (all staff)",
             _num(m.get("total_available_hours_per_unit", 0), 2)),
        ]),
        ("⚙️ Assumptions (your levers)", [
            ("Base Efficiency", _pct(m.get("base_efficiency", 0))),
            ("New Hire %", _pct(m.get("new_hire_pct", 0))),
            ("New-Hire Productivity", _pct(m.get("new_hire_productivity", 0))),
            ("Adjusted Efficiency", _pct(m.get("adjusted_efficiency", 0))),
            ("Absenteeism", _pct(m.get("absenteeism", 0))),
            ("Overtime", _pct(m.get("overtime", 0))),
            ("Safety buffer", _pct(1.0 - float(safety or 0))),
        ]),
        ("👥 Workforce & hours (what you have)", [
            ("Available Headcount (production)", _num(m.get("available_headcount", 0), 0)),
            ("Support HC (Lead/Other)", _num(m.get("support_headcount", 0), 0)),
            ("Total Headcount", _num(m.get("total_headcount", 0), 0)),
            ("Net Available Hours (production)", _num(m.get("net_available_hours", 0), 0)),
            ("Total Available Hours (incl. support)",
             _num(m.get("total_available_hours", 0), 0)),
            ("Available Earned Hours", _num(m.get("available_earned_hours", 0), 1)),
        ]),
        ("🎯 Result (can we do it?)", [
            ("Headcount Needed (aggregate)", _num(m.get("headcount_needed_aggregate", 0), 2)),
            ("Headcount Needed (station-aware)",
             _num(hc_station, 0) if hc_station is not None else "—"),
            ("Missed Units (aggregate)", _num(m.get("missed_units_aggregate", 0), 1)),
            ("Missed Units (station-aware)",
             (f"{int(missed_station):,}" if missed_station is not None else "—")),
            ("Verdict", m.get("verdict", "—")),
        ]),
    ]


def _exec_summary_rowmap(m: dict) -> dict:
    """Flat {label: value} map across all sections (for comparison columns)."""
    return {label: val
            for _sec, rows in _exec_summary_sections(m)
            for (label, val) in rows}


# Section titles — used to detect & shade the divider rows in the single table.
_EXEC_SECTION_TITLES = frozenset(t for t, _ in _exec_summary_sections({}))


def _exec_scenarios_newest_first(location: str) -> list[str]:
    """The facility's saved executive scenarios, most recent save first.

    ``list_exec_scenarios`` sorts by name, so "Hen Aug 08/04" came before
    "Henderson October 09/23" and the 📥 Re-apply / 🗑 Delete picker opened on
    an old save while the side-by-side below it defaulted to the newest. One
    order for both: ``saved_at`` descending, name as the tie-break."""
    names = list_exec_scenarios(location)
    return sorted(
        names,
        key=lambda n: (
            str((get_exec_scenario(location, n) or {}).get("saved_at", "")), str(n),
        ),
        reverse=True,
    )


def _exec_rows_with_headers(m: dict) -> list[tuple[str, str]]:
    """Flatten the sections into one ordered (label, value) list, inserting each
    section title as a divider row (blank value) so the whole thing renders as a
    single table with shaded group separators."""
    out: list[tuple[str, str]] = []
    for title, rows in _exec_summary_sections(m):
        out.append((title, ""))
        out.extend(rows)
    return out


def _style_exec_table(row):
    """Row styler for the single exec-summary table: shade the section-divider
    rows (color band) and color the Verdict row (Good green / No Good red).
    Detects row type by the 'Metric' column value (table uses hide_index)."""
    metric = str(row.get("Metric", ""))
    if metric in _EXEC_SECTION_TITLES:
        return ["background-color:#dde6f0; color:#1f2d3d; font-weight:700"] * len(row)
    if metric == "Verdict":
        styles = []
        for col in row.index:
            v = row[col]
            if col == "Metric":
                styles.append("font-weight:700")
            elif str(v) == "Good":
                styles.append("color:white; background-color:#1a9850; font-weight:700")
            elif str(v) == "No Good":
                styles.append("color:white; background-color:#d73027; font-weight:700")
            else:
                styles.append("")
        return styles
    return ["" for _ in row]


@st.fragment
def tab_exec_summary(units, capacity, inputs, total_units: int = 0):
    # @st.fragment: the side-by-side pickers (one selectbox per facility) and
    # the saved-scenario picker only change what THIS tab shows, so a change
    # reruns the tab, not main(). The 📥 Re-apply button writes the sidebar's
    # reapply key and calls st.rerun() — which from inside a fragment still
    # reruns the whole app (scope defaults to "app"), exactly what it needs.
    """Monthly executive labor summary (mirrors the legacy planning sheet),
    with per-facility save/reload and a side-by-side multi-facility table."""
    location = inputs.get("location", "Henderson")
    month_label = inputs.get("plan_month_label", "")
    _reapplied = st.session_state.pop(f"_exec_reapply_toast_{location}", None)
    if _reapplied:
        st.success(f"Re-applied workforce assumptions from **{_reapplied}**.")
    st.markdown(f"### 📋 Executive Summary — {location}"
                f"{(' · ' + month_label) if month_label else ''}")
    st.caption(
        "Monthly labor roll-up in the style of the planning sheet. **New-hire ramp** "
        "and **absenteeism** reduce available labor while **overtime** raises it "
        "(all set in the sidebar under *Working-time settings*); safety is shown as "
        "a buffer. **Lead/Other** support staff add to total headcount & hours but "
        "not to production capacity. Headcount Needed and Missed Units are shown "
        "both as a simple **aggregate** (treats labor as fully shareable) and "
        "**station-aware** (respects per-station bottlenecks)."
    )

    has_live = units is not None and not units.empty
    metrics = None
    avail_hc = 0.0
    support_hc = 0.0
    if has_live:
        total_earned_minutes = float(units["total_labor"].sum())
        # Production HC drives the verdict; support (Lead/Other) counts only in
        # the total headcount / total available-hours rows.
        _prod, _supp, _tot = _split_crew_hc(inputs["crew_config"])
        avail_hc = float(_prod)
        support_hc = float(_supp)
        station_required_hc = None
        station_buildable = None
        if capacity is not None and not capacity.empty:
            try:
                station_required_hc = float(capacity["required_hc"].sum())
            except Exception:
                station_required_hc = None
            try:
                station_buildable = int(buildable_by_line(
                    units, capacity,
                    excluded_stations=inputs.get("excluded_stations"),
                ).get("total", 0))
            except Exception:
                station_buildable = None

        metrics = executive_summary(
            total_earned_minutes=total_earned_minutes,
            total_units=int(total_units or len(units)),
            available_hc=avail_hc,
            shift_minutes=int(inputs.get("shift", 480)),
            working_days=int(inputs.get("days", 20)),
            base_efficiency=float(inputs.get("efficiency", 0.5)),
            new_hire_pct=float(inputs.get("new_hire_pct", 0.0)),
            new_hire_productivity=float(inputs.get("new_hire_productivity", 1.0)),
            absenteeism=float(inputs.get("absenteeism", 0.0)),
            safety=float(inputs.get("safety", 1.0)),
            overtime=float(inputs.get("overtime", 0.0)),
            support_hc=support_hc,
            station_required_hc=station_required_hc,
            station_buildable=station_buildable,
        )

    if has_live and metrics is not None:
        col_live, col_save = st.columns([2, 1])
        with col_live:
            st.markdown(f"**Current settings — {location}**")
            _live_df = pd.DataFrame(
                _exec_rows_with_headers(metrics), columns=["Metric", location])
            try:
                st.dataframe(_live_df.style.apply(_style_exec_table, axis=1),
                             width="stretch", hide_index=True)
            except Exception:
                st.dataframe(_live_df, width="stretch", hide_index=True)
            hc_need = metrics["headcount_needed_aggregate"]
            if metrics["verdict"] == "Good":
                st.success(
                    f"✅ **Good** — {avail_hc:.0f} available ≥ {hc_need:.1f} needed "
                    "(aggregate)."
                )
            else:
                st.error(
                    f"⛔ **No Good** — short ~{hc_need - avail_hc:.1f} people "
                    f"({avail_hc:.0f} available vs {hc_need:.1f} needed); "
                    f"~{metrics['missed_units_aggregate']:.0f} units at risk."
                )
            # Clarify the stricter station-aware figure so a "Good" aggregate
            # verdict doesn't hide a per-station shortfall (rounds up per station
            # and can't share labor across them, so it's always ≥ the aggregate).
            _hc_station = metrics.get("headcount_needed_station")
            _missed_station = metrics.get("missed_units_station")
            if _hc_station is not None:
                # "Covered at the station level" must reflect the ACTUAL
                # per-station result (missed_units_station, from the buildable
                # allocation), not a pooled sum-of-required-HC vs total-HC
                # comparison. The pooled sum lets surplus at loosely-loaded
                # stations mask a shortfall at an unstaffed or overloaded one, so
                # it could previously claim "covered" while the same tab reported
                # 100+ units missed. Treat the plan as station-short if the
                # station-aware HC exceeds availability OR any units can't be
                # built at the binding station(s).
                _station_short = (_hc_station > avail_hc) or bool(_missed_station)
                if _station_short:
                    _msg = (
                        f"⚠️ The verdict uses the **aggregate** need. The stricter "
                        f"**station-aware** need is **{_hc_station:.0f}**"
                    )
                    if _hc_station > avail_hc:
                        _msg += (
                            f" — by that view you're ~{_hc_station - avail_hc:.0f} short"
                        )
                    _msg += "."
                    if _missed_station:
                        _msg += (
                            f" At current staffing, **{int(_missed_station)}** of "
                            f"{metrics.get('total_units', 0)} units can't be built at the "
                            f"binding station(s)."
                        )
                    _msg += " See the 📊 Capacity tab's *Gap* column for which stations."
                    st.caption(_msg)
                else:
                    st.caption(
                        f"Stricter **station-aware** need is **{_hc_station:.0f}** "
                        f"(≤ {avail_hc:.0f} available) and every station can build its "
                        "share — covered at the station level too."
                    )

        with col_save:
            st.markdown("**💾 Save this scenario**")
            _placeholder = f"{location} {month_label}".strip()
            _save_name = st.text_input(
                "Scenario name", key=f"exec_save_name_{location}",
                placeholder=_placeholder or "e.g. Cypress May",
            )
            if st.button("💾 Save executive scenario",
                         key=f"exec_save_btn_{location}", width="stretch"):
                token = _github_token()
                name = (_save_name or "").strip()
                if not name:
                    st.warning("Enter a scenario name first.")
                elif not token:
                    st.error("GitHub token not configured. Ask your admin to add "
                             "`github_token` to Streamlit Secrets.")
                else:
                    # Freeze the per-station headcount so the side-by-side shows
                    # the staffing as-of-save (matches the frozen metrics).
                    try:
                        _crew_hc = {str(s): int(h) for s, h in
                                    inputs["crew_config"]["HC"].items()}
                    except Exception:
                        _crew_hc = {}
                    assumptions = {
                        "units": metrics["total_units"],
                        "total_earned_hours": metrics["total_earned_hours"],
                        "base_efficiency": metrics["base_efficiency"],
                        "new_hire_pct": metrics["new_hire_pct"],
                        "new_hire_productivity": metrics["new_hire_productivity"],
                        "absenteeism": metrics["absenteeism"],
                        "overtime": metrics.get("overtime", 0.0),
                        "safety": metrics["safety"],
                        "shift_minutes": int(inputs.get("shift", 480)),
                        "working_days": int(inputs.get("days", 20)),
                        "available_hc": avail_hc,
                        "support_hc": support_hc,
                        "crew_hc": _crew_hc,
                        "schedule_label": month_label,
                    }
                    try:
                        with st.spinner("Saving executive scenario..."):
                            save_exec_scenario_to_github(
                                location, name, assumptions, metrics, token)
                        _track_and_flush("exec_scenario_save",
                                         facility=location, name=name)
                        st.success(f"✅ Saved executive scenario **{name}**.")
                        _post_save_redeploy_notice(f"exec_{location}")
                    except Exception as e:
                        _show_github_error(e, "Save")

            saved = _exec_scenarios_newest_first(location)
            if saved:
                pick = st.selectbox("Saved scenarios", saved, key=f"exec_pick_{location}")
                c1, c2 = st.columns(2)
                if c1.button("📥 Re-apply", key=f"exec_load_{location}",
                             width="stretch"):
                    a = (get_exec_scenario(location, pick) or {}).get("assumptions", {})
                    # Stash under a NON-widget key; the sidebar applies it to the
                    # slider widgets on the next run, before they're instantiated
                    # (writing a widget key after instantiation raises).
                    st.session_state[f"_exec_reapply_{location}"] = {
                        "new_hire_pct": a.get("new_hire_pct", 0),
                        "new_hire_productivity": a.get("new_hire_productivity", 0.5),
                        "absenteeism": a.get("absenteeism", 0.05),
                        "overtime": a.get("overtime", 0.0),
                    }
                    st.session_state[f"_exec_reapply_toast_{location}"] = pick
                    st.rerun()
                if c2.button("🗑 Delete", key=f"exec_del_{location}",
                             width="stretch") and _confirm_destructive(
                        f"_exec_delete_pending_{location}", pick,
                        f"⚠️ Delete executive scenario **{pick}** for everyone? "
                        "This can't be undone — click **🗑 Delete** again to confirm.",
                ):
                    token = _github_token()
                    if not token:
                        st.error("GitHub token not configured.")
                    else:
                        try:
                            with st.spinner(f"Deleting '{pick}'..."):
                                delete_exec_scenario_on_github(location, pick, token)
                            st.success(f"🗑 Deleted **{pick}**.")
                            st.rerun()
                        except Exception as e:
                            _show_github_error(e, "Delete")
    else:
        st.info("📭 Load a schedule to compute the live summary. Saved scenarios "
                "(if any) still appear below.")

    # --- Multi-facility side-by-side (from saved frozen snapshots) ------
    st.markdown("---")
    st.markdown("#### 🏭 Side-by-side comparison (saved scenarios)")
    st.caption("Pick a saved scenario per facility to compare them like the "
               "executive sheet. Each column is **frozen** as of its save.")
    fac_cols = st.columns(len(LOCATIONS))
    chosen = {}
    for i, fac in enumerate(LOCATIONS):
        # Newest save first (same order as the Re-apply picker above) and
        # default to it — index=1, after "—".
        names = _exec_scenarios_newest_first(fac)
        with fac_cols[i]:
            if names:
                chosen[fac] = st.selectbox(
                    fac, ["—"] + names, index=1, key=f"exec_cmp_{fac}")
            else:
                st.caption(f"**{fac}** — none saved")
                chosen[fac] = "—"

    # {facility column label: flat {metric: value} map}, plus the per-station
    # headcount snapshot for each column (frozen at save; live crew as fallback
    # for scenarios saved before crew_hc was captured).
    rowmaps = {}
    crewmaps = {}
    for fac in LOCATIONS:
        nm = chosen.get(fac, "—")
        if nm and nm != "—":
            block = get_exec_scenario(fac, nm) or {}
            m = block.get("metrics", {})
            if m:
                col = f"{fac} — {nm}"
                rowmaps[col] = _exec_summary_rowmap(m)
                ch = (block.get("assumptions", {}) or {}).get("crew_hc") or {}
                if not ch:
                    try:
                        ch = {str(s): int(h) for s, h
                              in load_facility_crew_df(fac)["HC"].items()}
                    except Exception:
                        ch = {}
                crewmaps[col] = ch

    if rowmaps:
        _cols = list(rowmaps.keys())
        # Single table; section titles become shaded divider rows (blank cells).
        _disp = []
        for _sec_title, _ref_rows in _exec_summary_sections({}):
            _disp.append({"Metric": _sec_title, **{c: "" for c in _cols}})
            for lbl, _ in _ref_rows:
                _disp.append({"Metric": lbl,
                              **{c: rowmaps[c].get(lbl, "—") for c in _cols}})
        _cmp_df = pd.DataFrame(_disp, columns=["Metric"] + _cols)
        try:
            st.dataframe(_cmp_df.style.apply(_style_exec_table, axis=1),
                         width="stretch", hide_index=True)
        except Exception:
            st.dataframe(_cmp_df, width="stretch", hide_index=True)

        # Per-station headcount side-by-side (only stations staffed somewhere).
        _station_order = list(STATION_DEFAULTS.keys()) + list(SUPPORT_ROLES)
        _present = [s for s in _station_order
                    if any(int(crewmaps[c].get(s, 0)) > 0 for c in _cols)]
        if _present:
            st.markdown("**👷 Station headcount**")
            _hc_rows = [{"Station": s, **{c: int(crewmaps[c].get(s, 0)) for c in _cols}}
                        for s in _present]
            _hc_rows.append({"Station": "Total",
                             **{c: int(sum(crewmaps[c].values())) for c in _cols}})
            _hc_df = pd.DataFrame(_hc_rows, columns=["Station"] + _cols)

            def _style_total(row):
                if str(row.get("Station", "")) == "Total":
                    return ["background-color:#dde6f0; font-weight:700"] * len(row)
                return ["" for _ in row]
            try:
                st.dataframe(_hc_df.style.apply(_style_total, axis=1),
                             width="stretch", hide_index=True)
            except Exception:
                st.dataframe(_hc_df, width="stretch", hide_index=True)

        # CSV carries the Section grouping in its own column (no divider rows).
        _csv_records = []
        for _sec_title, _ref_rows in _exec_summary_sections({}):
            for lbl, _ in _ref_rows:
                _csv_records.append({"Section": _sec_title, "Metric": lbl,
                                     **{c: rowmaps[c].get(lbl, "—") for c in _cols}})
        for s in _present:
            _csv_records.append({"Section": "Station headcount", "Metric": s,
                                 **{c: int(crewmaps[c].get(s, 0)) for c in _cols}})
        if _present:
            _csv_records.append({"Section": "Station headcount", "Metric": "Total",
                                 **{c: int(sum(crewmaps[c].values())) for c in _cols}})
        st.download_button(
            "📥 Download comparison CSV",
            pd.DataFrame(_csv_records).to_csv(index=False).encode("utf-8"),
            file_name="executive_summary.csv",
            mime="text/csv",
        )
    else:
        st.info("Save a scenario for each facility (above) to build the "
                "side-by-side comparison.")


def tab_overview(units, capacity, batt_type, inputs, schedule_month: str = "", weekly_util=None):
    # =============================================================
    # Empty-schedule guard FIRST — don't render zeroed KPIs and force
    # the planner to scroll past meaningless 0s before reaching the
    # "load a schedule" message at the bottom.
    # =============================================================
    total_units = len(units)
    if total_units == 0 or capacity is None or capacity.empty:
        st.info(
            "ℹ️ **No schedule loaded.** Upload a CSV or type a few SKUs "
            "in the sidebar to see your plan's capacity picture and "
            "recommended actions."
        )
        return

    # =============================================================
    # Compute headline numbers
    # =============================================================
    total_labor = int(units["total_labor"].sum())
    total_batt = int(units["Bat"].where(units["Battery"] > 0, 0).sum())
    # Production HC drives the Required-HC gap; support (Lead/Other) is shown
    # separately so it never makes the plant look falsely overstaffed.
    production_hc, support_hc, total_hc = _split_crew_hc(inputs["crew_config"])
    _prod_crew = inputs["crew_config"].loc[~inputs["crew_config"].index.isin(SUPPORT_ROLES)]
    active_stations = int((_prod_crew["HC"] > 0).sum())
    total_required_hc = int(capacity["required_hc"].sum())
    total_hc_gap = total_required_hc - production_hc

    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    no_station_list = capacity[capacity["overall_status"] == "🔴 NO STATION"].index.tolist()
    near_cap = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
    tight = capacity[capacity["overall_status"] == "🟡 TIGHT"].index.tolist()
    ok_stations = capacity[capacity["overall_status"] == "🟢 OK"].index.tolist()

    # =============================================================
    # Big status banner
    # =============================================================
    month_label = f" · {schedule_month}" if schedule_month else ""
    st.markdown(f"### {inputs['location']} Manufacturing{month_label}")

    has_blocker = bool(over_stations or no_station_list)
    has_warning = bool(near_cap)

    if has_blocker:
        blockers = []
        if over_stations:
            blockers.append(f"{len(over_stations)} station(s) **over capacity** ({', '.join(over_stations)})")
        if no_station_list:
            blockers.append(f"{len(no_station_list)} station(s) **not available** here ({', '.join(no_station_list)})")
        st.error(
            "🚨 **Action needed.** " + " · ".join(blockers)
            + "\n\nSee the **Capacity** and **Recommendations** tabs for fixes."
        )
    elif has_warning:
        st.warning(
            f"⚠️ **Watch closely.** {len(near_cap)} station(s) near capacity: "
            f"**{', '.join(near_cap)}**. Small changes could push them over."
        )
    else:
        st.success("✅ **All clear.** Every station is within safe capacity for this plan.")

    st.markdown("---")

    # =============================================================
    # Hero KPIs — planner's decision row
    # ----------------------------------------------------------------
    # Story arc: what was asked (Planned) → what's possible (Buildable)
    # → the gap as % (Coverage) → workload context (Total work). The
    # bottleneck station name lives in the single-line readout BELOW
    # this row, not in a 5th KPI column — keeps the headline focused on
    # the question "can I build this plan?" and avoids naming the
    # bottleneck in 4 different places.
    # =============================================================
    st.markdown("#### 📦 What this plan builds")

    # Buildable is LINE-AWARE: the plant runs parallel production lines
    # (Compressor / Generator / EBOSS / PM) whose outputs add up, so one line
    # isn't throttled by another line's bottleneck (e.g. PDS/compressor is not
    # limited by the Battery station, which only EBOSS/PM use). A line that
    # needs an unstaffed station contributes 0 (the others still count).
    _excluded = inputs.get("excluded_stations")
    _bl = buildable_by_line(units, capacity, excluded_stations=_excluded)
    buildable_units = int(_bl.get("total", 0) or 0)
    # _max_units_at_current_mix still drives the single-bottleneck readout below.
    _mu = _max_units_at_current_mix(capacity, total_units, excluded_stations=_excluded)
    coverage_pct = (buildable_units / total_units * 100.0) if total_units > 0 else 0.0
    gap_units = buildable_units - total_units  # negative = short; positive = surplus

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Units planned", f"{total_units:,}",
        help="Total finished-good units in this schedule (or manual entry).",
    )
    c2.metric(
        "Buildable units", f"{buildable_units:,}",
        help=(
            "How many units of the current SKU mix the facility can build this "
            "month. Summed across the parallel production lines (Compressor / "
            "Generator / EBOSS / PM) — each scales to its OWN bottleneck, so one "
            "line isn't throttled by another's constraint. See the per-line "
            "breakdown below."
        ),
    )
    # Coverage delta-color: "normal" means a POSITIVE delta (surplus = more
    # buildable than planned) renders green, and a NEGATIVE delta (short of
    # plan = bad) renders red. That's the natural "growth is good" reading
    # for this metric — we want planners to see red when they can't cover
    # the plan and green when they have headroom.
    c3.metric(
        "Coverage", f"{coverage_pct:.0f}%",
        delta=f"{gap_units:+,} vs plan",
        delta_color="normal",
        help=(
            "Buildable ÷ Planned. <100% = the plan exceeds capacity at this "
            "mix; ≥100% = you have headroom. The delta shows the unit gap."
        ),
    )
    _tl_min = int(round(float(total_labor or 0)))
    _tl_hr = float(total_labor or 0) / 60.0
    c4.metric(
        "Total work", f"{_tl_min:,} p-min",
        help="Total labor required across all stations, shown as person-minutes (with person-hours below).",
    )
    c4.caption(f"≈ {_tl_hr:,.1f} p-hr")

    # Per-line breakdown — show how buildable decomposes across the parallel
    # production lines (only meaningful when the plan spans more than one line).
    _render_buildable_by_line(_bl, buildable_units)

    if _excluded:
        st.caption(
            "⏸️ **Buildable** excludes "
            + ", ".join(f"**{s}**" for s in _excluded)
            + " (set in the sidebar → **⏱️ Working-time settings**) — those "
            "stations don't cap the count. They're still shown on the "
            "**Capacity** tab."
        )

    # ----------------------------------------------------------------
    # Bottleneck line — single, definitive, sits right under the KPIs.
    # Replaces the previous markdown headline + 2-metric Labor/Throughput
    # row + caption (the data is preserved inline here).
    # ----------------------------------------------------------------
    _lu = float(_mu.get("labor_max_util_safe", 0) or 0)
    _tu = float(_mu.get("thru_max_util_safe", 0) or 0)
    _lm = int(_mu.get("labor_max_units", 0) or 0)
    _tm = int(_mu.get("thru_max_units", 0) or 0)
    _lb = _mu.get("labor_bottleneck", "") or "—"
    _tb = _mu.get("thru_bottleneck", "") or "—"

    if _mu["infeasible"]:
        _ms = ", ".join(f"**{s}**" for s in _mu["missing_stations"])
        _pm = int(_mu.get("potential_max_units", 0))
        _pb = _mu.get("potential_bottleneck", "")
        _pu = float(_mu.get("potential_util_safe", 0) or 0)
        if _pm > 0 and _pb:
            _ceiling = (
                f" Once those are staffed, the next bottleneck would be "
                f"**{_pb}** at {_pu:.0%} util — capping the mix at "
                f"**{_pm:,} units**."
            )
        elif _pm > 0:
            _ceiling = (
                f" Once those are staffed, no other station is binding — "
                f"the mix could go up to **{_pm:,} units**."
            )
        else:
            _ceiling = ""
        _one = len(_mu["missing_stations"]) == 1
        st.error(
            f"🏭 **Whole-mix cap: 0 units** — holding the current SKU mix in "
            f"fixed proportion, {_ms} {'has' if _one else 'have'} no HC but this "
            f"mix needs it, so the mix can't scale as a single block. "
            f"(Lines that don't route through {'that station' if _one else 'those stations'} "
            f"can still build — see the per-line **Buildable** breakdown above.) "
            f"Add headcount in the 👥 **Station headcount** sidebar panel to "
            f"unlock the full plan.{_ceiling}"
        )
    elif _mu.get("bottleneck"):
        st.markdown(
            f"🎯 **Bottleneck: {_mu['bottleneck']} · "
            f"{_mu['max_util_safe']:.0%} util.** "
            f"Labor cap: **{_lm:,}** ({_lb}) · "
            f"Throughput cap: **{_tm:,}** ({_tb})."
        )
    else:
        st.markdown(
            f"🎯 **No binding station detected.** "
            f"Mix scales freely — capacity headroom available."
        )

    st.caption(
        "Scales the current SKU mix proportionally until any station hits "
        "its safe capacity (HC × shift × days × efficiency × safety) or "
        "throughput cap. Relieve the bottleneck by adding HC to that "
        "station in the 👥 **Station headcount** panel."
    )

    st.markdown("#### 👥 Headcount picture")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric(
        "People you have", f"{production_hc}",
        help=(
            f"Production headcount across {active_stations} active stations."
            + (f" Plus {support_hc} support (Lead/Other) → {total_hc} total "
               "headcount." if support_hc else "")
        ),
    )
    c6.metric(
        "People you need", f"{total_required_hc}",
        help=(
            f"Sum of headcount required at every station to meet this plan, "
            f"using your buffer ({inputs['safety']:.2f}) and "
            f"productive-time settings ({inputs['efficiency']:.3f})."
        ),
    )
    gap_label = "Short" if total_hc_gap > 0 else ("Surplus" if total_hc_gap < 0 else "Even")
    c7.metric(
        gap_label, f"{abs(total_hc_gap)}",
        delta=f"{total_hc_gap:+d}" if total_hc_gap != 0 else None,
        delta_color="inverse",
        help="Difference between people you need and people you have.",
    )
    short_count = int((capacity["hc_gap"] > 0).sum())
    c8.metric(
        "Stations needing more people", f"{short_count}",
        help="Stations where required headcount exceeds what you have today.",
    )

    st.markdown("---")

    # =============================================================
    # Proposed-schedule preview — Sequencer feature. When the planner has
    # generated a proposal but not yet accepted/discarded it, show its
    # weekly heatmap side-by-side with the current one so the comparison
    # is glanceable.
    # =============================================================
    _proposed = st.session_state.get("_proposed_schedule")
    if (
        isinstance(_proposed, pd.DataFrame) and not _proposed.empty
        and weekly_util is not None and not weekly_util.empty
    ):
        # Compute the proposal's weekly heatmap on the fly using the same
        # helper that produced `weekly_util` for the current schedule.
        try:
            # Pull machine_df / acc_df from cached loaders (same path main() uses).
            # Project the facility's routing so the proposed heatmap matches the
            # current Capacity tab numbers at this plant.
            _machine = _project_facility_routing(
                _load_machine_df(_csv_mtime("machine_clean.csv")),
                inputs.get("location", "Henderson"),
            )
            _acc = _load_acc_df(_csv_mtime("acc_clean.csv"))
            proposed_util = _weekly_utilization_for(
                _proposed, _machine, _acc, inputs,
            )
        except Exception:
            proposed_util = None
        if proposed_util is not None and not proposed_util.empty:
            st.markdown("#### 💡 Current vs proposed (side-by-side)")
            st.caption(
                "Left: your **current** weekly pressure. Right: the Sequencer's "
                "**proposed** level-loaded version. Scroll up to ✅ Accept or 🚫 Discard."
            )

            def _make_heatmap(df_util, title):
                fig = px.imshow(
                    df_util,
                    text_auto=".0%",
                    color_continuous_scale=[
                        (0.00, "#16A34A"),
                        (0.60, "#A3E635"),
                        (0.75, "#FACC15"),
                        (0.90, "#F97316"),
                        (1.00, "#DC2626"),
                    ],
                    zmin=0, zmax=1.5,
                    aspect="auto",
                    labels={"x": "Week", "y": "Station", "color": "Util %"},
                )
                fig.update_layout(
                    title=dict(text=title, font=dict(size=14)),
                    height=min(380, 60 + 26 * max(len(df_util.index), 1)),
                    margin=dict(l=4, r=4, t=40, b=4),
                    coloraxis_showscale=False,
                )
                return fig

            cL, cR = st.columns(2)
            with cL:
                st.plotly_chart(
                    _make_heatmap(weekly_util, "Current"),
                    width="stretch",
                    key="overview_heatmap_current",
                )
            with cR:
                st.plotly_chart(
                    _make_heatmap(proposed_util, "Proposed"),
                    width="stretch",
                    key="overview_heatmap_proposed",
                )
            st.markdown("---")

    # =============================================================
    # Weekly utilization heatmap — STAGING / Step 1 of the time-dimension
    # work. Renders only when the schedule has a PRODUCTION DAY column;
    # otherwise the helper returns None and we skip it cleanly.
    # =============================================================
    if weekly_util is not None and not weekly_util.empty:
        st.markdown("#### 📅 Weekly pressure")
        st.caption(
            "Labor utilization per station × week (at a 5-working-days-per-week "
            "reference). Spot which week is the bottleneck even when the "
            "whole-month view averages the pressure out."
        )
        # Clip the color scale at 1.5× so spikes don't wash out the lower end.
        # zmin=0 keeps green visible; zmax=1.5 means anything ≥150% looks
        # the same deep red — which is fine since that's "definitely red".
        _hm = px.imshow(
            weekly_util,
            text_auto=".0%",
            color_continuous_scale=[
                (0.00, "#16A34A"),  # green
                (0.60, "#A3E635"),  # light green
                (0.75, "#FACC15"),  # yellow
                (0.90, "#F97316"),  # orange
                (1.00, "#DC2626"),  # red
            ],
            zmin=0, zmax=1.5,
            aspect="auto",
            labels={"x": "Week", "y": "Station", "color": "Util %"},
        )
        _hm.update_layout(
            height=min(400, 60 + 28 * len(weekly_util.index)),
            margin=dict(l=4, r=4, t=8, b=4),
            coloraxis_showscale=False,
        )
        st.plotly_chart(
            _hm, width="stretch",
            key="overview_weekly_heatmap_full",
        )
        st.markdown("---")

    # =============================================================
    # Recommended actions — auto-generated from data
    # =============================================================
    st.markdown("#### 🎯 Recommended actions")
    actions = []

    # NOTE: empty-schedule guard now fires at the very top of the tab
    # (returns before any KPI rendering), so we don't repeat it here.

    # Separate over-capacity stations into "throughput-only" vs "needs HC".
    # A red Overall status can come from labor demand exceeding headcount
    # (fix = add people), or from throughput (cells/cycle) exceeding daily
    # need (fix = add stations/cells or extend working days). Telling a
    # planner to "hire" when the constraint is actually fixture cycle time
    # is misleading, so we split them out.
    labor_over_stations = []  # need more headcount
    throughput_over_stations = []  # need more cells / longer days
    for st_disp in over_stations:
        labor_over = str(capacity.loc[st_disp, "labor_status_safe"]) == "🔴"
        thru_over = str(capacity.loc[st_disp, "thru_status_safe"]) == "🔴"
        # If labor is the over-driver, hiring helps; otherwise treat as throughput.
        if labor_over:
            labor_over_stations.append(st_disp)
        elif thru_over:
            throughput_over_stations.append(st_disp)
        else:
            # Edge case (raw-but-not-safe over) — surface as labor anyway.
            labor_over_stations.append(st_disp)

    # 1. Stations missing entirely (HC=0 with demand)
    if no_station_list:
        actions.append(
            f"**Fix facility setup.** {len(no_station_list)} station(s) have demand but 0 headcount: "
            f"{', '.join(no_station_list)}. Either add people in the sidebar or remove those units from the schedule."
        )

    # 2. Labor-driven over-capacity — name the per-station gap
    if labor_over_stations:
        parts = []
        for st_disp in labor_over_stations:
            gap = 0
            try:
                gap = int(capacity.loc[st_disp, "hc_gap"])
            except Exception:
                gap = 0
            if gap > 0:
                parts.append(f"**~{gap} more at {st_disp}**")
            else:
                # Gap math couldn't pin it (rounding or no required_hc) — fall back to a generic call-out.
                parts.append(f"**more capacity at {st_disp}**")
        actions.append(
            f"**Add {', '.join(parts)}** to bring utilization below 100% (with your safety buffer). "
            f"Alternatives: run overtime, extend working days, or defer some units."
        )

    # 3. Throughput-driven over-capacity — different fix
    if throughput_over_stations:
        # Battery Assembly is the canonical case (cell count × cycle time).
        if "Battery Assembly" in throughput_over_stations:
            actions.append(
                "🔋 **Battery throughput is the bottleneck.** Add battery cells "
                "(Stations/Cells) or extend working days — hiring more people at "
                "Battery won't help, because the constraint is the assembly fixture cycle."
            )
            others = [s for s in throughput_over_stations if s != "Battery Assembly"]
        else:
            others = list(throughput_over_stations)
        if others:
            actions.append(
                f"**Throughput is short at {', '.join(others)}** — these stations need more "
                "parallel cells (Stations/Cells in the sidebar) or longer working days. "
                "Adding labor without more cells won't move the cycle."
            )

    # 4. Headcount short overall (but no station is individually red)
    if total_hc_gap > 0 and not labor_over_stations:
        actions.append(
            f"**Plan to add ~{total_hc_gap} people** across the line to comfortably hit this plan."
        )

    # 5. Near-cap warnings (only when nothing is already red)
    if near_cap and not over_stations:
        actions.append(
            f"**Monitor {', '.join(near_cap)}** — these are 90%+ utilized and risky for any schedule change."
        )

    # 6. Surplus
    if total_hc_gap < -3:
        idle = capacity[capacity["labor_util_safe"] < 0.5].index.tolist()
        if idle:
            actions.append(
                f"**Surplus capacity at {', '.join(idle[:3])}** — consider cross-training "
                f"these {abs(total_hc_gap)} people to support bottleneck stations."
            )

    if not actions:
        actions.append("✅ **No action needed** — this plan is well-balanced. Maintain current staffing.")

    for i, action in enumerate(actions, 1):
        st.markdown(f"{i}. {action}")
    _render_action_jumps(over_stations, no_station_list, near_cap)

    st.markdown("---")

    # =============================================================
    # Station status — colored cards instead of a dense table
    # =============================================================
    st.markdown("#### 📊 Station status at a glance")
    st.caption(
        "Each card shows one station. Color = overall status (labor + throughput). "
        "Number = how loaded it is."
    )

    # Build cards in rows of 4
    stations_to_show = list(capacity.index)
    color_map = {
        "🔴 OVER":       ("#E15759", "white"),
        "🔴 NO STATION": ("#999999", "white"),
        "🟠 NEAR-CAP":   ("#F0A04B", "white"),
        "🟡 TIGHT":      ("#F2D75D", "#333"),
        "🟢 OK":         ("#70AD47", "white"),
        "⚪ N/A":         ("#EEEEEE", "#666"),
    }
    for i in range(0, len(stations_to_show), 4):
        cols = st.columns(4)
        for j, st_name in enumerate(stations_to_show[i:i + 4]):
            row = capacity.loc[st_name]
            status = row["overall_status"]
            bg, fg = color_map.get(status, ("#EEEEEE", "#333"))
            util_pct = row["labor_util_safe"] * 100 if not row.get("station_missing") else 0
            hc = int(row["HC"])
            req = int(row["required_hc"])
            status_label = status.split(" ", 1)[1] if " " in status else status
            cols[j].markdown(
                f"""
<div style="background:{bg};color:{fg};padding:12px;border-radius:8px;margin-bottom:8px;">
<div style="font-weight:600;font-size:0.95rem;">{st_name}</div>
<div style="font-size:0.78rem;opacity:0.85;">{status_label}</div>
<div style="font-size:1.6rem;font-weight:700;margin-top:6px;">{util_pct:.0f}%</div>
<div style="font-size:0.78rem;opacity:0.85;">{hc} people · need {req}</div>
</div>
""",
                unsafe_allow_html=True,
            )

    st.markdown("---")

    # =============================================================
    # Battery type mix (kept — visual is clearer than table)
    # =============================================================
    st.markdown("#### 🔋 Batteries needed by type")
    fig = px.bar(
        batt_type, x="batt_type", y="total_batteries", color="batt_type",
        text="total_batteries",
        labels={"batt_type": "Battery type", "total_batteries": "Batteries"},
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(showlegend=False, height=340, margin=dict(t=20, b=20))
    st.plotly_chart(fig, width="stretch")


def tab_capacity_vs_demand(capacity, inputs, batt_sku=None, batt_type=None, total_units: int = 0, units=None):
    st.header("📊 Capacity vs Demand")
    st.markdown(
        "**Can each station's capacity keep up with this plan's demand?** "
        "Each row is one station. Use the chart for a visual scan, the simple table for the headline numbers."
    )
    st.caption(
        f"Plan settings — shift: {inputs['shift']} min · "
        f"working days: {inputs['days']} · "
        f"buffer: {inputs['safety']:.2f} · "
        f"productive time: {inputs['efficiency']:.3f}"
    )
    # Stations the planner has excluded from buildable-unit sizing (still shown
    # in the tables below, marked ⏸️). Read once so it's in scope for the whole
    # tab, including the station summary that renders even when total_units==0.
    _excluded = inputs.get("excluded_stations") or []
    if _excluded:
        st.caption(
            "⏸️ Excluded from buildable-unit sizing: "
            + ", ".join(f"**{s}**" for s in _excluded)
            + " — still shown below, but they don't cap Buildable units. "
            "Change this in the sidebar → **⏱️ Working-time settings**."
        )

    # =============================================================
    # Headline caps — how many units this mix can build, broken out by
    # which constraint is biting (labor person-mins vs throughput bays).
    # Mirrors the Overview KPI so planners working in the Capacity tab
    # don't need to bounce back to Overview to see the upper bound.
    # =============================================================
    if total_units > 0:
        _mu = _max_units_at_current_mix(capacity, total_units, excluded_stations=_excluded)
        _lu = float(_mu.get("labor_max_util_safe", 0) or 0)
        _tu = float(_mu.get("thru_max_util_safe", 0) or 0)
        _lm = int(_mu.get("labor_max_units", 0) or 0)
        _tm = int(_mu.get("thru_max_units", 0) or 0)
        _lb = _mu.get("labor_bottleneck", "") or "—"
        _tb = _mu.get("thru_bottleneck", "") or "—"
        # Line-aware buildable (parallel Compressor/Generator/EBOSS/PM lines).
        _bl = buildable_by_line(units, capacity, excluded_stations=_excluded) if units is not None else {"lines": [], "total": 0}
        _bl_total = int(_bl.get("total", 0) or 0)
        _multi_line = len(_bl.get("lines", [])) > 1
        if _lu > 0 or _tu > 0:
            st.markdown("#### 🏭 Unit caps for this mix")
            if _mu["infeasible"]:
                _ms = ", ".join(f"**{s}**" for s in _mu["missing_stations"])
                st.warning(
                    f"⚠️ **{_ms}** {'have' if len(_mu['missing_stations'])>1 else 'has'} no HC — "
                    "current build is blocked at **0 units**. Caps below show "
                    "what would be possible once those stations are staffed."
                )
            cP, cL, cT = st.columns(3)
            _pm = int(_mu.get("potential_max_units", 0) or 0)
            _pb = _mu.get("potential_bottleneck", "") or "—"
            _pu = float(_mu.get("potential_util_safe", 0) or 0)
            cP.metric(
                "Max units (all lines)" if _multi_line else "Max units (tighter cap)",
                f"{_bl_total:,}" if (_multi_line and _bl_total > 0)
                else (f"{_pm:,}" if _pu > 0 else f"{total_units:,}"),
                help=(
                    "Sum of the parallel production lines' caps — each line "
                    "(Compressor/Generator/EBOSS/PM) scaled to its OWN bottleneck, "
                    "so one line isn't throttled by another's. Per-line breakdown below."
                ) if _multi_line else
                "The smaller of the labor and throughput caps for the current mix.",
            )
            cL.metric(
                "Labor-capped units",
                f"{_lm:,}" if _lu > 0 else "—",
                help=(
                    "Person-minute constraint. Relieve by adding HC, "
                    "working days, or shift minutes."
                ),
            )
            cT.metric(
                "Throughput-capped units",
                f"{_tm:,}" if _tu > 0 else "—",
                help=(
                    "Bays × cycle-time constraint. Relieve by adding "
                    "concurrent bays or shortening cycle time."
                ),
            )
            cP.caption(
                f"Sum across {len(_bl['lines'])} lines · global bottleneck **{_pb}**"
                if _multi_line else f"Bottleneck: **{_pb}** · {_pu:.0%} util"
            )
            cL.caption(f"Bottleneck: **{_lb}** · {_lu:.0%} util")
            cT.caption(f"Bottleneck: **{_tb}** · {_tu:.0%} util")
            st.caption(
                f"Current plan: **{total_units:,} units**. Caps scale the "
                "current SKU mix proportionally until the named station "
                "hits its safe capacity."
            )
            if _multi_line:
                _render_buildable_by_line(_bl, _bl_total)

            # ---------------------------------------------------------
            # Per-area unit caps — what each production area can support
            # on its own, ignoring bottlenecks elsewhere. Lets a planner
            # see (e.g.) the Accessories or Trailer ceiling separate from
            # an easily-relievable constraint like Shipping.
            # ---------------------------------------------------------
            from core.constants import STATION_AREA_GROUPS

            grouped = {s for stns in STATION_AREA_GROUPS.values() for s in stns}
            area_groups = dict(STATION_AREA_GROUPS)
            leftover = [s for s in capacity.index if s not in grouped]
            if leftover:
                area_groups["Other"] = leftover

            area_rows = []
            for area, stations in area_groups.items():
                sub = capacity[capacity.index.isin(stations)]
                if sub.empty:
                    continue
                _amu = _max_units_at_current_mix(sub, total_units, excluded_stations=_excluded)
                _a_pm = int(_amu.get("potential_max_units", 0) or 0)
                _a_pu = float(_amu.get("potential_util_safe", 0) or 0)
                # Only meaningful when the area actually carries demand.
                if _a_pu <= 0:
                    continue
                area_rows.append({
                    "Area": area,
                    "Max units": _a_pm,
                    "Bottleneck": _amu.get("potential_bottleneck", "") or "—",
                    "Util %": round(_a_pu * 100),
                    "Blocked?": "⚠️ unstaffed" if _amu.get("infeasible") else "",
                })

            if area_rows:
                area_df = (
                    pd.DataFrame(area_rows)
                    .sort_values("Max units")
                    .reset_index(drop=True)
                )
                with st.expander("🗂️ Unit caps by production area", expanded=False):
                    st.caption(
                        "Each area's ceiling for the current mix, computed from "
                        "only that area's stations. The lowest-cap area is the "
                        "true binding area — others (e.g. Shipping) can often be "
                        "relieved later by adding HC there."
                    )
                    st.dataframe(
                        area_df, width="stretch", hide_index=True,
                        column_config={
                            "Area": st.column_config.TextColumn(
                                "Area", help="Production area (grouped stations)."),
                            "Max units": st.column_config.NumberColumn(
                                "Max units", format="%d",
                                help="Units of the current mix this area alone can build."),
                            "Bottleneck": st.column_config.TextColumn(
                                "Bottleneck", help="The binding station within the area."),
                            "Util %": st.column_config.NumberColumn(
                                "Util %", format="%d%%",
                                help="The bottleneck station's utilization at the current plan."),
                            "Blocked?": st.column_config.TextColumn(
                                "Blocked?", help="Flagged when a needed station in the area has no HC."),
                        },
                    )

            # ---------------------------------------------------------
            # Space limits — max units driven by physical cells/stations
            # (throughput), not by headcount. Adding people doesn't relieve
            # a space-bound station; only adding cells or cutting cycle time
            # does. Surfaces the per-station + per-area space ceiling that's
            # otherwise buried in the full-detail expander.
            # ---------------------------------------------------------
            _space_days = int(inputs.get("days", 0) or 0)
            stn_rows = []
            for st_disp, row in capacity.iterrows():
                need_day = float(row.get("need_per_day", 0) or 0)
                cap_day = float(row.get("thru_cap_safe", 0) or 0)
                util = float(row.get("thru_util_safe", 0) or 0)
                # Only stations carrying demand AND with a defined cell cap
                # are space-constrained; skip the rest.
                if need_day <= 0 or cap_day <= 0 or util <= 0:
                    continue
                conc = int(row.get("Conc", 0) or 0)
                is_batt = "Battery" in str(st_disp)
                stn_rows.append({
                    "Station": f"🔋 {st_disp}" if is_batt else str(st_disp),
                    "Stations/Cells": conc,
                    "Cycle min/unit": round(float(row.get("avg_cycle", 0) or 0), 1),
                    "Units/day per cell": round(cap_day / conc, 1) if conc > 0 else 0.0,
                    "Max units/day (cells)": int(round(cap_day)),
                    "Cell capacity / plan": int(round(cap_day * _space_days)),
                    "Mix ceiling (units)": int(total_units // util),
                    "Cells used %": round(util * 100),
                    "Status": row.get("thru_status_safe", "") or "—",
                })

            # Per-area space roll-up (reuses area_groups built just above).
            # Uses the HC-agnostic _area_space_cap (NOT the labor-aware
            # _max_units_at_current_mix) so the area's space bottleneck counts
            # cells regardless of staffing — consistent with the per-station
            # table above, which also ignores HC.
            sp_area_rows = []
            for area, stations in area_groups.items():
                sub = capacity[capacity.index.isin(stations)]
                _cap = _area_space_cap(sub, total_units, excluded_stations=_excluded)
                if _cap is None:
                    continue
                sp_area_rows.append({
                    "Area": area,
                    "Mix ceiling (units)": _cap["max_units"],
                    "Limiting cell": _cap["limiting_cell"],
                    "Cells used %": _cap["cells_used_pct"],
                })

            if stn_rows:
                with st.expander(
                    "🏗️ Space limits — max units by cells/stations", expanded=False
                ):
                    st.caption(
                        "Throughput is set by **physical cells, not people** — "
                        "adding HC won't lift these. Relieve a space-bound "
                        "station by adding **Stations/Cells** in the 👥 Station "
                        "headcount panel or shortening its cycle time."
                    )
                    stn_df = (
                        pd.DataFrame(stn_rows)
                        .sort_values("Mix ceiling (units)")
                        .reset_index(drop=True)
                    )
                    st.dataframe(
                        stn_df, width="stretch", hide_index=True,
                        column_config={
                            "Station": st.column_config.TextColumn(
                                "Station", help="🔋 Battery is measured in batteries/day, not units/day."),
                            "Stations/Cells": st.column_config.NumberColumn(
                                "Stations/Cells", format="%d",
                                help="Parallel cells/bays you have at this station today."),
                            "Cycle min/unit": st.column_config.NumberColumn(
                                "Cycle min/unit", format="%.1f",
                                help="Calendar minutes one cell spends per unit (labor ÷ crew)."),
                            "Units/day per cell": st.column_config.NumberColumn(
                                "Units/day per cell", format="%.1f",
                                help="Extra units/day each added cell buys = (shift ÷ cycle) × buffers. 🔋 Battery row is batteries/day per cell."),
                            "Max units/day (cells)": st.column_config.NumberColumn(
                                "Max units/day (cells)", format="%d",
                                help="Total daily cell throughput = cells × (shift ÷ cycle) × buffers, rounded. 🔋 Battery row is batteries/day."),
                            "Cell capacity / plan": st.column_config.NumberColumn(
                                "Cell capacity / plan", format="%d",
                                help="PHYSICAL ceiling: total units this station's cells can process over the plan = Max units/day × working days (efficiency & safety applied). 🔋 Battery row is batteries/plan."),
                            "Mix ceiling (units)": st.column_config.NumberColumn(
                                "Mix ceiling (units)", format="%d",
                                help="MIX ceiling: how far the CURRENT SKU mix can scale before this station's cells saturate. Different from cell capacity — it accounts for what you're actually building."),
                            "Cells used %": st.column_config.NumberColumn(
                                "Cells used %", format="%d%%",
                                help="Share of this station's cell-time the plan consumes. >100% = space bottleneck."),
                            "Status": st.column_config.TextColumn(
                                "Status", help="Throughput health: 🟢 OK · 🟡 Tight · 🟠 Near cap · 🔴 Over."),
                        },
                    )
                    st.caption(
                        "Lowest **Mix ceiling (units)** = your space bottleneck "
                        "(how far the current mix can grow). The 🔋 Battery row is "
                        "counted in **batteries/day**."
                    )

                    if sp_area_rows:
                        st.markdown("**By production area**")
                        sp_area_df = (
                            pd.DataFrame(sp_area_rows)
                            .sort_values("Mix ceiling (units)")
                            .reset_index(drop=True)
                        )
                        st.dataframe(
                            sp_area_df, width="stretch", hide_index=True,
                            column_config={
                                "Area": st.column_config.TextColumn(
                                    "Area", help="Production area (grouped stations)."),
                                "Mix ceiling (units)": st.column_config.NumberColumn(
                                    "Mix ceiling (units)", format="%d",
                                    help="How far the current mix can scale before this area's tightest cell saturates (same metric as the per-station column)."),
                                "Limiting cell": st.column_config.TextColumn(
                                    "Limiting cell", help="The space-binding station within the area (counts cells regardless of staffing)."),
                                "Cells used %": st.column_config.NumberColumn(
                                    "Cells used %", format="%d%%",
                                    help="The limiting cell's utilization at the current plan."),
                            },
                        )

            st.markdown("---")

    # =============================================================
    # Utilization chart (most scannable)
    # =============================================================
    chart_data = capacity.reset_index()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["labor_util_safe"] * 100,
        name="Labor utilization",
        marker_color="#5B9BD5",
        text=[f"{v*100:.0f}%" for v in chart_data["labor_util_safe"]],
        textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["thru_util_safe"] * 100,
        name="Throughput utilization",
        marker_color="#ED7D31",
        text=[f"{v*100:.0f}%" for v in chart_data["thru_util_safe"]],
        textposition="outside",
    ))
    fig.add_hline(y=100, line_dash="dash", line_color="red", annotation_text="100% (over capacity)")
    fig.add_hline(y=90, line_dash="dot", line_color="orange", annotation_text="90% (near cap)")
    fig.update_layout(
        barmode="group", height=420,
        yaxis_title="Utilization (%)",
        margin=dict(t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, width="stretch")

    # =============================================================
    # Simple, executive-friendly table
    # =============================================================
    st.markdown("#### 📋 Station summary")
    simple = pd.DataFrame(index=capacity.index)
    simple["Status"] = capacity["overall_status"]
    simple["People"] = capacity["HC"].astype(int)
    simple["Need"] = capacity["required_hc"].astype(int)
    simple["Gap"] = capacity["hc_gap"].astype(int)
    simple["Labor used %"] = (capacity["labor_util_safe"] * 100).round(0).astype(int)
    simple["Throughput used %"] = (capacity["thru_util_safe"] * 100).round(0).astype(int)
    simple["Avg cycle (min)"] = capacity["avg_cycle"].round(1)
    # Tag excluded stations so they stay visible but are clearly not counted
    # toward Buildable units. Display-only frame, so renaming the index is safe.
    if _excluded:
        _excl_set = set(_excluded)
        simple = simple.rename(
            index={s: f"{s} ⏸️" for s in list(simple.index) if s in _excl_set}
        )

    st.dataframe(
        simple, width="stretch", height=480,
        column_config={
            "Status":  st.column_config.TextColumn("Status", help="Color-coded overall health: 🟢 OK · 🟡 Tight · 🟠 Near cap · 🔴 Over · ⚪ Not at this facility."),
            "People":  st.column_config.NumberColumn("People", help="Headcount you have at this station."),
            "Need":    st.column_config.NumberColumn("Need", help="People you'd need to comfortably meet the plan."),
            "Gap":     st.column_config.NumberColumn("Gap", help="Need − People. Positive = short staffed."),
            "Labor used %": st.column_config.NumberColumn(
                "Labor used %",
                help="How much of the available labor at this station is consumed by the plan. >100% means over capacity.",
                format="%d%%",
            ),
            "Throughput used %": st.column_config.NumberColumn(
                "Throughput used %",
                help="How much of the line/cell time at this station is consumed. >100% means physical bottleneck.",
                format="%d%%",
            ),
        },
    )
    if _excluded:
        st.caption(
            "⏸️ = excluded from **Buildable units** sizing (sidebar → planning "
            "assumptions). Its utilization is still shown so the constraint stays "
            "visible until the model is fixed."
        )

    # =============================================================
    # Full detail — for engineers / planners who want all the numbers
    # =============================================================
    with st.expander("🔍 Show full detail (all columns)", expanded=False):
        disp = pd.DataFrame(index=capacity.index)
        disp["HC"] = capacity["HC"]
        disp["Required HC"] = capacity["required_hc"]
        disp["HC gap"] = capacity["hc_gap"]
        disp["Stations/Cells"] = capacity["Conc"]
        disp["Crew/unit"] = capacity["Crew"]
        disp["Labor demand (p-min)"] = capacity["labor_demand"].astype(int)
        disp["Labor demand (p-hr)"] = (capacity["labor_demand"] / 60.0).round(1)
        disp["Labor capacity (with buffer)"] = capacity["labor_cap_safe"].astype(int)
        disp["Labor capacity (p-hr)"] = (capacity["labor_cap_safe"] / 60.0).round(1)
        disp["Labor util (with buffer)"] = (capacity["labor_util_safe"] * 100).round(1).astype(str) + "%"
        disp["Labor status"] = capacity["labor_status_safe"]
        disp["Labor capacity (raw)"] = capacity["labor_cap_raw"].astype(int)
        disp["Labor util (raw)"] = (capacity["labor_util_raw"] * 100).round(1).astype(str) + "%"
        disp["Avg cycle (min)"] = capacity["avg_cycle"].round(1)
        disp["Units or batteries"] = capacity["units_or_batt"]
        disp["Need / day"] = capacity["need_per_day"].round(2)
        disp["Throughput cap (with buffer)"] = capacity["thru_cap_safe"].round(2)
        disp["Throughput util (with buffer)"] = (capacity["thru_util_safe"] * 100).round(1).astype(str) + "%"
        disp["Throughput status"] = capacity["thru_status_safe"]
        disp["Throughput cap (raw)"] = capacity["thru_cap_raw"].round(2)
        disp["Throughput util (raw)"] = (capacity["thru_util_raw"] * 100).round(1).astype(str) + "%"
        disp["Overall"] = capacity["overall_status"]
        st.dataframe(disp, width="stretch", height=480)
        st.caption(
            "**With buffer** = capacity multiplied by your safety + efficiency factors. "
            "**Raw** = theoretical maximum, no buffers applied. "
            "Battery row reports BATTERIES/day, not units/day."
        )

    # ----------------------------------------------------------------
    # 🔋 Battery throughput section (was a top-level tab before)
    # ----------------------------------------------------------------
    if batt_sku is not None and batt_type is not None:
        total_batt = int(batt_sku["total_batteries"].sum()) if not batt_sku.empty else 0
        if total_batt == 0:
            st.markdown("---")
            st.markdown("#### 🔋 Battery throughput")
            st.info(
                f"**No batteries needed for this plan at {inputs.get('location', '')}.** "
                "(Either the schedule has no BOSS units, or this facility doesn't build batteries.)"
            )
        else:
            st.markdown("---")
            with st.expander("🔋 Battery throughput detail", expanded=False):
                tab_battery_throughput(batt_sku, batt_type, capacity, inputs)


def tab_battery_throughput(batt_sku, batt_type, capacity, inputs):
    st.header("🔋 Battery demand & throughput")
    st.markdown(
        "**Can we build enough batteries to meet this plan?** "
        "Batteries are usually the bottleneck — this tab shows demand vs daily capacity."
    )

    total_batt = int(batt_sku["total_batteries"].sum())
    total_units = int(batt_sku["units"].sum())
    avg_per_unit = total_batt / total_units if total_units else 0
    daily_need = total_batt / inputs["days"] if inputs["days"] else 0

    cap_safe = capacity.loc["Battery Assembly", "thru_cap_safe"] if "Battery Assembly" in capacity.index else 0
    cap_raw = capacity.loc["Battery Assembly", "thru_cap_raw"] if "Battery Assembly" in capacity.index else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Batteries to build", f"{total_batt:,}",
        help="Total batteries needed across all units in this plan.",
    )
    c2.metric(
        "Avg per unit", f"{avg_per_unit:.2f}",
        help="Average batteries per BOSS unit (varies — BOSS25 = 1, BOSS220 = 3, BOSS400 = 5).",
    )
    c3.metric(
        "Need / day", f"{daily_need:.2f}",
        help=f"Daily build rate to finish in {inputs['days']} working days.",
    )
    c4.metric(
        "Have / day (with buffer)", f"{cap_safe:.1f}",
        delta=f"{(cap_safe - daily_need):+.1f} vs need",
        delta_color="normal" if cap_safe >= daily_need else "inverse",
        help="Daily capacity at the Battery Assembly cells, with your safety + efficiency buffers applied.",
    )

    if cap_safe < daily_need and cap_safe > 0:
        st.error(
            f"🚨 **Battery throughput is the bottleneck.** "
            f"You need ~{daily_need:.1f}/day but can only do ~{cap_safe:.1f}/day. "
            f"See **Recommendations** for fix options."
        )
    elif cap_safe == 0:
        st.warning(
            "⚠️ Battery Assembly is set to 0 cells/headcount for this facility. "
            "If you need batteries here, configure the station in the sidebar."
        )

    st.markdown("---")
    st.markdown("#### 📋 Battery demand by FG SKU")
    st.dataframe(batt_sku, width="stretch", height=360)

    st.markdown("#### 📊 Battery demand by type")
    cc1, cc2 = st.columns([2, 1])
    with cc1:
        fig = px.pie(batt_type, values="total_batteries", names="batt_type",
                     hole=0.45, title="Type mix")
        fig.update_layout(height=400)
        st.plotly_chart(fig, width="stretch")
    with cc2:
        st.dataframe(batt_type, width="stretch")

    # Comparison view
    # =============================================================
    # Plain-English summary
    # =============================================================
    days_needed_safe = (total_batt / cap_safe) if cap_safe else float("inf")
    days_needed_raw = (total_batt / cap_raw) if cap_raw else float("inf")

    st.markdown("#### 📅 How many days to finish all batteries?")
    summary = pd.DataFrame([
        {
            "Scenario": "Realistic (with buffer)",
            "Capacity / day": f"{cap_safe:.1f} batt/day" if cap_safe else "—",
            "Days needed": f"{days_needed_safe:.1f} days" if days_needed_safe != float("inf") else "—",
            "Verdict": "✅ Fits" if (days_needed_safe <= inputs["days"]) else "❌ Over target",
        },
        {
            "Scenario": "Best case (no buffer)",
            "Capacity / day": f"{cap_raw:.1f} batt/day" if cap_raw else "—",
            "Days needed": f"{days_needed_raw:.1f} days" if days_needed_raw != float("inf") else "—",
            "Verdict": "✅ Fits" if (days_needed_raw <= inputs["days"]) else "❌ Over target",
        },
    ])
    st.dataframe(summary, width="stretch", hide_index=True)
    st.caption(
        f"Your target window is **{inputs['days']} working days**. "
        "The realistic scenario uses your safety + efficiency factors; "
        "the best case assumes everything runs flawlessly."
    )


def tab_mitigation(capacity, batt_sku, units, inputs):
    st.header("🔧 Recommendations")
    st.markdown(
        "**Where to find slack and how to close gaps.** "
        "Use this tab when one or more stations are tight, near, or over capacity."
    )

    # Quick summary of where the issues are
    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    near_stations = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
    if over_stations:
        st.error(f"🚨 Over capacity: **{', '.join(over_stations)}**. Address first.")
    if near_stations:
        st.warning(f"⚠️ Near capacity: **{', '.join(near_stations)}**. Monitor.")
    if not over_stations and not near_stations:
        st.success("✅ No stations are over or near capacity — no urgent action needed.")

    st.markdown("---")
    st.markdown("#### 👥 People you could move (rotation candidates)")
    st.caption(
        "Stations sorted from most idle to most loaded. A ✅ / 🟡 **Rotation "
        "potential** tag means people there are free to cross-train and help "
        "bottleneck stations; 🟠 / 🔴 rows have little or none to spare."
    )

    idle_rows = []
    for disp, row in capacity.iterrows():
        if row.get("station_missing", False):
            continue
        util = row["labor_util_safe"]
        idle_pct = max(0, 1 - util)
        idle_hc_eq = idle_pct * row["HC"]
        if util > 1:
            tag = "🔴 Bottleneck"
            note = "Needs help — add capacity here"
        elif idle_pct > 0.5:
            tag = "✅ Lots of slack"
            note = f"~{idle_hc_eq:.1f} people equivalent free"
        elif idle_pct > 0.3:
            tag = "🟡 Some slack"
            note = f"{idle_pct*100:.0f}% of capacity is idle"
        else:
            tag = "🟠 Busy"
            note = "Limited rotation potential"
        idle_rows.append({
            "Station": disp,
            "People": int(row["HC"]),
            "Used %": int(round(util * 100)),
            "Idle %": int(round(idle_pct * 100)),
            "Rotation potential": tag,
            "Notes": note,
            "_sort": idle_pct,
        })
    # idle_rows is empty only when every station has HC=0 (an all-unstaffed
    # facility config). Building a DataFrame from [] gives zero columns, so the
    # .sort_values("_sort") below would KeyError — guard it like the other
    # empty-frame fixes (Build Time drill-down).
    if not idle_rows:
        st.info(
            "No staffed stations to rotate — every station shows 0 people. "
            "Add headcount in the sidebar **👥 Station headcount** editor to see "
            "rotation candidates."
        )
    else:
        idle_df = pd.DataFrame(idle_rows).sort_values("_sort", ascending=False).drop(columns="_sort")
        st.dataframe(
            idle_df, width="stretch", height=420, hide_index=True,
            column_config={
                "Used %": st.column_config.NumberColumn("Used %", format="%d%%"),
                "Idle %": st.column_config.NumberColumn("Idle %", format="%d%%"),
            },
        )

    # =============================================================
    # Fix playbook — scenario-aware (works for any bottleneck)
    # =============================================================
    st.markdown("---")
    st.markdown("#### 🛠️ Standard fix options")
    st.caption(
        "Each option closes a capacity gap differently. Pick the right one based on "
        "whether you need a permanent vs. temporary fix and how much capex you can spend."
    )

    bottleneck = (over_stations[0] if over_stations
                  else (near_stations[0] if near_stations else "the bottleneck station"))

    playbook = pd.DataFrame([
        {
            "#": 1,
            "Option": f"Cross-train and rotate people to {bottleneck}",
            "What it does": "Move people from idle stations to where they're needed.",
            "Effort": "Low — uses the rotation table above",
            "Speed": "Days",
            "When to use": "🟢 Best first move — no capex, immediate",
        },
        {
            "#": 2,
            "Option": "Defer some units to next period",
            "What it does": "Pull the lowest-priority units out of this schedule.",
            "Effort": "Low — schedule change only",
            "Speed": "Immediate",
            "When to use": "🟢 When customer lead time can absorb it",
        },
        {
            "#": 3,
            "Option": "Overtime at the bottleneck",
            "What it does": f"Extra hours per day on {bottleneck}.",
            "Effort": "Low — staffing-only change",
            "Speed": "Days",
            "When to use": "🟡 Temporary fix; budget for OT premium",
        },
        {
            "#": 4,
            "Option": "Add Saturday shifts",
            "What it does": "Extra working days in the month.",
            "Effort": "Medium — touches whole line",
            "Speed": "Weeks",
            "When to use": "🟡 When OT isn't enough; weekend premium",
        },
        {
            "#": 5,
            "Option": "Hire / add a cell at the bottleneck",
            "What it does": "Permanent capacity increase.",
            "Effort": "High — hiring, training, possibly capex",
            "Speed": "Months",
            "When to use": "🟢 If demand is sustained beyond this period",
        },
    ]).set_index("#")
    st.dataframe(playbook, width="stretch", height=260)

    st.info(
        "**Tip for planners:** Combine a short-term move (rotation or overtime) "
        "with a structural decision (hiring or scheduling change) so you handle "
        "this month *and* set up the next one for success."
    )


# =============================================================
# 📤 Export & Excel link — whole database / current analysis as .xlsx
# =============================================================
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DATABASE_EXPORT_FILES = tuple(sorted({f for _s, f, _d in EXCEL_DATABASE_TABLES}))


def _database_export_signature(include_rows: bool) -> tuple:
    """Cache key for the database workbook: every data file's mtime plus the
    loader schema version. A catalog / scenario save rewrites the local file,
    so the next visit rebuilds instead of serving a stale workbook."""
    return (
        tuple(_csv_mtime(f) for f in _DATABASE_EXPORT_FILES),
        bool(include_rows),
        _LOADER_SCHEMA_VERSION,
    )


@st.cache_data(show_spinner=False)
def _build_database_workbook_cached(sig: tuple, include_rows: bool) -> bytes:
    # ``sig`` is only here to key the cache (no leading underscore on purpose —
    # Streamlit skips hashing underscore-prefixed params).
    return build_excel_workbook(
        database_excel_tables(include_saved_schedule_rows=include_rows),
        readme=database_excel_readme(include_saved_schedule_rows=include_rows),
    )


def _session_settings_rows(inputs: dict | None) -> list[dict]:
    """The sidebar's scalar inputs as Setting / Value rows for the analysis
    export. Frames (crew table) and widgets (the upload) are skipped; the
    plan-month tuple is already mirrored by ``plan_month_label``."""
    rows = []
    for key, val in (inputs or {}).items():
        if isinstance(val, (list, tuple, set)):
            if key == "plan_month":
                continue
            val = "; ".join(str(v) for v in val)
        elif not isinstance(val, (str, int, float, bool)) or val is None:
            continue
        rows.append({"Setting": key, "Value": val})
    return rows


def _render_export_view(schedule_df, units, capacity, inputs, location: str) -> None:
    """📤 Export & Excel link sub-view of 📁 Data & Setup.

    Three ways out: the whole database as one workbook, this session's
    analysis as a workbook, and read-only live links (raw GitHub URLs plus a
    Power Query script) so Excel can refresh the catalogs itself.
    """
    st.subheader("📤 Export & Excel link")
    st.caption(
        "Everything the tool knows, in Excel. Download the whole database as one "
        "workbook, download this session's analysis, or point a sheet at the live "
        "data files so it refreshes on its own."
    )

    # ---- 1) Whole database ---------------------------------------------------
    st.markdown("#### 1 · Whole database as one workbook")
    st.markdown(
        "One sheet per data file — the catalogs exactly as stored, plus the crew, "
        "settings, scenarios, executive scenarios, saved schedules and process-flow "
        "JSON flattened into plain tables. A **README** sheet says what each sheet "
        "is and which file it came from."
    )
    include_rows = st.checkbox(
        "Include every row of every saved schedule",
        value=False,
        key="export_include_saved_rows",
        help=(
            "Adds the big one: thousands of rows × every column the planners' "
            "CSVs carried. Slower to build and a much larger file. The *index* "
            "of saved schedules is always included."
        ),
    )
    sheet_rows = [
        {"Sheet": s, "Source file": f"data/{f}", "What it holds": d}
        for s, f, d in EXCEL_DATABASE_TABLES
        if include_rows or s != EXCEL_SAVED_ROWS_SHEET
    ]
    st.dataframe(pd.DataFrame(sheet_rows), width="stretch", hide_index=True)
    try:
        with st.spinner("Building workbook…"):
            db_bytes = _build_database_workbook_cached(
                _database_export_signature(include_rows), include_rows,
            )
    except Exception as e:  # noqa: BLE001 — never take the whole tab down
        st.error(f"❌ Couldn't build the database workbook: {e}")
        db_bytes = b""
    st.download_button(
        "📥 Download database workbook (.xlsx)",
        db_bytes,
        file_name=f"labor_model_database_{today_local_str()}.xlsx",
        mime=_XLSX_MIME,
        width="stretch",
        disabled=not db_bytes,
    )
    if db_bytes:
        st.caption(
            f"{len(db_bytes) / 1024:,.0f} KB · built from this app's data files, "
            "including every save made here (other users' saves land after "
            "the ~1 minute rebuild)."
        )

    # ---- 2) This session's analysis -----------------------------------------
    st.markdown("#### 2 · This session's analysis")
    if units is None or units.empty:
        st.info(
            "Load a schedule in the sidebar to export the per-unit labor and "
            "capacity-by-station tables for it."
        )
    else:
        crew_cfg = (inputs or {}).get("crew_config")
        tables = {
            "Schedule": schedule_df if schedule_df is not None else pd.DataFrame(),
            "Units (labor per unit)": units,
            "Capacity by station": capacity if capacity is not None else pd.DataFrame(),
            "Crew (this session)": crew_cfg if isinstance(crew_cfg, pd.DataFrame) else pd.DataFrame(),
            "Settings": pd.DataFrame(_session_settings_rows(inputs), columns=["Setting", "Value"]),
        }
        try:
            analysis_bytes = build_excel_workbook(tables)
        except Exception as e:  # noqa: BLE001
            st.error(f"❌ Couldn't build the analysis workbook: {e}")
            analysis_bytes = b""
        st.download_button(
            "📥 Download analysis workbook (.xlsx)",
            analysis_bytes,
            file_name=(
                f"labor_model_analysis_{location.lower()}_{today_local_str()}.xlsx"
            ),
            mime=_XLSX_MIME,
            width="stretch",
            disabled=not analysis_bytes,
            help=(
                "Schedule as loaded (after suffix cleanup / auto-spread), one row "
                "per unit with its per-station labor, the capacity table behind the "
                "Capacity tab, and the crew + sidebar settings that produced them."
            ),
        )

    # ---- 3) Live link ---------------------------------------------------------
    st.markdown("#### 3 · Link Excel to the live data")
    st.markdown(
        "The catalogs are plain CSV files on GitHub, so Excel can read them "
        "directly: **Data → Get Data → From Web**, paste the URL, **Load**. From "
        "then on **Data → Refresh All** pulls whatever the team has saved since. "
        "Read-only — edits still go through the catalog editors here."
    )
    link_rows = [
        {"Table": s, "File": f"data/{f}", "URL": raw_github_url(f)}
        for s, f, _d in EXCEL_DATABASE_TABLES
        if f in EXCEL_CSV_LINKABLE_FILES
    ]
    st.dataframe(
        pd.DataFrame(link_rows),
        width="stretch", hide_index=True,
        column_config={"URL": st.column_config.LinkColumn("URL")},
    )
    pick = st.selectbox(
        "Power Query script for…", [r["Table"] for r in link_rows],
        key="export_pq_pick",
    )
    pick_url = next((r["URL"] for r in link_rows if r["Table"] == pick), "")
    st.code(power_query_m_for_csv(pick_url), language="text")
    with st.expander("Step by step", expanded=False):
        st.markdown(
            """
**Quick way (one table):** Excel → **Data** → **Get Data** → **From Other
Sources** → **From Web** → paste the URL above → **OK** → **Load**.

**Script way (exact types, repeatable):** **Data** → **Get Data** → **Launch
Power Query Editor** → **Home** → **New Source → Blank Query** → **Advanced
Editor** → replace everything with the script above → **Done** → rename the
query → **Close & Load**.

**Refresh:** **Data** → **Refresh All** (or right-click the table →
**Refresh**). Each refresh re-reads the file as it is on GitHub right then.

**Notes**
- The JSON files (crew, settings, scenarios, executive scenarios, saved
  schedules, process flow) need a few transform steps in Power Query, so they
  aren't one-click links — the database workbook above already has them
  flattened.
- If the repo is ever made private, these URLs stop working anonymously; the
  query then needs a token (`Web.Contents` with an `Authorization` header).
  Ask the maintainer.
            """
        )


# =============================================================
# 📥 Import from Excel — catalog sheets → preview → SHA-locked cell merge
# =============================================================
@st.cache_data(show_spinner=False)
def _parse_catalog_workbook_cached(raw_bytes: bytes, filename: str, schema_ver: int):
    """Read the uploaded workbook once per upload (keyed by its bytes) so a
    widget nudge doesn't re-open the file. ``schema_ver`` is in the key on
    purpose (no leading underscore — Streamlit skips hashing those): the sheet
    is reconciled against the live CSV header, which changes with the schema."""
    return read_catalog_workbook(raw_bytes, filename)


def _render_import_view(machine_df, acc_df) -> None:
    """📥 Import from Excel sub-view of 📁 Data & Setup.

    The way back from the export: a planner edits labor minutes (or adds a
    SKU row) on the ``Machine catalog`` / ``Accessory catalog`` sheet of the
    database workbook and uploads it. Each catalog sheet found gets a preview
    — the cells that would change, the new SKUs, the catalog SKUs the sheet
    doesn't mention — and its own 💾 Save, which pushes those cells through
    :func:`_save_catalog_csv` (fetch fresh + SHA-locked merge), exactly like
    the in-app editors. Nothing is written until save is clicked.
    """
    st.subheader("📥 Import from Excel")
    for _p in (_provenance_line("machine"), _provenance_line("accessory")):
        if _p:
            st.caption(_p)
    st.caption(
        "Bring labor standards in from Excel. Upload the IE's cycle-time workbook "
        "**as it is** — its own headers (`SKU ID`, `Warehouse (PICK)`, "
        "`Final Assy (FN-ASSY)` …) are recognised — or the database workbook from "
        "**📤 Export & Excel link** after editing it. You'll see exactly what would "
        "change before anything is written; the save merges only those cells on top "
        "of whatever the team has committed since. Routing, descriptions and the "
        "`XXX` estimation placeholders live in the app and are never touched by an "
        "import."
    )

    # Who owns the labor minutes. With Excel as the source, a minute edited in
    # the app is still saved and used at once — but recorded as *pending* and
    # protected from being reverted by the next import until the workbook
    # carries it. A button rather than a checkbox bound to the persisted flag,
    # so a stale widget in one session can never re-save over another user's
    # change.
    _excel_now = _excel_source()
    _lc1, _lc2 = st.columns([3, 2])
    with _lc1:
        st.markdown(
            "**Source of truth for labor minutes:** "
            + ("📗 **Excel.** A minute changed in the app is used right away and "
               "listed as ⏳ pending until the workbook is updated and re-imported."
               if _excel_now else
               "🔓 **either** — minutes edited in the app are simply saved; an "
               "import overwrites them.")
        )
    with _lc2:
        _src_label = (
            "🔓 Stop treating Excel as the source" if _excel_now
            else "📗 Make Excel the source of truth"
        )
        if st.button(
            _src_label, key="catalog_excel_source_btn", width="stretch",
            help=(
                "Team-wide, saved to GitHub. With Excel as the source, each "
                "in-app minute edit is tracked as pending and an import won't "
                "revert it while the workbook still has the old number. Routing, "
                "Description, SKU renames and the add-SKU / placeholder forms are "
                "the app's own either way."
            ),
        ):
            _tok = _github_token()
            if not _tok:
                st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            else:
                try:
                    set_excel_source_to_github(not _excel_now, _tok)
                    _load_catalog_source_cached.clear()
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"❌ Couldn't save the setting: {exc}")
    if _excel_now:
        for _k in ("machine", "accessory"):
            _render_pending_summary(_k, _pending_for(_k))

    with st.expander("What the import does — and doesn't", expanded=False):
        st.markdown(
            """
- **Which sheets:** `Machine catalog` and `Accessory catalog` (the exported
  workbook's names), or `Machine` / `Acc` and a few other spellings. A sheet
  that isn't named is recognised by its header row. **The IE workbook's own
  headers work** — `SKU ID`, `Warehouse (PICK)`, `Wire Assy (PREP-WH)`,
  `Final Assy (FN-ASSY)`, `Acc KIT (PREP-ACC)` and so on map onto the
  catalog's columns, and the preview lists every mapping it made. Columns like
  `Unit`, `Total`, `Lead`, `Flag` are ignored. Other sheets are ignored.
- **Matching:** rows match catalog rows by **SKU** (case doesn't matter).
  A SKU that isn't in the catalog becomes a **new row**.
- **Never deletes:** catalog SKUs that aren't on the sheet are left exactly
  as they are. Columns the sheet doesn't have are left alone on every row;
  extra columns are ignored. **A blank cell never changes anything** — type
  `0` to zero a value.
- **Validated like the editor:** labor cells must be numbers ≥ 0; routing
  cells (`Final Station HND` …) must be one of the dropdown's options (case is
  corrected for you); a SKU can't start with `=`, `+`, `-` or `@`; a SKU may
  appear once. Any of those blocks the save until the file is fixed.
- **Save:** fetches the latest file from GitHub, applies only the previewed
  cells (and appends the new rows), and pushes with the file's SHA as a lock —
  retrying once on a conflict. A colleague's edit of another cell in the
  meantime survives. `Last Modified` is stamped on every changed row.
            """
        )

    uploaded = st.file_uploader(
        "Workbook (.xlsx / .xlsm) — the exported database workbook, or a single catalog sheet",
        type=["xlsx", "xlsm"],
        key="catalog_import_upload",
    )
    if uploaded is None:
        return

    sig = _upload_sig(uploaded)
    try:
        book = _parse_catalog_workbook_cached(
            uploaded.getvalue(), getattr(uploaded, "name", ""), _LOADER_SCHEMA_VERSION,
        )
    except CatalogImportError as exc:
        st.error(f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — never take the whole tab down
        st.error(f"❌ Couldn't read **{getattr(uploaded, 'name', 'the file')}**: {exc}")
        return

    if not book.sheets:
        st.error(
            f"❌ No catalog sheet found in **{getattr(uploaded, 'name', 'the file')}**. "
            "Expected a sheet named `Machine catalog` or `Accessory catalog` (as the "
            "export names them), or a single sheet whose header row has `SKU` plus "
            "that catalog's labor columns."
        )
        for name, why in book.skipped:
            st.caption(f"• `{name}` — {why}")
        return
    if book.skipped:
        st.caption(
            "Ignored sheets: " + ", ".join(f"`{n}`" for n, _why in book.skipped)
        )

    for key, sheet in book.sheets.items():
        source_df = machine_df if key == "machine" else acc_df
        _render_catalog_import_section(
            sheet, source_df, sig, upload_name=getattr(uploaded, "name", ""),
        )


def _render_catalog_import_section(sheet, source_df, upload_sig: str,
                                   upload_name: str = "") -> None:
    """Preview + save for one catalog sheet of an uploaded workbook."""
    spec = sheet.spec
    icon = "🏭" if spec.key == "machine" else "🔌"
    st.markdown("---")
    st.markdown(f"#### {icon} {spec.sheet} ← sheet `{sheet.sheet_name}`")
    _render_stale_data_banner(spec.file_path)

    if sheet.matched_by == "header":
        st.caption(
            f"Recognised by its header row (the sheet isn't called `{spec.sheet}`)."
        )
    if sheet.aliased:
        # The IE workbook's spellings, and what each one landed on. Shown
        # rather than silently applied: a planner about to overwrite standards
        # should see that "Final Assy (FN-ASSY)" is FN_Assy, not guess.
        _pairs = list(sheet.aliased.items())
        st.caption(
            "Recognised the workbook's own headers: "
            + ", ".join(f"`{src}` → `{dst}`" for src, dst in _pairs[:12])
            + (" …" if len(_pairs) > 12 else "")
        )
    if sheet.extra_columns:
        st.caption(
            f"Ignored columns (not in `{spec.file_path}`): "
            + ", ".join(f"`{c}`" for c in sheet.extra_columns[:12])
            + (" …" if len(sheet.extra_columns) > 12 else "")
        )
    if sheet.missing_columns:
        st.caption(
            "Columns not on the sheet — left as they are on every row: "
            + ", ".join(f"`{c}`" for c in sheet.missing_columns[:12])
            + (" …" if len(sheet.missing_columns) > 12 else "")
        )

    try:
        diff = diff_catalog_sheet(sheet, source_df)
    except Exception as exc:  # noqa: BLE001
        st.error(f"❌ Couldn't compare the sheet with the catalog: {exc}")
        return

    # Temporary in-app minute edits: decide per cell whether this sheet has
    # caught up, still has the old number (keep the planner's value), or was
    # changed to something else (Excel wins). Done before the preview so what
    # the planner sees is what the save will write.
    pending_report = None
    _pending = _pending_for(spec.key) if _excel_source() else {}
    if _pending:
        _revert = st.checkbox(
            "Revert my temporary in-app changes to the workbook's values",
            key=f"catalog_import_revert_{spec.key}_{upload_sig}", value=False,
            help=(
                "Unticked (default): where the sheet still has the number you "
                "replaced in the app, your value is kept and stays pending. "
                "Ticked: the sheet's value is applied and the pending mark cleared."
            ),
        )
        diff, pending_report = reconcile_pending(diff, sheet, _pending, revert_to_excel=_revert)
        _rp = pending_report
        if _rp.caught_up:
            st.success(
                f"✅ The workbook now carries {len(_rp.caught_up)} of your temporary "
                "change(s) — they stop being pending: "
                + ", ".join(f"`{s}` {c} = {v:g}" for s, c, v, _ in _rp.caught_up[:8])
                + (" …" if len(_rp.caught_up) > 8 else "")
            )
        if _rp.still_pending:
            st.warning(
                f"⏳ {len(_rp.still_pending)} temporary change(s) are still not in the "
                "workbook — **your values are kept** and stay pending: "
                + ", ".join(
                    f"`{s}` {c} (app {a:g}, Excel {'blank' if e is None else f'{e:g}'})"
                    for s, c, a, e in _rp.still_pending[:8]
                )
                + (" …" if len(_rp.still_pending) > 8 else "")
                + " Tick the box above to take the workbook's values instead."
            )
        if _rp.reverted:
            st.info(
                f"↩️ Reverting {len(_rp.reverted)} temporary change(s) to the workbook's "
                "values: " + ", ".join(f"`{s}` {c} → {e:g}" for s, c, _a, e in _rp.reverted[:8])
                + (" …" if len(_rp.reverted) > 8 else "")
            )
        if _rp.excel_changed:
            st.warning(
                f"⚠️ For {len(_rp.excel_changed)} cell(s) the workbook has a *different* "
                "value than your temporary change — Excel is the source, so its value "
                "will be applied: "
                + ", ".join(f"`{s}` {c} (app {a:g} → Excel {e:g})" for s, c, a, e in _rp.excel_changed[:8])
                + (" …" if len(_rp.excel_changed) > 8 else "")
            )
        if _rp.not_on_sheet:
            st.caption(
                f"{len(_rp.not_on_sheet)} temporary change(s) aren't on this sheet "
                "(SKU missing or cell blank) — left as they are."
            )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows on sheet", diff.n_rows)
    c2.metric("Cells to change", len(diff.cells))
    c3.metric("New SKUs", len(diff.new_rows))
    c4.metric(
        "Catalog SKUs not on sheet", len(diff.absent_skus),
        help="Left untouched — an import never deletes a row.",
    )

    if diff.duplicate_skus:
        st.error(
            "❌ Listed more than once on the sheet: "
            + ", ".join(f"`{s}`" for s in diff.duplicate_skus[:12])
            + (" …" if len(diff.duplicate_skus) > 12 else "")
            + ". Keep one row per SKU and re-upload."
        )
    if diff.problems:
        st.error(
            f"❌ {len(diff.problems)} cell(s) didn't validate. Fix them in Excel "
            "and re-upload — nothing from this sheet is saved until the file is clean."
        )
        st.dataframe(diff.problems_frame(), width="stretch", hide_index=True)

    if diff.cells:
        with st.expander(
            f"📝 {len(diff.cells)} cell change(s) across {len(diff.changed_skus)} SKU(s)",
            expanded=True,
        ):
            st.dataframe(diff.cells_frame(), width="stretch", hide_index=True)
    if diff.new_rows:
        with st.expander(f"➕ {len(diff.new_rows)} new SKU(s)", expanded=True):
            st.dataframe(diff.new_rows_frame(), width="stretch", hide_index=True)
            _seed_tbl = _routing_seed_preview(spec.file_path, diff.new_rows)
            if _seed_tbl is not None:
                st.caption(
                    "Blank labor cells are saved blank (the app reads them as 0). "
                    "Per-facility routing: a value on the sheet is kept; a blank "
                    "routing cell is seeded from the most similar SKU already in "
                    "the catalog (same family and class), so the new SKU inherits "
                    "each plant's custom routing — `defaults` means every station "
                    "keeps its own team:"
                )
                st.dataframe(_seed_tbl, width="stretch", hide_index=True)
            else:
                st.caption("Blank labor cells are saved blank (the app reads them as 0).")
    if diff.absent_skus:
        with st.expander(
            f"⏸️ {len(diff.absent_skus)} catalog SKU(s) not on the sheet — left untouched",
            expanded=False,
        ):
            st.write(
                ", ".join(f"`{s}`" for s in diff.absent_skus[:300])
                + (" …" if len(diff.absent_skus) > 300 else "")
            )

    done_key = f"catalog_import_done::{upload_sig}::{spec.key}"
    done = st.session_state.get(done_key)
    if done:
        st.success(
            f"✅ Already applied from this upload (commit `{done}`). The preview "
            "above still compares against the catalog as loaded *before* that "
            "save — export a fresh workbook to make further changes."
        )
        return
    if diff.is_empty:
        st.info("Nothing to change — this sheet matches the catalog. (Blank cells never change anything.)")
        return

    bits = []
    if diff.cells:
        bits.append(f"{len(diff.cells)} cell change(s)")
    if diff.new_rows:
        bits.append(f"{len(diff.new_rows)} new SKU(s)")
    label = f"💾 Save {' + '.join(bits)} to `{spec.file_path}` on GitHub"
    if diff.blocked:
        st.button(label, key=f"catalog_import_save_{spec.key}", width="stretch",
                  disabled=True, help="Fix the problems listed above and re-upload.")
        return

    numeric_cols, text_cols = catalog_editable_columns(spec, sheet.live_columns)
    if st.button(label, key=f"catalog_import_save_{spec.key}", width="stretch",
                 type="primary"):
        result = _save_catalog_csv(
            edited=None, source_df=source_df, editable_cols=numeric_cols,
            file_path=spec.file_path, label=spec.label, text_cols=text_cols,
            diffs=diff.cell_tuples(), additions=diff.new_rows, via="Excel import",
        )
        if result:
            st.session_state[done_key] = result
            # Provenance + pending bookkeeping. Best effort — the catalog save
            # already landed, so a hiccup here is reported, not fatal.
            try:
                _tok = _github_token()
                if _tok:
                    record_import_to_github(
                        spec.key, file=upload_name or "workbook",
                        sheet=sheet.sheet_name, cells=len(diff.cells),
                        new_skus=len(diff.new_rows), token=_tok,
                    )
                    if pending_report is not None and pending_report.to_clear:
                        clear_pending_to_github(
                            spec.key, pending_report.to_clear, _tok,
                            reason="Excel caught up / Excel's value taken",
                        )
                    _load_catalog_source_cached.clear()
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Saved, but couldn't record where the standards came from: {exc}")


def _remember_data_setup_view() -> None:
    """Mirror the Data & Setup view picker into a non-widget key so the choice
    survives a trip to another tab (a widget that isn't rendered loses its
    widget-backed state at the end of the run)."""
    st.session_state["_data_setup_view_last"] = st.session_state.get("_data_setup_view")


def _jump_to(tab: "str | None", view: "str | None" = None, *, open_crew: bool = False) -> None:
    """Take the planner to a control from a main-panel button: a tab (and a
    Data & Setup ``view``), and/or the sidebar's 👥 Station headcount expander.

    The widgets themselves were built earlier in this run (the strip and the
    tab bodies come after main-panel buttons only on the NEXT run, the sidebar
    before them on every run), so this leaves one-shot seeds the widgets
    consume right before they are built — ``ACTIVE_TAB_SEED_KEY`` in
    :func:`_render_tab_strip`, ``_data_setup_view_last`` (and a popped
    ``_data_setup_view``, so the radio initialises from the mirror) in
    :func:`tab_data_setup`, ``CREW_EXPANDER_SEED_KEY`` in the sidebar — and
    reruns at once. A main-panel rerun is safe: the sidebar is already built.
    """
    if tab:
        st.session_state[ACTIVE_TAB_SEED_KEY] = tab
        st.session_state[ACTIVE_TAB_MIRROR_KEY] = tab
    if view:
        st.session_state["_data_setup_view_last"] = view
        st.session_state.pop("_data_setup_view", None)
    if open_crew:
        st.session_state[CREW_EXPANDER_SEED_KEY] = True
    _track_and_flush("guide_jump", tab=tab or "", view=view or "", crew=bool(open_crew))
    st.rerun()


def tab_data_setup(
    machine_df, acc_df, schedule_df, item_master_df, item_packages_df,
    units=None, schedule_df_full=None, location: str = "Henderson",
    capacity=None, inputs=None,
):
    """Consolidated data + admin tab.

    Replaces three previous top-level tabs (Update Labor, Data Quality, Source
    Data) with a single tab containing radio sub-views. Sub-views in order:

      📋 Schedule              — read-only schedule view
      🗓 Sequence              — manual reshape: edit PRODUCTION DAY inline, bulk-move
                                 units between weeks
      🧹 Customer suffix cleanup — strip customer-decal tails (HRC / ES / UR …) from the
                                 loaded schedule's SKUs
      🏭 Machine catalog       — editable catalog + add-new-SKU form
      🔌 Accessory catalog     — editable catalog + add-new + XXX placeholder forms
      🔩 Items                 — unified items table editor
      📦 Item packages         — package definitions editor
      🔬 Reconciliation & Apply — items-vs-aggregate diff + write-back
      🔍 Data Quality          — validation report
      📤 Export & Excel link   — whole database / this session as .xlsx + live links
      📥 Import from Excel     — catalog sheets from a workbook → preview → SHA-locked save
    """
    st.header("📁 Data & Setup")
    st.caption(
        "All data inputs in one place. Switch views below to browse the schedule, "
        "edit the catalogs, manage items / packages, reconcile, or run data quality checks."
    )

    _views = [
        "📋 Schedule",
        "🗓 Sequence",
        "🧹 Customer suffix cleanup",
        "🏭 Machine catalog",
        "🔌 Accessory catalog",
        "🔩 Items",
        "📦 Item packages",
        "🔬 Reconciliation & Apply",
        "🔍 Data Quality",
        "📤 Export & Excel link",
        "📥 Import from Excel",
    ]
    # Only the tab in front is built (see _render_tab_strip), so this radio is
    # torn down whenever the planner visits another tab and would snap back to
    # "Schedule". The on_change mirror keeps the last choice in a plain
    # session key and feeds it back as the initial index.
    _remembered = st.session_state.get("_data_setup_view_last")
    sub = st.radio(
        "View",
        _views,
        index=_views.index(_remembered) if _remembered in _views else 0,
        horizontal=True,
        label_visibility="collapsed",
        key="_data_setup_view",
        on_change=_remember_data_setup_view,
    )

    # Used-in-schedule maps (shared by some sub-views)
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    if sub == "📋 Schedule":
        st.subheader("📋 Schedule")
        st.caption("Read-only view of the schedule currently driving the analysis tabs.")
        if schedule_df.empty:
            st.info("No schedule rows. Upload a CSV in the sidebar or switch to manual entry.")
        else:
            st.dataframe(schedule_df, width="stretch", height=600)

    elif sub == "🗓 Sequence":
        # Sequence reshape needs the FULL schedule (not the time-window
        # subset) so the planner can move units across all weeks of the
        # month, not just the currently-filtered slice.
        _inputs = inputs if isinstance(inputs, dict) else {}
        _render_sequencer_view(
            schedule_df_full if schedule_df_full is not None else schedule_df,
            plan_month=_inputs.get("plan_month"),
            plan_month_label=_inputs.get("plan_month_label", ""),
            manual_mode=_inputs.get("schedule_mode") == SCHEDULE_MODE_MANUAL,
        )

    elif sub == "🧹 Customer suffix cleanup":
        # Same pattern as Sequence: cleanup operates on the full schedule.
        _inputs = inputs if isinstance(inputs, dict) else {}
        _render_suffix_cleanup_view(
            schedule_df_full if schedule_df_full is not None else schedule_df,
            machine_df, acc_df,
            manual_mode=_inputs.get("schedule_mode") == SCHEDULE_MODE_MANUAL,
        )

    elif sub == "🏭 Machine catalog":
        tab_floor_verification_machine(machine_df, schedule_df, used_fg, location=location)

    elif sub == "🔌 Accessory catalog":
        tab_floor_verification_accessory(acc_df, schedule_df, used_acc, machine_df)

    elif sub == "🔩 Items":
        _render_item_master_view(item_master_df)

    elif sub == "📦 Item packages":
        _render_item_packages_view(item_packages_df, item_master_df)

    elif sub == "🔬 Reconciliation & Apply":
        _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc)

    elif sub == "🔍 Data Quality":
        tab_data_validation(machine_df, acc_df, units=units)

    elif sub == "📤 Export & Excel link":
        _render_export_view(
            schedule_df_full if schedule_df_full is not None else schedule_df,
            units, capacity, inputs, location,
        )

    elif sub == "📥 Import from Excel":
        _render_import_view(machine_df, acc_df)


def tab_floor_verification_machine(machine_df, schedule_df, used_fg, location: str = "Henderson"):
    """The Machine-catalog editor — extracted from the old tab_floor_verification
    so it can be embedded as a sub-view of Data & Setup.

    Per-facility routing: the editor surfaces ONLY the 3 routing columns
    matching the sidebar's selected facility (e.g. ``Final Station CYP`` at
    Cypress). The other 6 columns stay in the CSV untouched. The dropdown's
    display label drops the suffix so the editor reads "Final Station" /
    "AccKIT Station" / "PDI Station" — the facility context comes from the
    sidebar.
    """
    from core.constants import FACILITY_CODE
    fac_code = FACILITY_CODE.get(location, "HND")
    fs_col = f"Final Station {fac_code}"
    ak_col = f"AccKIT Station {fac_code}"
    pd_col = f"PDI Station {fac_code}"
    ws_col = f"Wire Station {fac_code}"
    uc_col = f"Undercarriage Station {fac_code}"

    st.subheader("🏭 Machine catalog")
    _render_stale_data_banner("data/machine_clean.csv")
    excel_src = _excel_source()
    pending = _pending_for("machine") if excel_src else {}
    if excel_src:
        st.info(
            "📗 **Excel is the source of truth for labor minutes.** You can still "
            "change a minute here — it's saved and used right away — but it's "
            "listed as ⏳ **pending** until the workbook is updated and re-imported "
            "(**📥 Import from Excel**). Routing, Description, SKU renames and the "
            "add-SKU form are the app's own."
        )
    _prov = _provenance_line("machine")
    if _prov:
        st.caption(_prov)
    _render_pending_summary("machine", pending)
    st.caption(
        f"Edit labor times in the table below — **all labor columns are "
        f"person-minutes per unit** (not hours; `Bat` is a battery count). SKUs "
        f"used in the current schedule are highlighted. Click **💾 Save** to push "
        f"changes to GitHub — all users see new values after redeploy (~1 min). · "
        f"Routing columns shown are for **{location}** ({fac_code}); switch "
        f"facility in the sidebar to edit another plant's routing."
    )

    editable_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy", "PDI", "QC", "Ship", "ETO", "Bat"]
    display_df = machine_df.reset_index(drop=True).copy()
    # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
    if "FN_Assy_old" in display_df.columns and "FN_Assy" not in display_df.columns:
        display_df = display_df.rename(columns={"FN_Assy_old": "FN_Assy"})
    # Backward-compat: default the facility's routing columns for a stale
    # cache (a worker process may still be on the pre-migration frame).
    if fs_col not in display_df.columns:
        display_df[fs_col] = "Final"
    if ak_col not in display_df.columns:
        display_df[ak_col] = "AccKIT"
    if pd_col not in display_df.columns:
        display_df[pd_col] = "PDI"
    if ws_col not in display_df.columns:
        display_df[ws_col] = "Wire"
    if uc_col not in display_df.columns:
        display_df[uc_col] = "Undercarriage"
    # Drop the projected legacy routing columns from the editor view —
    # they're compute helpers, not the source of truth. We also drop the
    # OTHER facilities' columns so the editor only shows ONE set of
    # routing dropdowns matching the sidebar location.
    _drop_cols = ["Final Station", "AccKIT Station", "PDI Station", "Wire Station",
                  "Undercarriage Station"]
    for _code in ("HND", "SPB", "CYP"):
        if _code == fac_code:
            continue
        _drop_cols.extend([f"Final Station {_code}", f"AccKIT Station {_code}",
                           f"PDI Station {_code}", f"Wire Station {_code}",
                           f"Undercarriage Station {_code}"])
    display_df = display_df.drop(columns=[c for c in _drop_cols if c in display_df.columns])
    if "Last Modified" not in display_df.columns:
        display_df["Last Modified"] = ""
    display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_fg.get(s, 0)))
    display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

    only_used = st.checkbox(
        "Show only SKUs used in current schedule", value=False, key="fv_m_only_used"
    )
    if only_used:
        display_df = display_df[display_df["In schedule"]].copy()
    display_df = display_df.sort_values(
        by=["In schedule", "Used (qty)"], ascending=[False, False]
    ).reset_index(drop=True)

    col_cfg = {
        # NOTE on pinned columns: Streamlit's frontend silently disables
        # pinning when the SUM of pinned column widths exceeds ~60% of the
        # container width. Keep these compact so the freeze stays active on
        # normal-sized browser windows. Width="large" on Description was
        # enough to trip the threshold and quietly turn pinning off.
        "SKU": st.column_config.TextColumn(
            "SKU", width="small", pinned=True,
            help=("Editable — rename to fix a typo. ⚠️ Schedule rows using the "
                  "OLD SKU won't auto-update; re-point or re-upload them."),
        ),
        "Description": st.column_config.TextColumn(
            "Description", width="medium", pinned=True,
            help="Editable — change the text and click 💾 Save.",
        ),
        fs_col: st.column_config.SelectboxColumn(
            "Final Station",
            options=["Final", "Undercarriage", "ComAcc", "GenAcc", "PMAcc"],
            help=(
                f"Which team's station receives this SKU's FN_Assy labor "
                f"at **{location}**. Default Final Assembly; choose another "
                f"team when they actually build the unit — e.g. **PDS "
                f"compressors → Undercarriage**, or ComAcc / GenAcc / PMAcc. "
                f"Switch facility in the sidebar to edit a different plant's "
                f"routing."
            ),
            required=False,
        ),
        ak_col: st.column_config.SelectboxColumn(
            "AccKIT Station",
            options=["AccKIT", "ComAcc", "GenAcc", "PMAcc", "Final"],
            help=(
                f"Which station receives this SKU's Accessory-KIT labor "
                f"at **{location}**. Default Accessories KIT; reroute when "
                f"there's no separate AccKIT team (e.g. PDS → ComAcc, "
                f"SDG → GenAcc)."
            ),
            required=False,
        ),
        pd_col: st.column_config.SelectboxColumn(
            "PDI Station",
            options=["PDI", "ComAcc", "GenAcc", "PMAcc", "Final", "QC"],
            help=(
                f"Which station receives this SKU's PDI labor at "
                f"**{location}**. Default PDI; reroute when there's no "
                f"separate PDI team (e.g. PDS → ComAcc, SDG → GenAcc, or "
                f"fold into QC)."
            ),
            required=False,
        ),
        ws_col: st.column_config.SelectboxColumn(
            "Wire Station",
            options=["Wire", "GenAcc", "ComAcc", "PMAcc", "Final", "Trailer"],
            help=(
                f"Which station receives this SKU's Wire-assembly labor at "
                f"**{location}**. Default Wire Assembly; reroute when this "
                f"plant has no Wire team (e.g. Cypress: SDG → GenAcc, "
                f"PDS → ComAcc)."
            ),
            required=False,
        ),
        uc_col: st.column_config.SelectboxColumn(
            "Undercarriage Station",
            options=["Undercarriage", "ComAcc", "GenAcc", "PMAcc", "Final"],
            help=(
                f"Which station absorbs this SKU's Undercarriage-Assembly labor "
                f"at **{location}** — i.e. the Final-Assembly labor that lands at "
                f"Undercarriage (PDS units). Default Undercarriage Assembly; "
                f"reroute when this plant has no dedicated undercarriage crew "
                f"(e.g. Henderson: Undercarriage → Com Accessories)."
            ),
            required=False,
        ),
        "Used (qty)": st.column_config.NumberColumn(
            "Used (qty)", disabled=True, pinned=True,
        ),
        "In schedule": st.column_config.CheckboxColumn(
            "In schedule", disabled=True, pinned=True,
        ),
        "Last Modified": st.column_config.TextColumn(
            "Last Modified", disabled=True,
            help="Date this row was last updated through the app. Blank = never edited.",
        ),
    }
    for c in editable_cols:
        col_cfg[c] = st.column_config.NumberColumn(
            c, min_value=0, step=1,
            help=("Saved and used at once; listed as pending until the workbook "
                  "carries the same value.") if excel_src else None,
        )
    if excel_src:
        # Which of this row's minutes are temporary (not yet in Excel).
        display_df.insert(
            2, "⏳ Pending in Excel",
            display_df["SKU"].map(lambda s: ", ".join(pending.get(s, {}).keys())),
        )
        col_cfg["⏳ Pending in Excel"] = st.column_config.TextColumn(
            "⏳ Pending in Excel", disabled=True, width="small",
            help="Minute columns changed in the app that the workbook doesn't carry yet.",
        )

    edited = st.data_editor(
        display_df,
        width="stretch", num_rows="fixed",
        key=f"fv_machine_editor_{int(only_used)}_{int(excel_src)}", height=600,
        column_config=col_cfg, hide_index=True,
    )

    edited_cells, renames = _detect_sku_renames(display_df, edited)
    if renames:
        st.caption("🔁 Pending SKU rename(s): "
                   + ", ".join(f"`{o}` → `{n}`" for o, n in renames))
    merge_approved = _render_rename_collisions(
        renames, machine_df, key_prefix="fv_m", used_map=used_fg,
    )
    _render_pending_changes(
        edited_cells, machine_df, editable_cols,
        text_cols=["Description", fs_col, ak_col, pd_col, ws_col, uc_col],
    )

    if st.button("💾 Save updated Machine catalog to GitHub", width="stretch"):
        _save_catalog_csv(
            edited=edited_cells, source_df=machine_df,
            editable_cols=editable_cols,
            file_path="data/machine_clean.csv", label="machine",
            text_cols=["Description", fs_col, ak_col, pd_col, ws_col, uc_col],
            renames=renames, merge_approved=merge_approved,
        )

    _render_add_new_sku(
        source_df=machine_df, editable_cols=editable_cols,
        file_path="data/machine_clean.csv", label="machine",
    )


def tab_floor_verification_accessory(acc_df, schedule_df, used_acc, machine_df):
    """The Accessory-catalog editor — extracted from tab_floor_verification."""
    st.subheader("🔌 Accessory catalog")
    _render_stale_data_banner("data/acc_clean.csv")
    excel_src = _excel_source()
    pending = _pending_for("accessory") if excel_src else {}
    if excel_src:
        st.info(
            "📗 **Excel is the source of truth for labor minutes.** You can still "
            "change a minute here — it's saved and used right away — but it's "
            "listed as ⏳ **pending** until the workbook is updated and re-imported "
            "(**📥 Import from Excel**). Description, SKU renames and the add-SKU / "
            "placeholder forms are the app's own."
        )
    _prov = _provenance_line("accessory")
    if _prov:
        st.caption(_prov)
    _render_pending_summary("accessory", pending)
    st.caption(
        "Edit labor times in the table below. SKUs used in the current schedule "
        "are highlighted. Click **💾 Save** to push changes to GitHub."
    )

    editable_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                     "PMAcc", "GenAcc", "ComAcc"]
    display_df = acc_df.reset_index(drop=True).copy()
    # Backward-compat: alias "Compressor" → "ComAcc" if cache is stale
    if "Compressor" in display_df.columns and "ComAcc" not in display_df.columns:
        display_df = display_df.rename(columns={"Compressor": "ComAcc"})
    if "Last Modified" not in display_df.columns:
        display_df["Last Modified"] = ""
    display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_acc.get(s, 0)))
    display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

    only_used = st.checkbox(
        "Show only SKUs used in current schedule", value=False, key="fv_a_only_used"
    )
    if only_used:
        display_df = display_df[display_df["In schedule"]].copy()
    display_df = display_df.sort_values(
        by=["In schedule", "Used (qty)"], ascending=[False, False]
    ).reset_index(drop=True)

    # Live computed columns: Bat from machine catalog + Total per unit.
    # Wrap every operand in pd.to_numeric so an Arrow-backed string column
    # (newer pandas default) can't trigger "Can only string multiply by an
    # integer." Each column is coerced through float, NA-filled, before any
    # arithmetic touches it.
    bat_counts = pd.to_numeric(
        display_df["SKU"].astype(str).map(
            lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
        ),
        errors="coerce",
    ).fillna(0).astype(int)
    non_batt_cols = [c for c in editable_cols if c != "BattSubRaw"]
    base_numeric = display_df[non_batt_cols].apply(
        lambda col: pd.to_numeric(col, errors="coerce").fillna(0)
    )
    base = base_numeric.sum(axis=1)
    if "BattSubRaw" in display_df.columns:
        per_batt = pd.to_numeric(display_df["BattSubRaw"], errors="coerce").fillna(0)
    else:
        per_batt = pd.Series(0, index=display_df.index)
    display_df["Bat"] = bat_counts.astype(int)
    display_df["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)
    display_df["Total per unit (p-hr)"] = (
        display_df["Total per unit (p-min)"] / 60.0
    ).round(2)

    col_cfg = {
        # See note in tab_floor_verification_machine: keep pinned widths
        # compact so Streamlit's ~60% container-width threshold doesn't
        # silently turn off pinning.
        "SKU": st.column_config.TextColumn(
            "SKU", width="small", pinned=True,
            help=("Editable — rename to fix a typo. ⚠️ Schedule rows using the "
                  "OLD SKU won't auto-update; re-point or re-upload them."),
        ),
        "Description": st.column_config.TextColumn(
            "Description", width="medium", pinned=True,
            help="Editable — change the text and click 💾 Save.",
        ),
        "Used (qty)": st.column_config.NumberColumn(
            "Used (qty)", disabled=True, pinned=True,
        ),
        "In schedule": st.column_config.CheckboxColumn(
            "In schedule", disabled=True, pinned=True,
        ),
        "Bat": st.column_config.NumberColumn(
            "Bat", disabled=True,
            help="Battery count for this accessory's FG family — from machine_clean.csv.",
        ),
        "Total per unit (p-min)": st.column_config.NumberColumn(
            "Total per unit (p-min)", disabled=True,
            help="Non-battery labor + BattSubRaw × Bat.",
        ),
        "Total per unit (p-hr)": st.column_config.NumberColumn(
            "Total per unit (p-hr)", disabled=True,
            help="Total per unit in person-hours.",
            format="%.2f",
        ),
        "Last Modified": st.column_config.TextColumn(
            "Last Modified", disabled=True,
            help="Date this row was last updated through the app. Blank = never edited.",
        ),
    }
    for c in editable_cols:
        col_cfg[c] = st.column_config.NumberColumn(
            c, min_value=0, step=1,
            help=("Saved and used at once; listed as pending until the workbook "
                  "carries the same value.") if excel_src else None,
        )
    if excel_src:
        display_df.insert(
            2, "⏳ Pending in Excel",
            display_df["SKU"].map(lambda s: ", ".join(pending.get(s, {}).keys())),
        )
        col_cfg["⏳ Pending in Excel"] = st.column_config.TextColumn(
            "⏳ Pending in Excel", disabled=True, width="small",
            help="Minute columns changed in the app that the workbook doesn't carry yet.",
        )

    edited = st.data_editor(
        display_df,
        width="stretch", num_rows="fixed",
        key=f"fv_acc_editor_{int(only_used)}_{int(excel_src)}", height=600,
        column_config=col_cfg, hide_index=True,
    )
    st.caption(
        "💡 **Total per unit** = non-battery labor + `BattSubRaw × Bat`. "
        "`Bat` is the exact count from the machine catalog (max within the family)."
    )

    edited_cells, renames = _detect_sku_renames(display_df, edited)
    if renames:
        st.caption("🔁 Pending SKU rename(s): "
                   + ", ".join(f"`{o}` → `{n}`" for o, n in renames))
    merge_approved = _render_rename_collisions(
        renames, acc_df, key_prefix="fv_a", used_map=used_acc,
    )
    _render_pending_changes(edited_cells, acc_df, editable_cols, text_cols=["Description"])

    if st.button("💾 Save updated Accessory catalog to GitHub", width="stretch"):
        _save_catalog_csv(
            edited=edited_cells, source_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv", label="accessory",
            text_cols=["Description"], renames=renames,
            merge_approved=merge_approved,
        )

    _render_add_new_sku(
        source_df=acc_df, editable_cols=editable_cols,
        file_path="data/acc_clean.csv", label="accessory",
    )

    # XXX-style placeholder helper (replaces the old A999 family placeholder)
    _render_add_xxx_placeholder(
        acc_df=acc_df, editable_cols=editable_cols,
        file_path="data/acc_clean.csv",
    )


def _render_pending_changes(edited, source_df, editable_cols, text_cols=None):
    """Show a small expander listing cells where the edited value differs from
    the source. Helps the user confirm their edits are captured before clicking
    Save (and helps debug 'nothing happens' cases)."""
    text_cols = text_cols or []
    if edited is None or len(edited) == 0:
        return
    if "SKU" not in edited.columns:
        return
    try:
        ei = edited.set_index("SKU")
    except Exception:
        return

    diffs = []
    for sku in ei.index:
        if sku not in source_df.index:
            continue
        for col in editable_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.at[sku, col]
            old_val = source_df.at[sku, col]
            try:
                if pd.notna(new_val) and float(new_val) != float(old_val):
                    diffs.append({
                        "SKU": sku, "Column": col,
                        "Old": float(old_val), "New": float(new_val),
                        "Δ": float(new_val) - float(old_val),
                    })
            except (TypeError, ValueError):
                continue
        # Text columns (e.g. Description, Final Station) — string compare,
        # no numeric Δ. Robust to source_df missing the column (stale cache).
        for col in text_cols:
            if col not in ei.columns:
                continue
            new_v = ei.at[sku, col]
            old_v = source_df.at[sku, col] if col in source_df.columns else ""
            new_s = "" if pd.isna(new_v) else str(new_v).strip()
            old_s = "" if pd.isna(old_v) else str(old_v).strip()
            if new_s != old_s:
                diffs.append({
                    "SKU": sku, "Column": col,
                    "Old": old_s, "New": new_s, "Δ": "—",
                })

    if not diffs:
        st.caption("ℹ️ No pending edits — make a change in the table above before saving.")
        return

    n = len(diffs)
    with st.expander(f"📝 {n} pending change(s) — preview before saving", expanded=False):
        st.dataframe(pd.DataFrame(diffs), width="stretch", hide_index=True)


def _routing_seed_preview(file_path: str, rows) -> "pd.DataFrame | None":
    """What per-facility routing each new SKU in ``rows`` would get, as
    :func:`routing_seed_table` — one line per SKU with each plant's reroutes.

    ``rows`` are ``{"SKU": ..., "Description": ...}`` dicts (an Excel import's
    new rows, or bare SKUs from the missing-SKU banner); a routing cell a row
    already carries wins over the seed, exactly as the save does. Seeds come
    from the catalog CSV on disk with its blanks intact. Returns None when the
    catalog has no routing columns (the accessory catalog) or there is nothing
    to seed.
    """
    rows = [r for r in (rows or []) if str((r or {}).get("SKU", "")).strip()]
    if not rows:
        return None
    catalog = _raw_catalog_csv_cached(file_path, _csv_mtime(Path(file_path).name))
    seeder = RoutingSeeder(catalog)
    if not seeder.columns:
        return None
    seeds = [
        seeder.seed(r["SKU"], r.get("Description", "")).with_given(r) for r in rows
    ]
    return routing_seed_table(seeds)


def _render_seeded_routing(seed) -> None:
    """After an add-SKU save: the per-facility routing the new row was seeded
    with, stations × plants. Shows nothing for a catalog without routing
    columns (the accessory catalog)."""
    if seed is None or not getattr(seed, "values", None):
        return
    st.caption(
        f"🧭 Per-facility routing seeded from {seed.describe()} — the new SKU "
        "inherits each plant's custom routing from the most similar SKU already "
        "in the catalog. Change it in the table above if that isn't right."
    )
    st.dataframe(seed.preview_frame(), width="stretch", hide_index=True)


def _render_add_xxx_placeholder(acc_df, editable_cols, file_path):
    """Quick form to add an estimation placeholder accessory (e.g. `BOSS25 AXXX`).

    Mirrors the machine-catalog `XXX` convention: pick a family, generate
    `{family} AXXX` (with space), and save with default labor times you can
    adjust. Existing examples already in the catalog include `PDS100 AXXX`,
    `BOSS25PM AXXX`, `BOSS220PM AXXX`, etc.

    Note: the model does NOT auto-fall-back to this placeholder when an
    accessory is missing. Some real orders have no accessory component; a
    silent fallback would inflate labor. The placeholder is a regular row
    that the user can OPT to use by typing its SKU in the manual entry. The
    Data Quality view automatically flags any `XXX` SKU as `⚠ Estimation placeholder`.
    """
    with st.expander("➕ Add placeholder accessory (XXX)", expanded=False):
        st.caption(
            "Creates a `{family} AXXX` placeholder accessory row, matching the "
            "machine-catalog XXX naming convention. This is **not** used as an "
            "automatic fallback — it's a conventionally-named accessory you can "
            "reference explicitly."
        )

        family = st.selectbox(
            "FG family",
            options=KNOWN_FAMILIES,
            help="The placeholder SKU will be `{family} AXXX`.",
            key="placeholder_family",
        )
        new_sku = f"{family} AXXX"
        st.text(f"SKU → {new_sku}")

        # Defaults — sensible numbers for the typical family placeholder
        default_labor = {
            "Warehouse": 10,
            "AccKIT": 10,
            "Nameplate Prep": 10,
            "BattSubRaw": 320,
            "PMAcc": 60,
            "GenAcc": 60,
            "ComAcc": 0,
        }

        with st.form("add_xxx_placeholder", clear_on_submit=True):
            new_desc = st.text_input(
                "Description",
                value=f"{family} ESTIMATION ACCESSORY PACKAGE",
                key="placeholder_desc",
            )

            cols_per_row = 4
            new_values = {}
            for i in range(0, len(editable_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, c in enumerate(editable_cols[i:i + cols_per_row]):
                    new_values[c] = row_cols[j].number_input(
                        c, min_value=0, step=1,
                        value=int(default_labor.get(c, 0)),
                        key=f"placeholder_num_{c}",
                    )

            submitted = st.form_submit_button(
                f"💾 Add `{new_sku}` & save to GitHub",
                width="stretch",
            )

        if not submitted:
            return

        # Validate
        existing = {str(s).upper() for s in acc_df["SKU"].astype(str)}
        if new_sku.upper() in existing:
            st.error(
                f"`{new_sku}` already exists in the accessory catalog. "
                "Edit the existing row in the table above instead."
            )
            return

        token = _github_token()
        if not token:
            st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            return

        today = today_local_str()
        new_sku_upper = new_sku.upper()
        response = None
        seed = None
        for attempt in (1, 2):
            try:
                with st.spinner(
                    f"Adding {new_sku} to {file_path}"
                    + (" — retrying" if attempt == 2 else "") + "..."
                ):
                    fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                    if fresh_df.empty:
                        st.error("Could not fetch the current catalog from GitHub.")
                        return

                    fresh_skus_upper = set(
                        fresh_df["SKU"].astype(str).str.strip().str.upper()
                    ) if "SKU" in fresh_df.columns else set()
                    if new_sku_upper in fresh_skus_upper:
                        st.error(
                            f"`{new_sku}` was just added by another user. Refresh."
                        )
                        return

                    if "Last Modified" not in fresh_df.columns:
                        fresh_df["Last Modified"] = ""

                    new_row = {col: "" for col in fresh_df.columns}
                    new_row["SKU"] = new_sku
                    if "Description" in new_row:
                        new_row["Description"] = new_desc
                    for c, v in new_values.items():
                        if c in new_row:
                            new_row[c] = int(v)
                    new_row["Last Modified"] = today
                    # Per-facility routing (no-op for a catalog without the
                    # columns): seed blank cells from the most similar row.
                    seed = seed_facility_routing(fresh_df, new_sku, new_desc)
                    seed.fill(new_row)

                    merged = pd.concat(
                        [fresh_df, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    csv_text = merged.to_csv(index=False)

                    response = save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Add XXX placeholder accessory '{new_sku}' via app",
                        sha=fresh_sha,
                    )
                break
            except GitHubConflict:
                if attempt == 2:
                    st.error("❌ Save failed: concurrent commit. Please refresh and retry.")
                    return
                continue
            except Exception as e:
                _show_github_error(e, "Save")
                return

        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            _clear_catalog_cache(file_path)
        except Exception:
            pass
        _mark_catalog_saved(file_path, response)
        _track_and_flush("catalog_add_placeholder", file=file_path, sku=new_sku)
        st.success(
            f"✅ Added XXX placeholder accessory `{new_sku}` "
            + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
            + "  \nIn use now; teammates see it after the redeploy (~1 min)."
        )
        _render_seeded_routing(seed)


def _render_add_new_sku(source_df, editable_cols, file_path, label, extra_text_cols=None):
    """Form to add a brand-new SKU to a catalog and immediately push to GitHub.

    Args:
      source_df:     The loaded catalog DataFrame (machine_df or acc_df).
      editable_cols: Labor columns the user can fill in (numbers).
      file_path:     Path in the repo (e.g. "data/machine_clean.csv").
      label:         Display label for messages ("machine" or "accessory").
      extra_text_cols: Optional extra text columns to capture (defaults to none).
    """
    extra_text_cols = extra_text_cols or []
    with st.expander(f"➕ Add a new {label} SKU", expanded=False):
        st.caption(
            f"Use this to add a brand-new {label} SKU that isn't in the catalog "
            "yet. The new row is committed to GitHub immediately along with today's "
            "date in **Last Modified**."
        )
        if routing_columns_in(getattr(source_df, "columns", [])):
            st.caption(
                "🧭 Per-facility routing (Final / AccKIT / PDI / Wire / "
                "Undercarriage station at every plant) is seeded from the most "
                "similar SKU already in the catalog — same family and class — so "
                "the new SKU inherits each plant's custom routing. You'll see what "
                "it got right after the save."
            )
        with st.form(f"add_new_{label}", clear_on_submit=True):
            new_sku = st.text_input(
                "SKU", help="Must be unique — case-insensitive check against the catalog.",
                key=f"new_sku_{label}",
            ).strip()
            new_desc = st.text_input(
                "Description", help="Short, descriptive text shown in catalog views.",
                key=f"new_desc_{label}",
            ).strip()

            extra_text_values = {}
            for c in extra_text_cols:
                extra_text_values[c] = st.text_input(
                    c, key=f"new_text_{label}_{c}",
                ).strip()

            cols_per_row = 4
            new_values = {}
            for i in range(0, len(editable_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, c in enumerate(editable_cols[i:i + cols_per_row]):
                    new_values[c] = row_cols[j].number_input(
                        c, min_value=0, step=1, value=0,
                        key=f"new_num_{label}_{c}",
                    )

            submitted = st.form_submit_button(
                f"💾 Add {label} SKU and save to GitHub", width="stretch",
            )

        if not submitted:
            return

        if not new_sku:
            st.error("SKU cannot be empty.")
            return
        # CSV / spreadsheet formula injection guard — values starting with
        # =, +, -, or @ are interpreted as formulas when the CSV is opened
        # in Excel / Sheets and can execute commands. Disallow them.
        if new_sku[:1] in ("=", "+", "-", "@"):
            st.error(
                f"`{new_sku}` cannot start with `=`, `+`, `-`, or `@` — these "
                "trigger formula execution in Excel. Choose a different prefix."
            )
            return
        existing = {str(s).upper() for s in source_df["SKU"].astype(str)}
        if new_sku.upper() in existing:
            st.error(f"`{new_sku}` is already in the {label} catalog. "
                     "Edit the existing row in the table above instead.")
            return

        token = _github_token()
        if not token:
            st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            return

        today = today_local_str()
        new_sku_upper = new_sku.upper()

        # Fetch + append + push, with one retry on SHA conflict.
        response = None
        seed = None
        for attempt in (1, 2):
            try:
                with st.spinner(
                    f"Adding {new_sku} to {file_path}"
                    + (" — retrying" if attempt == 2 else "") + "..."
                ):
                    fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                    if fresh_df.empty:
                        st.error("Could not fetch the current catalog from GitHub.")
                        return

                    # Concurrent-safety check: did someone else just add this SKU?
                    fresh_skus_upper = set(
                        fresh_df["SKU"].astype(str).str.strip().str.upper()
                    ) if "SKU" in fresh_df.columns else set()
                    if new_sku_upper in fresh_skus_upper:
                        st.error(
                            f"`{new_sku}` was just added by another user. "
                            "Refresh the page to see it, then edit the existing row instead."
                        )
                        return

                    if "Last Modified" not in fresh_df.columns:
                        fresh_df["Last Modified"] = ""

                    # Build the new row matching fresh_df's schema
                    new_row = {col: "" for col in fresh_df.columns}
                    new_row["SKU"] = new_sku
                    if "Description" in new_row:
                        new_row["Description"] = new_desc
                    for c, v in extra_text_values.items():
                        if c in new_row:
                            new_row[c] = v
                    for c, v in new_values.items():
                        if c in new_row:
                            new_row[c] = int(v)
                    new_row["Last Modified"] = today
                    # Per-facility routing: a blank cell would load as the
                    # station's default team, so seed the row from the most
                    # similar SKU on the fetched file (no-op for a catalog
                    # without routing columns).
                    seed = seed_facility_routing(fresh_df, new_sku, new_desc)
                    seed.fill(new_row)

                    merged = pd.concat(
                        [fresh_df, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    csv_text = merged.to_csv(index=False)

                    response = save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Add new {label} SKU {new_sku} via app",
                        sha=fresh_sha,
                    )
                break  # success
            except GitHubConflict:
                if attempt == 2:
                    st.error(
                        "❌ Save failed: another user committed concurrently. "
                        "Please refresh the page and try again."
                    )
                    return
                continue
            except Exception as e:
                _show_github_error(e, "Save")
                return

        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            _clear_catalog_cache(file_path)
        except Exception:
            pass
        _mark_catalog_saved(file_path, response)
        _track_and_flush("catalog_add_sku", file=file_path, label=label, sku=new_sku)
        st.success(
            f"✅ Added `{new_sku}` to `{file_path}` "
            + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
            + "  \nIn use now; teammates see it after the redeploy (~1 min)."
        )
        _render_seeded_routing(seed)


@st.cache_data(ttl=30, show_spinner=False)
def _latest_sha_cached(file_path: str, token: str) -> "str | None":
    """Cache the GitHub SHA lookup for 30 seconds to avoid hammering the API."""
    return latest_catalog_sha(file_path, token)


def _mark_catalog_saved(file_path: str, response=None) -> None:
    """After our OWN successful commit to ``file_path``, record the new blob
    SHA as the one this session has loaded, so the stale-data banner doesn't
    flag the saver's own save as "updated by another user".

    ``response`` is the GitHub Contents-API PUT payload; its ``content.sha``
    is exactly what :func:`latest_catalog_sha` returns next. When the payload
    doesn't carry it, forget the loaded SHA instead — the banner's next render
    re-records whatever is current.
    """
    key = f"loaded_sha::{file_path}"
    sha = None
    if isinstance(response, dict):
        content = response.get("content")
        if isinstance(content, dict):
            sha = content.get("sha")
    if sha:
        st.session_state[key] = str(sha)
    else:
        st.session_state.pop(key, None)


def _render_stale_data_banner(file_path: str) -> None:
    """Show a yellow banner if another user committed to `file_path` after
    we recorded our 'loaded' SHA. The user can click 🔄 to refresh. Every save
    path calls :func:`_mark_catalog_saved` so the saver's own commit is never
    reported as someone else's."""
    token = _github_token()
    if not token:
        return  # No token → can't poll; silently skip the banner

    state_key = f"loaded_sha::{file_path}"
    # On the first render for this file in this session, record the current SHA
    # as "what the user loaded" — anything newer is stale relative to them.
    if state_key not in st.session_state:
        st.session_state[state_key] = _latest_sha_cached(file_path, token)
        return

    loaded_sha = st.session_state[state_key]
    latest = _latest_sha_cached(file_path, token)
    if latest is None or loaded_sha is None:
        return  # File missing or transient API error — silently skip
    if latest != loaded_sha:
        c1, c2 = st.columns([4, 1])
        with c1:
            st.warning(
                f"⚠️ **`{file_path}` was just updated by another user.** "
                "Your view is stale — refresh before saving so you don't lose their changes."
            )
        with c2:
            if st.button("🔄 Refresh", key=f"stale_refresh_{file_path}"):
                # Pull their commit into this container's copy so the view —
                # and every tab — uses it now, not after the next redeploy.
                try:
                    _fresh_bytes, _fresh_sha = fetch_catalog_csv_from_github(
                        file_path, token,
                    )
                    if _fresh_bytes:
                        mirror_csv_locally(
                            file_path, _fresh_bytes.decode("utf-8-sig"),
                        )
                        latest = _fresh_sha or latest
                except Exception:
                    pass
                try:
                    _clear_catalog_cache(file_path)
                except Exception:
                    pass
                st.session_state[state_key] = latest
                st.rerun()


def _detect_sku_renames(display_df, edited, key_col="SKU"):
    """Positionally compare the SKU column of the rendered ``display_df`` against
    the editor's ``edited`` output (same row order — the editors use
    ``num_rows="fixed"``) to find inline SKU renames.

    Returns ``(edited_cells, renames)`` where:
      • ``renames`` is a list of ``(old_sku, new_sku)`` for rows whose SKU
        changed (new value non-blank), and
      • ``edited_cells`` is a copy of ``edited`` with the SKU column reset to the
        original values — so the cell/text diff (keyed by the *old* SKU) still
        detects labor/description edits made on a row that's also being renamed.
    """
    if edited is None or key_col not in edited.columns or key_col not in display_df.columns:
        return edited, []
    orig = display_df[key_col].astype(str).str.strip().tolist()
    new = edited[key_col].astype(str).str.strip().tolist()
    if len(orig) != len(new):
        # Shapes diverged unexpectedly — skip rename detection, diff as-is.
        return edited, []
    renames = [(o, n) for o, n in zip(orig, new) if o != n and n]
    edited_cells = edited.copy()
    edited_cells[key_col] = orig
    return edited_cells, renames


def _compute_cell_diffs(edited, source_df, editable_cols, text_cols=None):
    """Return a list of (sku, col, new_value) tuples for cells the user changed.

    Compares `edited` (user's data_editor output) against `source_df` (the
    in-memory catalog loaded at session start).

    `editable_cols` are numeric (compared as floats). `text_cols` (e.g.
    ``["Description"]``) are compared as trimmed strings so free-text columns
    can be edited and saved too.
    """
    text_cols = text_cols or []
    # Drop UI-only / derived columns first. We exclude any name that is also in
    # `editable_cols` so we don't accidentally strip a real editable column
    # (e.g. "Bat" is a derived column in the accessory editor but a real
    # editable column in the machine editor).
    candidate_drops = [
        "Bat", "Total per unit (p-min)",
        "Total (1 batt)", "Total (3 batt)", "Total (5 batt)",
        "Total labor (p-min)",
        "Used (qty)", "In schedule",
    ]
    drop_cols = [c for c in candidate_drops
                 if c in edited.columns and c not in editable_cols]
    work = edited.drop(columns=drop_cols)
    if "SKU" not in work.columns:
        return []
    ei = work.set_index("SKU")

    diffs = []
    for sku in ei.index:
        if sku not in source_df.index:
            continue
        for col in editable_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.loc[sku, col]
            old_val = source_df.loc[sku, col]
            try:
                if pd.notna(new_val) and float(new_val) != float(old_val):
                    diffs.append((sku, col, new_val))
            except (TypeError, ValueError):
                continue
        # Text columns (e.g. Description, Final Station) — compare as trimmed
        # strings. Robust to the column being missing from ``source_df`` (e.g.
        # a stale cache that pre-dates a newly-added column): treat the
        # missing source as an empty string so any non-empty edit still
        # registers as a diff and reaches the GitHub save.
        for col in text_cols:
            if col not in ei.columns:
                continue
            new_val = ei.loc[sku, col]
            old_val = source_df.loc[sku, col] if col in source_df.columns else ""
            new_s = "" if pd.isna(new_val) else str(new_val).strip()
            old_s = "" if pd.isna(old_val) else str(old_val).strip()
            if new_s != old_s:
                diffs.append((sku, col, new_s))
    return diffs


def _apply_diffs_and_serialize(fresh_df, diffs, editable_cols, renames=None,
                               merges=None):
    """Apply the cell-level diffs (and optional SKU renames) on top of
    `fresh_df` (from GitHub) and return CSV text.

    `diffs` are keyed by the row's *current* (old) SKU. `renames` is a list of
    ``(old_sku, new_sku)`` applied AFTER the cell diffs — so cell edits made on
    a row that's also being renamed still land correctly (they're matched by
    the old SKU first, then the SKU is changed). Stamps Last Modified for any
    row that changed.

    `merges` is a list of ``(old_sku, existing_sku)`` the planner confirmed are
    typo-duplicates: the ``old_sku`` row is **deleted** and the ``existing_sku``
    row already on file is kept untouched. Applied last, so a merged-away row
    can't be resurrected by a later rename.

    Returns ``(csv_text, n_cell_rows_changed, n_renamed, n_merged)``.
    """
    renames = renames or []
    merges = merges or []
    today = today_local_str()
    if "Last Modified" not in fresh_df.columns:
        fresh_df["Last Modified"] = ""

    # Build an index lookup. The CSV from GitHub doesn't have SKU as index;
    # we need to find each SKU by its column value.
    if "SKU" not in fresh_df.columns:
        raise RuntimeError("Catalog CSV is missing the SKU column.")
    fresh_df = fresh_df.copy()
    fresh_df["__sku_norm"] = fresh_df["SKU"].astype(str).str.strip()

    changed_skus = set()
    for sku, col, new_val in diffs:
        mask = fresh_df["__sku_norm"] == str(sku).strip()
        if not mask.any():
            continue
        # Ensure the column exists in the fresh file (e.g. column renames)
        if col not in fresh_df.columns:
            fresh_df[col] = 0
        idx = fresh_df.index[mask][0]
        fresh_df.at[idx, col] = new_val
        changed_skus.add(sku)

    for sku in changed_skus:
        mask = fresh_df["__sku_norm"] == str(sku).strip()
        if mask.any():
            idx = fresh_df.index[mask][0]
            fresh_df.at[idx, "Last Modified"] = today

    # Apply SKU renames last, matching on the (old) normalized SKU.
    n_renamed = 0
    for old_sku, new_sku in renames:
        mask = fresh_df["__sku_norm"] == str(old_sku).strip()
        if not mask.any():
            continue
        idx = fresh_df.index[mask][0]
        fresh_df.at[idx, "SKU"] = new_sku
        fresh_df.at[idx, "Last Modified"] = today
        fresh_df.at[idx, "__sku_norm"] = str(new_sku).strip()
        n_renamed += 1

    # Confirmed typo-duplicates: drop the duplicate row, keep the SKU already
    # on file exactly as it is. Deliberately NOT a value merge — the planner
    # said the surviving row is the right one, so copying the typo row's labor
    # over it would silently overwrite good data with the values they were
    # trying to get rid of.
    n_merged = 0
    for old_sku, _existing_sku in merges:
        mask = fresh_df["__sku_norm"] == str(old_sku).strip()
        if not mask.any():
            continue
        fresh_df = fresh_df[~mask]
        n_merged += 1

    fresh_df = fresh_df.drop(columns=["__sku_norm"])
    return fresh_df.to_csv(index=False), len(changed_skus), n_renamed, n_merged


def _read_fresh_csv(file_path: str, token: str) -> "tuple[pd.DataFrame, str | None]":
    """Fetch the latest CSV from GitHub and parse it. Returns (df, sha)."""
    csv_bytes, sha = fetch_catalog_csv_from_github(file_path, token)
    if not csv_bytes:
        # First-time write — empty DataFrame is fine (caller will populate)
        return pd.DataFrame(), sha
    # Some CSVs are latin-1 encoded; try utf-8 first, fall back to latin-1
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes))
    except UnicodeDecodeError:
        df = pd.read_csv(io.BytesIO(csv_bytes), encoding="latin-1")
    return df, sha


def _import_missing_skus_to_catalog(
    file_path: str, label: str, skus: "list[str]", token: str,
) -> "tuple[list[str], list[str]]":
    """Append placeholder rows for ``skus`` to the catalog CSV on GitHub.

    Each new row has the SKU + today's ``Last Modified`` and **blank** labor
    cells — the loaders coerce blanks to 0, so the SKU enters the catalog with
    zero labor that the planner fills in afterwards. This is the "quick import"
    path: it gets the missing SKU into the system so its units stop being
    dropped, without forcing the planner to enter labor times up front. The
    per-facility routing cells are **seeded** (:class:`RoutingSeeder`) from
    the most similar SKU already on the file, so the row inherits each
    plant's custom routing instead of loading as the default team.

    Reuses the same fresh-fetch + SHA-locked push + one-retry pattern as
    :func:`_render_add_new_sku`. SKUs already present on the freshly-fetched
    file (someone else added them) and SKUs that would trigger a spreadsheet
    formula (leading ``= + - @``) are skipped.

    Returns ``(added, skipped)`` SKU lists. Raises on a hard save failure so
    the caller can surface the error.
    """
    today = today_local_str()
    added: list[str] = []
    skipped: list[str] = []

    for attempt in (1, 2):
        fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
        if fresh_df.empty or "SKU" not in fresh_df.columns:
            raise RuntimeError(f"Could not read `{file_path}` from GitHub.")

        if "Last Modified" not in fresh_df.columns:
            fresh_df["Last Modified"] = ""

        existing = set(fresh_df["SKU"].astype(str).str.strip().str.upper())
        seeder = RoutingSeeder(fresh_df)

        added = []
        skipped = []
        new_rows = []
        for raw in skus:
            sku = str(raw).strip()
            if not sku:
                continue
            # Formula-injection guard — same rule as the manual add form.
            if sku[:1] in ("=", "+", "-", "@"):
                skipped.append(sku)
                continue
            if sku.upper() in existing:
                skipped.append(sku)
                continue
            row = {col: "" for col in fresh_df.columns}
            row["SKU"] = sku
            row["Last Modified"] = today
            seeder.seed(sku).fill(row)
            new_rows.append(row)
            added.append(sku)
            existing.add(sku.upper())  # guard against dupes within this batch

        if not new_rows:
            return added, skipped  # nothing new to write

        merged = pd.concat([fresh_df, pd.DataFrame(new_rows)], ignore_index=True)
        csv_text = merged.to_csv(index=False)
        try:
            response = save_catalog_to_github(
                csv_text, file_path, token,
                message=f"Import {len(added)} missing {label} SKU(s) from schedule via app",
                sha=fresh_sha,
            )
            _mark_catalog_saved(file_path, response)
            return added, skipped
        except GitHubConflict:
            if attempt == 2:
                raise
            continue  # someone committed concurrently — refetch and retry

    return added, skipped


def _rename_collisions(renames, source_df):
    """Return the ``(old, new)`` renames whose target SKU already exists.

    Used at *render* time (against the in-memory catalog) so the planner can
    resolve a collision before saving, rather than discovering it as a failed
    save. The save path re-checks against the freshly-fetched file — this is a
    UX pre-check, not the authority.
    """
    if source_df is None or "SKU" not in getattr(source_df, "columns", []):
        return []
    existing = set(source_df["SKU"].astype(str).str.strip().str.upper())
    out = []
    for old, new in renames:
        old, new = str(old).strip(), str(new).strip()
        if not old or not new or old == new:
            continue
        if new.upper() in (existing - {old.upper()}):
            out.append((old, new))
    return out


def _render_rename_collisions(renames, source_df, key_prefix, used_map=None):
    """Surface rename-onto-an-existing-SKU collisions *before* the save, and
    let the planner resolve each one as a typo-duplicate merge.

    Renaming `BOSS25-10` → `BOSS25-010` when `BOSS25-010` is already in the
    catalog used to be a flat rejection at save time, which is a dead end when
    the real story is "these are the same product, one of them is a typo". The
    checkbox here says exactly what merging does — keep the row already on
    file, delete the duplicate — and warns when the SKU about to disappear is
    referenced by the current schedule.

    Returns the set of upper-cased *old* SKUs approved for merging.
    """
    collisions = _rename_collisions(renames, source_df)
    if not collisions:
        return set()

    approved = set()
    used_map = used_map or {}
    for old, new in collisions:
        st.warning(
            f"⚠️ **`{new}` is already in the catalog.** You renamed `{old}` → "
            f"`{new}`, so two rows would share one SKU."
        )
        n_used = int(used_map.get(old, 0) or 0)
        used_note = (
            f" ⚠️ `{old}` is used by **{n_used} unit(s)** in the current "
            f"schedule — those rows will show as *missing SKU* until you "
            f"re-point them at `{new}`."
            if n_used else ""
        )
        if st.checkbox(
            f"It's a typo — merge `{old}` into the existing `{new}`",
            key=f"{key_prefix}_merge_{old}",
            help=(
                f"Keeps the `{new}` row already in the catalog exactly as it is "
                f"and deletes the duplicate `{old}` row. The existing row's "
                f"labor times win — nothing is copied over from `{old}`."
            ),
        ):
            approved.add(old.upper())
            st.caption(
                f"↪️ On save: `{new}` is kept unchanged, `{old}` is removed."
                + used_note
            )
        else:
            st.caption(
                f"↪️ Leave unticked to have the save rejected — then fix `{old}` "
                f"by hand, or edit the `{new}` row directly."
            )
    return approved


def _validate_renames(renames, fresh_df, merge_approved=None):
    """Validate ``(old, new)`` SKU renames against the freshly-fetched catalog.

    Returns ``(clean_renames, merges, error_message)``. ``error_message`` is
    None when everything is valid. Rules: new SKU non-empty, not a
    spreadsheet-formula prefix, unique within the batch, and not colliding with
    a *different* existing SKU.

    ``merge_approved`` is a set of upper-cased *old* SKUs the planner has
    explicitly confirmed are typo-duplicates. For those, a collision is not an
    error: the rename is reclassified as a **merge** — the edited (typo) row is
    dropped and the SKU already in the catalog is kept as-is. Merges come back
    in the second return value so the caller can apply and report them
    separately from ordinary renames.
    """
    fresh_upper = set(fresh_df["SKU"].astype(str).str.strip().str.upper()) \
        if "SKU" in fresh_df.columns else set()
    approved = {str(s).strip().upper() for s in (merge_approved or set())}
    clean = []
    merges = []
    seen_new = set()
    for old, new in renames:
        old = str(old).strip()
        new = str(new).strip()
        if not old or old == new:
            continue
        if not new:
            return None, None, f"`{old}` → (blank): a SKU can't be renamed to an empty value."
        if new[:1] in ("=", "+", "-", "@"):
            return None, None, (
                f"`{new}` can't start with `=`, `+`, `-`, or `@` "
                "(those trigger formula execution in Excel)."
            )
        if new.upper() in seen_new:
            return None, None, f"Two rows are being renamed to the same SKU `{new}`."
        # Collision with a different existing SKU (one not being renamed away).
        others = fresh_upper - {old.upper()}
        if new.upper() in others:
            if old.upper() in approved:
                # Confirmed typo — fold this row into the SKU already on file.
                merges.append((old, new))
                continue
            return None, None, (
                f"`{new}` already exists in the catalog — pick a unique SKU, "
                "edit that row instead, or tick **“it's a typo — merge”** above "
                "to keep the existing row and drop this duplicate."
            )
        seen_new.add(new.upper())
        clean.append((old, new))
    return clean, merges, None


def _append_catalog_rows(fresh_df, additions):
    """Append brand-new SKU rows (from an Excel import) to the freshly-fetched
    catalog frame, matching its column schema.

    Each addition is a ``{column: value}`` dict carrying at least ``SKU``;
    columns the file doesn't have are dropped, labor columns the addition
    doesn't mention are left blank (the loaders read a blank labor cell as
    0), and ``Last Modified`` is stamped with today. A per-facility routing
    cell the addition leaves blank is **seeded** (:class:`RoutingSeeder`)
    from the most similar SKU already on the fetched file — the sheet never
    carries routing for the IE's workbook, and a blank would load as the
    default team; a routing value the sheet does give is kept as-is. A SKU
    already on the fetched file (someone added it
    since the planner exported), a formula-prefixed SKU (``= + - @``), or a
    repeat within the batch is skipped and reported, never overwritten — the
    same rules as :func:`_import_missing_skus_to_catalog`.

    Returns ``(frame, added_skus, skipped_skus)``.
    """
    additions = list(additions or [])
    if not additions:
        return fresh_df, [], []
    fresh_df = fresh_df.copy()
    if "Last Modified" not in fresh_df.columns:
        fresh_df["Last Modified"] = ""
    today = today_local_str()
    existing = set(fresh_df["SKU"].astype(str).str.strip().str.upper()) \
        if "SKU" in fresh_df.columns else set()
    added: list[str] = []
    skipped: list[str] = []
    rows: list[dict] = []
    seeder = RoutingSeeder(fresh_df)
    for item in additions:
        sku = str((item or {}).get("SKU", "")).strip()
        if not sku:
            continue
        if sku[:1] in ("=", "+", "-", "@") or sku.upper() in existing:
            skipped.append(sku)
            continue
        row = {col: "" for col in fresh_df.columns}
        for col, val in item.items():
            if col in row and col not in ("SKU", "Last Modified"):
                row[col] = "" if val is None else val
        row["SKU"] = sku
        row["Last Modified"] = today
        seeder.seed(sku, row.get("Description", "")).fill(row)
        rows.append(row)
        added.append(sku)
        existing.add(sku.upper())
    if rows:
        fresh_df = pd.concat([fresh_df, pd.DataFrame(rows)], ignore_index=True)
    return fresh_df, added, skipped


def _save_catalog_csv(edited, source_df, editable_cols, file_path, label,
                      text_cols=None, renames=None, merge_approved=None,
                      diffs=None, additions=None, via="app"):
    """Cell-level merge: compute the user's diff, fetch the latest file from
    GitHub, apply only those cells (and any SKU renames) on top, and push back.
    Retries once on a 409 SHA conflict (the narrow race window where someone
    else committed between our fetch and our PUT).

    `text_cols` (e.g. ``["Description"]``) lets free-text columns be edited and
    saved alongside the numeric labor columns. `renames` is a list of
    ``(old_sku, new_sku)`` for inline SKU renames. `merge_approved` is the set
    of old SKUs the planner confirmed are typo-duplicates — a rename onto an
    existing SKU then deletes the duplicate instead of failing the save.

    `diffs` lets a caller that already computed its ``(sku, col, new_value)``
    list (the Excel import — :func:`core.excel_io.diff_catalog_sheet`) skip
    the data_editor comparison; ``edited`` may then be None. `additions` is a
    list of new-row dicts (``{"SKU": ..., col: value}``) appended to the fresh
    file before the diffs are applied — see :func:`_append_catalog_rows`.
    `via` names the path in the commit message (``"app"`` for the editors,
    ``"Excel import"`` for the uploader).

    Returns the short commit SHA (or ``"saved"`` if GitHub didn't return
    one) on success, None when nothing was written.
    """
    renames = [r for r in (renames or []) if str(r[0]).strip() != str(r[1]).strip()]
    additions = list(additions or [])
    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    if diffs is None:
        diffs = _compute_cell_diffs(edited, source_df, editable_cols, text_cols=text_cols)
    diffs = list(diffs)
    if not diffs and not renames and not additions:
        st.info("No changes detected.")
        return

    n_diffs = len(diffs)
    # `renames` still mixes true renames with confirmed typo-merges; the split
    # isn't known until _validate_renames runs against the fetched file, so the
    # commit message and the counts below are finalized inside the loop.
    n_ren = len(renames)
    n_merged = 0
    n_added = 0
    added_skus: list[str] = []
    skipped_additions: list[str] = []
    applied_renames = list(renames)
    applied_merges = []
    commit_message = f"Update {label} catalog via {via}"

    response = None
    for attempt in (1, 2):
        try:
            with st.spinner(
                f"Saving {label} catalog to GitHub"
                f"{' — retrying' if attempt == 2 else ''}..."
            ):
                fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                if fresh_df.empty:
                    st.error("Could not fetch the current catalog from GitHub.")
                    return
                clean_renames, merges, rename_err = _validate_renames(
                    renames, fresh_df, merge_approved=merge_approved,
                )
                if rename_err:
                    st.error(f"❌ SKU rename rejected: {rename_err}")
                    return
                # New rows (Excel import) go on first, against the fetched
                # file, so a SKU someone added meanwhile is skipped rather
                # than duplicated; the cell diffs only ever touch rows that
                # were already there.
                fresh_df, added_skus, skipped_additions = _append_catalog_rows(
                    fresh_df, additions,
                )
                n_added = len(added_skus)
                if not diffs and not clean_renames and not merges and not n_added:
                    st.info(
                        "No changes to save — "
                        + (
                            "every SKU this would add is already on file "
                            f"({', '.join(f'`{s}`' for s in skipped_additions[:8])}); "
                            "export a fresh workbook to update those rows instead."
                            if skipped_additions else "nothing differs from the file."
                        )
                    )
                    return
                csv_text, changed_skus_count, n_renamed, n_merged = _apply_diffs_and_serialize(
                    fresh_df, diffs, editable_cols, renames=clean_renames,
                    merges=merges,
                )
                n_ren = len(clean_renames)
                applied_renames = list(clean_renames)
                applied_merges = list(merges)
                _parts = []
                if n_diffs:
                    _parts.append(f"{n_diffs} cells")
                if n_ren:
                    _parts.append(f"{n_ren} SKU rename(s)")
                if n_merged:
                    _parts.append(f"{n_merged} duplicate SKU(s) merged")
                if n_added:
                    _parts.append(f"{n_added} new SKU(s)")
                commit_message = (
                    f"Update {label} catalog via {via} ({', '.join(_parts)})"
                    if _parts else f"Update {label} catalog via {via}"
                )
                response = save_catalog_to_github(
                    csv_text, file_path, token,
                    message=commit_message,
                    sha=fresh_sha,
                )
            break  # success — exit retry loop
        except GitHubConflict:
            if attempt == 2:
                st.error(
                    "❌ Save failed: another user committed concurrently. "
                    "Please refresh the page (your edits are not lost — re-enter them) and try again."
                )
                return
            # else: loop will retry with a fresh fetch
            continue
        except Exception as e:
            st.error(
                f"❌ Save failed: {e}\n\n"
                "Common causes: GitHub token lacking **Contents: write** on the repo, token expired, "
                "or the repo path in `core/catalog_storage.py` is wrong."
            )
            return

    commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
    commit_url = (response or {}).get("commit", {}).get("html_url", "")
    try:
        _clear_catalog_cache(file_path)
    except Exception:
        pass
    _mark_catalog_saved(file_path, response)

    _track_and_flush(
        "catalog_save", file=file_path, label=label, cells=n_diffs, renames=n_ren,
        merges=n_merged, added=n_added, via=via,
    )

    # Excel-as-source: a minute edited in the app is saved (above) and used at
    # once, but recorded as *pending* so the next workbook import can tell
    # "Excel caught up" from "Excel still has the old number" — and won't
    # revert the planner in the second case. Editor saves only; the import
    # path reconciles pending itself. Numeric columns only (routing and
    # Description are the app's own). A SKU renamed or merged in this same
    # save is skipped: its pending key would point at a name that no longer
    # exists. Best effort — the catalog save already landed.
    n_pending = 0
    if (via == "app" and label in ("machine", "accessory") and diffs
            and _excel_source()):
        _moved = {str(o).strip() for o, _ in applied_renames} | {
            str(o).strip() for o, _ in applied_merges}
        _numeric = set(editable_cols)
        _pending_rows = []
        for _sku, _col, _new in diffs:
            if _col not in _numeric or str(_sku).strip() in _moved:
                continue
            try:
                _new_f = float(_new)
            except (TypeError, ValueError):
                continue
            _old = None
            if _sku in source_df.index and _col in source_df.columns:
                _ov = source_df.loc[_sku, _col]
                if isinstance(_ov, pd.Series):
                    _ov = _ov.iloc[0]
                try:
                    _old = None if pd.isna(_ov) else float(_ov)
                except (TypeError, ValueError):
                    _old = None
            _pending_rows.append((_sku, _col, _new_f, _old))
        if _pending_rows:
            try:
                record_pending_to_github(label, _pending_rows, token)
                _load_catalog_source_cached.clear()
                n_pending = len(_pending_rows)
            except Exception as exc:  # noqa: BLE001
                st.warning(
                    f"Saved, but couldn't mark {len(_pending_rows)} minute change(s) "
                    f"as pending in Excel: {exc}"
                )

    _saved_bits = []
    if n_diffs:
        _saved_bits.append(f"{n_diffs} cell change(s)")
    if n_ren:
        _saved_bits.append(f"{n_ren} SKU rename(s)")
    if n_merged:
        _saved_bits.append(f"{n_merged} duplicate SKU(s) merged")
    if n_added:
        _saved_bits.append(f"{n_added} new SKU(s)")
    st.success(
        f"✅ Saved {' + '.join(_saved_bits)} to `{file_path}` "
        + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
    )
    if n_pending:
        st.info(
            f"⏳ {n_pending} minute change(s) are in use now and marked **pending in "
            "Excel**. Update the workbook and re-import to make them permanent — "
            "until then an import keeps your values."
        )
    if n_ren:
        _ren_list = ", ".join(f"`{o}` → `{n}`" for o, n in applied_renames[:8])
        st.warning(
            f"🔁 **Renamed: {_ren_list}.** Heads up — schedule rows and saved "
            "schedules that still reference the **old** SKU won't auto-update; "
            "they'll show as *missing SKU* until you re-point or re-upload them. "
            "(Catalog-only change.)"
        )
    if n_merged:
        _mrg_list = ", ".join(f"`{o}` → `{n}`" for o, n in applied_merges[:8])
        # A merge deletes a row, so call out anything that pointed at it: cell
        # edits made on the duplicate in this same save are gone (the surviving
        # row was kept as-is, by design), and schedules referencing the old SKU
        # now have nothing to match.
        _merged_olds = {str(o).strip() for o, _ in applied_merges}
        _lost = sorted({str(s).strip() for s, _c, _v in diffs} & _merged_olds)
        _lost_note = (
            f" Edits you made on {', '.join(f'`{s}`' for s in _lost)} in this save "
            "were discarded along with the row — re-enter them on the surviving "
            "SKU if you still want them."
            if _lost else ""
        )
        st.warning(
            f"🧹 **Merged duplicate(s): {_mrg_list}.** The row already in the "
            f"catalog was kept unchanged and the duplicate was removed."
            f"{_lost_note} Schedule rows still referencing the removed SKU will "
            "show as *missing SKU* until you re-point or re-upload them."
        )
    if n_added:
        st.info("➕ Added: " + ", ".join(f"`{s}`" for s in added_skus[:12])
                + (" …" if len(added_skus) > 12 else ""))
    if skipped_additions:
        st.warning(
            "⏭️ Already on file, left as-is (added by someone else since your "
            "export): " + ", ".join(f"`{s}`" for s in skipped_additions[:12])
            + (" …" if len(skipped_additions) > 12 else "")
            + ". Export a fresh workbook to update those rows."
        )
    st.caption(
        "In use now. Teammates see the new values after the app redeploys "
        "(~1–2 minutes)."
    )
    return commit_sha or "saved"


def _qty_weighted_means(summary_df: pd.DataFrame) -> "tuple[float, float]":
    """``(avg labor p-min, avg lead-time days)`` per planned UNIT of the Build
    Time summary: each pairing's figure weighted by its ``Qty in schedule``.
    Falls back to the plain mean when the quantities are missing or sum to
    zero; ``(0.0, 0.0)`` for an empty frame."""
    if summary_df is None or summary_df.empty:
        return 0.0, 0.0
    labor = pd.to_numeric(summary_df["Total Labor (p-min)"], errors="coerce").fillna(0.0)
    lead = pd.to_numeric(summary_df["Lead Time (days)"], errors="coerce").fillna(0.0)
    qty = (
        pd.to_numeric(summary_df["Qty in schedule"], errors="coerce").fillna(0.0)
        if "Qty in schedule" in summary_df.columns
        else pd.Series(0.0, index=summary_df.index)
    )
    total = float(qty.sum())
    if total <= 0:
        return float(labor.mean()), float(lead.mean())
    return (
        float((labor * qty).sum() / total),
        float((lead * qty).sum() / total),
    )


@st.fragment
def tab_cycle_time(units, inputs, machine_df=None, acc_df=None):
    # @st.fragment: this tab's only interactive widget is the per-pairing
    # drill-down selectbox, which just changes what's displayed. Isolating it as
    # a fragment means picking a different pairing reruns ONLY this tab instead
    # of the whole script (all other tabs re-rendering). Safe because the tab is
    # pure display — no st.rerun() and no session-state writes that main()
    # consumes, so there's nothing that needs an app-wide rerun.
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW

    st.header("⏱ Build Time per Unit")
    st.markdown(
        "**How long does it take to build one of each unit?**  \n"
        "ℹ️ Lead Time assumes a **single person** building the whole unit start-to-finish. "
        "Real production runs in parallel across stations, so the actual elapsed time is much shorter. "
        "Use lead time to compare units to each other, not to predict ship dates."
    )
    with st.expander("What the columns mean"):
        st.markdown(
            "• **Total Labor** — sum of person-minutes across every station the unit touches.  \n"
            "• **Machine / Accessory Labor** — that total split by source: the FG (machine) "
            "catalog vs the accessory catalog. They add up to Total Labor.  \n"
            "• **(p-hr) columns** — the same figure as the matching (p-min) column, "
            "converted to person-hours (`p-min ÷ 60`); not a separate measurement.  \n"
            "• **Lead Time (days)** — wall-clock days if **one person** built the whole unit "
            f"alone = `Total Labor ÷ ({inputs['shift']} min × efficiency)`.  \n"
            "• **Sum of Cycles** — wall-clock minutes if every station was done sequentially "
            "with your current Crew settings (upper bound — real flow has parallel sub-assembly)."
        )

    shift = inputs["shift"]
    efficiency = max(inputs["efficiency"], 0.01)  # avoid div-by-zero

    # ---- Build the per-pairing summary ----
    summary_rows = []
    detail_rows = []
    for (fg, acc, cls, bat), grp in units.groupby(["fg_base", "acc", "Class", "Bat"]):
        sample = grp.iloc[0]
        total_labor = 0
        sum_cycles = 0.0
        longest_st = None
        longest_cycle = 0.0
        per_station = {}

        for st_key in STATION_KEYS:
            lbr = sample[st_key]
            if lbr == 0:
                continue
            crew = inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"]
            if st_key == "Final" and cls == "HS":
                cycle = lbr / HS_FINAL_CREW
            else:
                cycle = lbr / crew if crew else 0
            total_labor += lbr
            sum_cycles += cycle
            if cycle > longest_cycle:
                longest_cycle = cycle
                longest_st = STATION_KEY_TO_DISPLAY[st_key]
            per_station[st_key] = (int(lbr), round(cycle, 1))

        # Lead time (days) — assumes 1 person, shift × efficiency productive minutes/day
        lead_days = total_labor / (shift * efficiency) if shift > 0 else 0

        # Split the total by source (FG/machine catalog vs accessory catalog).
        machine_lbr = acc_lbr = None
        if machine_df is not None and acc_df is not None:
            split = unit_labor_split(fg, acc or None, machine_df, acc_df)
            if split is not None:
                machine_lbr = int(round(split["machine"]))
                acc_lbr = int(round(split["acc"]))

        summary_rows.append({
            "FG SKU": fg,
            "ACC SKU": acc or "—",
            "Class": cls,
            "Bat": int(bat),
            "Qty in schedule": len(grp),
            "Total Labor (p-min)": int(total_labor),
            "Total Labor (p-hr)": round(total_labor / 60.0, 1),
            "Machine Labor (p-min)": machine_lbr if machine_lbr is not None else int(total_labor),
            "Machine Labor (p-hr)": round((machine_lbr if machine_lbr is not None else total_labor) / 60.0, 1),
            "Acc Labor (p-min)": acc_lbr if acc_lbr is not None else 0,
            "Acc Labor (p-hr)": round((acc_lbr or 0) / 60.0, 1),
            "Lead Time (days)": round(lead_days, 2),
            "Sum of Cycles (min)": round(sum_cycles, 1),
            "Longest Station": longest_st or "—",
            "Longest Cycle (min)": round(longest_cycle, 1),
        })
        detail_rows.append({"key": f"{fg} + {acc or '—'}", "per_station": per_station, "cls": cls, "bat": int(bat)})

    summary_df = pd.DataFrame(summary_rows).sort_values(
        by="Total Labor (p-min)", ascending=False,
    ).reset_index(drop=True)

    # ---- Top metrics (per unit in the schedule) ----
    if not summary_df.empty:
        # Weight each pairing by how many units of it are planned, so the
        # figure is the schedule's labor per unit (Total work ÷ Units planned
        # on the Overview, man-hours / unit on the Executive Summary). The
        # plain mean over pairings counted a one-off and a 60-unit run the
        # same and disagreed with both.
        avg_labor, avg_lead = _qty_weighted_means(summary_df)
        slowest = summary_df.iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Unique unit pairings", f"{len(summary_df)}")
        c2.metric(
            "Avg labor / unit", _fmt_min_hr(avg_labor),
            help="Weighted by the units planned of each pairing — the schedule's "
                 "Total work ÷ Units planned.",
        )
        c3.metric(
            "Avg lead time / unit", f"{avg_lead:.1f} days",
            help="Weighted by the units planned of each pairing.",
        )
        c4.metric(
            "Slowest unit", f"{slowest['FG SKU']}",
            help=f"{_fmt_min_hr(slowest['Total Labor (p-min)'])} · {slowest['Lead Time (days)']} days",
        )

    st.markdown("---")
    st.subheader("📋 Summary — one row per unit pairing")
    st.caption("Sorted by Total Labor descending. Each row = one unique FG + Accessory pairing in the current schedule.")
    st.dataframe(
        summary_df,
        width="stretch",
        height=380,
        hide_index=True,
        column_config={
            "Total Labor (p-min)": st.column_config.NumberColumn(
                "Total Labor (p-min)",
                help="Person-minutes summed across all stations (Machine + Accessory).",
                format="%d",
            ),
            "Total Labor (p-hr)": st.column_config.NumberColumn(
                "Total Labor (p-hr)",
                help="Same as Total Labor (p-min) ÷ 60.",
                format="%.1f",
            ),
            "Machine Labor (p-min)": st.column_config.NumberColumn(
                "Machine Labor (p-min)",
                help="Labor from the FG (machine) catalog: Warehouse, Wire, Trailer, "
                     "Final, PDI, QC, Ship, ETO, and machine-default battery labor.",
                format="%d",
            ),
            "Machine Labor (p-hr)": st.column_config.NumberColumn(
                "Machine Labor (p-hr)",
                help="Machine Labor in person-hours.",
                format="%.1f",
            ),
            "Acc Labor (p-min)": st.column_config.NumberColumn(
                "Acc Labor (p-min)",
                help="Labor from the accessory catalog: Accessory KIT, PM/Gen/Com "
                     "accessories, accessory Warehouse, and accessory-supplied battery labor.",
                format="%d",
            ),
            "Acc Labor (p-hr)": st.column_config.NumberColumn(
                "Acc Labor (p-hr)",
                help="Accessory Labor in person-hours.",
                format="%.1f",
            ),
            "Lead Time (days)": st.column_config.NumberColumn(
                "Lead Time (days)",
                help=f"Total Labor ÷ ({shift} min × {efficiency:.3f} efficiency). "
                     f"How long for one person to build the unit alone.",
                format="%.2f",
            ),
            "Sum of Cycles (min)": st.column_config.NumberColumn(
                "Sum of Cycles (min)",
                help="Sum of cycle times across stations (assumes purely sequential — upper bound).",
                format="%.1f",
            ),
            "Longest Cycle (min)": st.column_config.NumberColumn(
                "Longest Cycle (min)",
                help="Cycle time at the bottleneck station for this unit.",
                format="%.1f",
            ),
        },
    )

    # ---- Per-station drill-down ----
    st.markdown("---")
    st.subheader("🔎 Drill down: per-station breakdown")
    st.caption("Pick a unit pairing to see its labor and cycle time at each station.")

    pairing_choices = [r["key"] for r in detail_rows]
    if pairing_choices:
        chosen = st.selectbox(
            "Unit pairing",
            options=pairing_choices,
            index=0,
        )
        chosen_detail = next(r for r in detail_rows if r["key"] == chosen)

        breakdown_rows = []
        for st_key, (lbr, cycle) in chosen_detail["per_station"].items():
            crew = int(inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"])
            hc_used = HS_FINAL_CREW if (st_key == "Final" and chosen_detail["cls"] == "HS") else crew
            breakdown_rows.append({
                "Station": STATION_KEY_TO_DISPLAY[st_key],
                "Total Labor (p-min)": lbr,
                "Total Labor (p-hr)": round(lbr / 60.0, 2),
                "Crew working in parallel": hc_used,
                "Cycle Time (min)": cycle,
            })
        breakdown_df = pd.DataFrame(breakdown_rows)
        # Defensive: a pairing whose catalog labor is zero at every station
        # produces an empty breakdown_rows → an empty DataFrame with no
        # columns. Skip the table (and the build-summary read-out) with a
        # friendly note instead of crashing on the missing column.
        if breakdown_df.empty:
            st.info(
                f"No labor recorded for **{chosen}** at any station — every "
                "catalog cell for this pairing is blank or zero. Check the "
                "FG / Accessory catalog row, then re-load this tab."
            )
            return
        # Add a TOTAL row
        total_lbr = int(breakdown_df["Total Labor (p-min)"].sum())
        total_cycle = round(breakdown_df["Cycle Time (min)"].sum(), 1)
        total_row = pd.DataFrame([{
            "Station": "🟦 TOTAL",
            "Total Labor (p-min)": total_lbr,
            "Total Labor (p-hr)": round(total_lbr / 60.0, 2),
            "Crew working in parallel": "—",
            "Cycle Time (min)": total_cycle,
        }])
        breakdown_df = pd.concat([breakdown_df, total_row], ignore_index=True)
        # The TOTAL row puts "—" in an otherwise-integer column, making it a
        # mixed-type object column that pyarrow can't serialize (it tries
        # int64). Cast to uniform string so Arrow serializes cleanly — no
        # noisy "Serialization … unsuccessful" warning.
        breakdown_df["Crew working in parallel"] = (
            breakdown_df["Crew working in parallel"].astype(str)
        )

        st.dataframe(
            breakdown_df, width="stretch", hide_index=True, height=460,
            column_config={
                "Total Labor (p-min)": st.column_config.NumberColumn(format="%d"),
                "Total Labor (p-hr)": st.column_config.NumberColumn(format="%.2f"),
                "Cycle Time (min)": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        # Quick read-out for this unit
        lead_days = total_lbr / (shift * efficiency) if shift > 0 else 0
        st.info(
            f"**Build summary for {chosen}:**  \n"
            f"• Total work: **{total_lbr:,} person-minutes** "
            f"(= {total_lbr/60:.1f} person-hours)  \n"
            f"• Lead time (1 person, fully utilized): **{lead_days:.2f} days**  \n"
            f"• Sequential build cycle (with current Crew): **{total_cycle:.0f} cal-min** "
            f"(= {total_cycle/60:.1f} hours, ~{total_cycle/shift:.2f} shifts)"
        )


def _compute_station_depths(nodes, edges):
    """Topological depth per node — used to lay out stages in Graphviz.

    `nodes` = set of station keys that the unit visits.
    `edges` = list of dicts with `From`/`To` keys (already filtered to visited nodes).
    Returns {node: depth}, with depth=0 for nodes that have no incoming edges.
    Nodes in a cycle or unreachable fall back to depth=0 to avoid hanging the layout.
    """
    incoming = {n: [] for n in nodes}
    outgoing = {n: [] for n in nodes}
    for e in edges:
        f, t = e["From"], e["To"]
        if f in nodes and t in nodes:
            incoming[t].append(f)
            outgoing[f].append(t)

    depth = {n: 0 for n in nodes}
    # Topological sort via Kahn's algorithm
    indeg = {n: len(incoming[n]) for n in nodes}
    queue = [n for n in nodes if indeg[n] == 0]
    visited = set()
    while queue:
        n = queue.pop(0)
        if n in visited:
            continue
        visited.add(n)
        for t in outgoing[n]:
            if depth[t] < depth[n] + 1:
                depth[t] = depth[n] + 1
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    return depth


def _fg_model_prefix(sku) -> str:
    """The model part of an FG SKU — what precedes the first ``-`` or space:
    ``SDG65-005`` → ``SDG65``, ``BOSS70HS-001`` → ``BOSS70HS``,
    ``SDG65S XXX`` → ``SDG65S``. Upper-cased; ``""`` for a blank SKU."""
    s = str(sku or "").strip().upper()
    return re.split(r"[-\s]", s, maxsplit=1)[0] if s else ""


def _families_compatible(a: str, b: str) -> bool:
    """Whether two model prefixes name the same product line: equal, or one
    extends the other with a non-digit (``BOSS70`` ↔ ``BOSS70HS``, ``PDS185``
    ↔ ``PDS185EZ``). ``SDG40`` vs ``SDG400`` is NOT a match — the longer one
    continues with a digit, so it is a different model."""
    a = str(a or "").strip().upper()
    b = str(b or "").strip().upper()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return long_.startswith(short) and not long_[len(short)].isdigit()


def _flow_accessory_options(fg_choice, acc_df, units_df=None) -> list[str]:
    """Accessory SKUs the Process Flow tab offers for ``fg_choice``, sorted.

    Every accessory whose family hint (``BOSS25-A016`` → ``BOSS25``,
    ``SDG65-A035`` → ``SDG65``) is compatible with the FG's own model prefix,
    plus whatever the loaded schedule actually pairs with this FG (a numeric
    accessory like ``5000000214`` has no family to match on).

    It used to compare the hint with :func:`_machine_family`, which collapses
    every SDG and PDS model into ``SDG`` / ``PDS`` and every ``BOSS70HS`` into
    ``BOSS70`` — so for two thirds of the catalog the dropdown offered nothing
    but "(none)", and an HS unit could not pick its own HS accessory.
    """
    fg_prefix = _fg_model_prefix(fg_choice)
    found: set[str] = set()
    if acc_df is not None and "SKU" in getattr(acc_df, "columns", []):
        for sku in acc_df["SKU"].astype(str):
            sku = sku.strip()
            if sku and _families_compatible(fg_prefix, _accessory_family_hint(sku)):
                found.add(sku)
    if (
        units_df is not None and not units_df.empty
        and "fg_base" in units_df.columns and "acc" in units_df.columns
    ):
        paired = units_df.loc[
            units_df["fg_base"].astype(str) == str(fg_choice), "acc",
        ]
        for sku in paired.dropna().astype(str):
            sku = sku.strip()
            if sku and sku.lower() not in ("nan", "none"):
                found.add(sku)
    return sorted(found)


@st.fragment
def tab_process_flow(units_df, machine_df, acc_df, inputs):
    # @st.fragment: the FG / Accessory selectboxes only change which unit's
    # route is drawn, so picking another SKU reruns this tab alone. The editor
    # panel's Save / Reset call a plain st.rerun() (app scope) after writing
    # process_flow.json, so the reloaded edges reach every reader.
    """Visual flow chart of a single unit's path through the workstations.

    The graph is built from edges in `data/process_flow.json`, which the user
    can edit through the panel below the diagram. Edges are filtered per unit
    class (HS / HT / STD) so different products can have different shapes.
    Nodes are auto-layered by topological depth — no hardcoded stages.
    """
    from core.labor_calculator import compute_unit_labor, classify_unit
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW

    st.header("🔀 Process Flow")
    st.markdown(
        "**Where does this unit go on the floor?** Pick an FG SKU "
        "(+ optional Accessory) to see the route. The graph is built from "
        "**editable per-class edges** managed in the **🔧 Edit process flow** "
        "panel below — no need to touch any file by hand. Use it to correct the "
        "manufacturing precedence for your facility."
    )

    # ---- Load edges from JSON -------------------------------------------
    all_edges = _load_process_flow_edges(_csv_mtime("process_flow.json"))

    # ---- SKU selectors ---------------------------------------------------
    fg_options = sorted(machine_df["SKU"].astype(str).unique().tolist())
    if not fg_options:
        st.info("No machine SKUs loaded.")
        return

    default_fg = None
    if units_df is not None and not units_df.empty:
        try:
            default_fg = str(units_df.iloc[0]["fg_base"])
        except Exception:
            default_fg = None
    fg_default_idx = fg_options.index(default_fg) if default_fg in fg_options else 0

    c1, c2 = st.columns(2)
    with c1:
        fg_choice = st.selectbox(
            "FG SKU", fg_options, index=fg_default_idx, key="flow_fg",
        )
    with c2:
        acc_options = ["(none)"] + _flow_accessory_options(fg_choice, acc_df, units_df)
        # Open on the accessory the schedule pairs with the default unit, so
        # the first route drawn is a unit that is actually planned.
        _acc_default = 0
        if default_fg is not None and fg_choice == default_fg:
            try:
                _first_acc = str(units_df.iloc[0].get("acc") or "").strip()
            except Exception:
                _first_acc = ""
            if _first_acc in acc_options:
                _acc_default = acc_options.index(_first_acc)
        acc_choice = st.selectbox(
            "Accessory SKU", acc_options, index=_acc_default, key="flow_acc",
        )

    acc_sku = None if acc_choice == "(none)" else acc_choice

    # ---- Compute labor for this pairing ---------------------------------
    labor = compute_unit_labor(fg_choice, acc_sku, machine_df, acc_df)
    if labor is None:
        st.error(f"`{fg_choice}` not found in the machine catalog.")
        return
    cls = labor["Class"]   # "HS" / "HT" / "STD"
    bat = int(labor["Bat"])

    # ---- Filter edges to this unit's class + visited stations ---------
    visited = {k for k in STATION_KEYS if float(labor.get(k, 0) or 0) > 0}
    filtered_edges = [
        e for e in all_edges
        if e.get("Class") in ("All", cls)
        and e.get("From") in visited and e.get("To") in visited
    ]

    if not visited:
        st.warning("This unit has no labor at any station — check the catalog.")
        return

    # ---- Per-station summary ---------------------------------------------
    crew_config = inputs["crew_config"]

    def _crew_for(st_key):
        disp = STATION_KEY_TO_DISPLAY.get(st_key, st_key)
        if disp in crew_config.index:
            try:
                return int(crew_config.loc[disp, "Crew"])
            except Exception:
                return 1
        return 1

    def _cycle(st_key, lbr):
        if lbr <= 0:
            return 0.0
        if st_key == "Final" and cls == "HS":
            return lbr / HS_FINAL_CREW
        crew = _crew_for(st_key)
        return lbr / crew if crew else 0.0

    station_visits = []
    for st_key in STATION_KEYS:
        lbr = float(labor.get(st_key, 0) or 0)
        if lbr <= 0:
            continue
        station_visits.append({
            "key": st_key,
            "name": STATION_KEY_TO_DISPLAY.get(st_key, st_key),
            "labor": int(round(lbr)),
            "cycle": round(_cycle(st_key, lbr), 1),
        })

    # ---- Topological-depth layout ---------------------------------------
    depths = _compute_station_depths(visited, filtered_edges)
    # Group by depth
    by_depth = {}
    for sv in station_visits:
        d = depths.get(sv["key"], 0)
        by_depth.setdefault(d, []).append(sv)
    max_depth = max(by_depth.keys()) if by_depth else 0

    # Color: orange if alone at its depth (sequential); blue if has siblings (parallel)
    SEQ_COLOR = "#FFE4B5"   # light orange
    PAR_COLOR = "#BCD6F2"   # light blue

    def _node_id(st_key: str) -> str:
        return f"n_{st_key}"

    def _node_label(sv) -> str:
        # Show person-mins + person-hours alongside cycle time on each
        # station node. Hours rounded to 1 decimal so 15 p-min reads as
        # "15 p-min · 0.3 p-hr" rather than "0 p-hr".
        _pmin = sv['labor']
        return (
            f"{sv['name']}\\n"
            f"{_pmin} p-min ({_pmin/60:.1f} p-hr) · {sv['cycle']:.0f} cal-min"
        )

    # ---- Build the Graphviz DOT -----------------------------------------
    dot_lines = [
        "digraph G {",
        '  rankdir=TB;',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", margin="0.18,0.10"];',
        '  edge [color="#666"];',
    ]

    for d in sorted(by_depth.keys()):
        group = by_depth[d]
        color = PAR_COLOR if len(group) > 1 else SEQ_COLOR
        if len(group) > 1:
            dot_lines.append(f"  {{ rank=same; // depth {d}")
            for sv in group:
                dot_lines.append(
                    f'    {_node_id(sv["key"])} [label="{_node_label(sv)}", fillcolor="{color}"];'
                )
            dot_lines.append("  }")
        else:
            sv = group[0]
            dot_lines.append(
                f'  {_node_id(sv["key"])} [label="{_node_label(sv)}", fillcolor="{color}"];'
            )

    for e in filtered_edges:
        dot_lines.append(f'  {_node_id(e["From"])} -> {_node_id(e["To"])};')

    dot_lines.append("}")
    dot = "\n".join(dot_lines)

    # ---- Headline metrics + render --------------------------------------
    total_labor = sum(sv["labor"] for sv in station_visits)
    sum_cycles = sum(sv["cycle"] for sv in station_visits)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Class", cls,
              help="HS = head-skid (PM only); HT = head + trailer; STD = standard trailer.")
    c2.metric("Batteries", bat)
    c3.metric("Total labor", _fmt_min_hr(total_labor))
    c4.metric("Stations visited", len(station_visits))

    if filtered_edges:
        st.graphviz_chart(dot, width="stretch")
    else:
        st.info(
            "No edges connect the stations this unit visits — diagram shows nodes "
            "only. Open **🔧 Edit process flow** below to add precedence edges."
        )
        st.graphviz_chart(dot, width="stretch")

    # ---- Per-station breakdown table ------------------------------------
    st.markdown("#### 🔎 Per-station breakdown")
    table_rows = []
    for sv in station_visits:
        d = depths.get(sv["key"], 0)
        same_depth = len(by_depth.get(d, []))
        table_rows.append({
            "Stage / depth": d,
            "Stage type": "Parallel" if same_depth > 1 else "Sequential",
            "Station": sv["name"],
            "Labor (p-min)": sv["labor"],
            "Cycle (cal-min)": sv["cycle"],
            "Crew (parallel)": (
                HS_FINAL_CREW if (sv["key"] == "Final" and cls == "HS")
                else _crew_for(sv["key"])
            ),
        })
    # Sort numerically by depth BEFORE the TOTAL row (which carries non-numeric
    # sentinels) is appended.
    breakdown_df = pd.DataFrame(table_rows).sort_values(by="Stage / depth")
    total_row = pd.DataFrame([{
        "Stage / depth": "",
        "Stage type": "🟦 TOTAL",
        "Station": "All stations",
        "Labor (p-min)": total_labor,
        "Cycle (cal-min)": round(sum_cycles, 1),
        "Crew (parallel)": "—",
    }])
    breakdown_df = pd.concat([breakdown_df, total_row], ignore_index=True)
    # The TOTAL row puts "" / "—" into otherwise-integer columns, making them
    # mixed-type object columns that pyarrow can't serialize (it tries int64).
    # Cast those columns to uniform string so Arrow serializes cleanly.
    for _c in ("Stage / depth", "Crew (parallel)"):
        breakdown_df[_c] = breakdown_df[_c].astype(str)
    st.dataframe(
        breakdown_df, width="stretch", hide_index=True, height=420,
    )

    # ---- Editor panel ----------------------------------------------------
    _render_flow_editor(all_edges)


def _render_flow_editor(edges: list) -> None:
    """Editable table of process-flow edges. Save / Reset push to GitHub."""
    from core.constants import STATION_KEYS

    with st.expander("🔧 Edit process flow", expanded=False):
        # Replay any one-shot toast stashed before rerun
        toast = st.session_state.pop("_flow_toast", None)
        if toast:
            level, msg = toast
            (st.success if level == "success" else st.error)(msg)

        st.caption(
            "Each row is one edge **From → To**. Use **Class = All** for edges that "
            "apply to every unit type, or **STD / HT / HS** for class-specific edges. "
            f"**Valid stations**: {', '.join(STATION_KEYS)}. "
            "**Valid classes**: All, STD, HT, HS."
        )

        # Build a clean seed DataFrame; normalize dtypes to avoid the
        # data_editor type-compat error.
        if edges:
            seed = pd.DataFrame(edges, columns=["From", "To", "Class"])
        else:
            seed = pd.DataFrame(columns=["From", "To", "Class"])
        for c in ("From", "To", "Class"):
            if c in seed.columns:
                seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})

        edited = st.data_editor(
            seed,
            num_rows="dynamic",
            width="stretch",
            key="flow_editor",
            height=420,
            column_config={
                "From": st.column_config.TextColumn(
                    "From", help=f"Station key (one of: {', '.join(STATION_KEYS)})",
                ),
                "To": st.column_config.TextColumn(
                    "To", help=f"Station key (one of: {', '.join(STATION_KEYS)})",
                ),
                "Class": st.column_config.TextColumn(
                    "Class", help="All / STD / HT / HS", width="small",
                ),
            },
        )

        # Live validation feedback
        bad_count = 0
        valid_stations = set(STATION_KEYS)
        for _, row in edited.iterrows():
            f = str(row.get("From", "") or "").strip()
            t = str(row.get("To", "") or "").strip()
            c = str(row.get("Class", "") or "").strip()
            if not f and not t and not c:
                continue  # blank scratch row
            if (f and f not in valid_stations) \
                    or (t and t not in valid_stations) \
                    or (c and c not in PROCESS_FLOW_VALID_CLASSES):
                bad_count += 1
        if bad_count:
            st.warning(
                f"⚠️ {bad_count} row(s) reference unknown station(s) or class. "
                "These will be silently dropped on save."
            )

        c1, c2 = st.columns([3, 1])
        with c1:
            if st.button(
                "💾 Save process flow to GitHub",
                key="flow_save_btn", width="stretch",
            ):
                _flow_save(edited)
        with c2:
            if st.button(
                "🔄 Reset to default",
                key="flow_reset_btn", width="stretch",
            ):
                _flow_reset(current_count=len(edited),
                            default_count=len(PROCESS_FLOW_DEFAULTS))


def _flow_save(edited_df) -> None:
    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner("Saving process flow to GitHub..."):
            response = save_process_flow_to_github(edited_df, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            _load_process_flow_edges.clear()
        except Exception:
            pass
        _track_and_flush("flow_save")
        st.session_state["_flow_toast"] = (
            "success",
            f"✅ Process flow saved"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        st.rerun()
    except Exception as e:
        _show_github_error(e, "Save")


def _flow_reset(current_count: int | None = None, default_count: int | None = None) -> None:
    # Process-flow edges are SHARED across all facilities and all users; a reset
    # replaces every custom edge with the built-in defaults. Require a second
    # click to confirm and spell out what's lost.
    _lost = (
        f"replaces all {current_count} current edge(s) with the "
        f"{default_count} built-in defaults"
        if current_count is not None and default_count is not None
        else "replaces every custom edge with the built-in defaults"
    )
    if not _confirm_destructive(
        "_flow_reset_pending", "reset",
        f"⚠️ Reset the process flow for **all facilities and all users**? This "
        f"{_lost} and can't be undone — click **🔄 Reset to default** again to confirm.",
    ):
        return
    token = _github_token()
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner("Resetting process flow to default..."):
            reset_process_flow_to_default(token)
        try:
            _load_process_flow_edges.clear()
        except Exception:
            pass
        _track_and_flush("flow_reset")
        st.session_state["_flow_toast"] = (
            "success", "🔄 Process flow reset to default.",
        )
        st.rerun()
    except Exception as e:
        _show_github_error(e, "Reset")


@st.fragment
def tab_data_validation(machine_df, acc_df, units=None):
    # @st.fragment: the severity / category multiselect filters below only
    # re-filter the displayed report, so isolating this view means changing a
    # filter reruns ONLY the report instead of the whole script. Pure display —
    # no st.rerun() and no session writes main() consumes.
    st.header("🔍 Data Quality")
    st.caption(
        "Automatic checks over the Machine and Accessory catalogs. Re-runs every "
        "time the app reloads — push updated CSVs to GitHub to clear any issues."
    )

    # ---- Schedule-vs-catalog mismatches (data drops that aren't otherwise visible) ----
    # `expand_schedule` quietly skips schedule rows whose FG isn't in the catalog,
    # and lets units build with zero accessory labor when the accessory isn't found.
    # Surface both here so the planner knows the displayed labor totals are missing
    # those rows.
    skipped_fg: list = []
    unknown_acc: list = []
    if units is not None and hasattr(units, "attrs"):
        skipped_fg = list(units.attrs.get("skipped_fg", []) or [])
        unknown_acc = list(units.attrs.get("unknown_acc", []) or [])

    if skipped_fg:
        st.warning(
            f"⚠️ **{len(skipped_fg)} FG SKU(s) in the schedule are missing from the "
            "machine catalog — their units were dropped from the analysis.**  \n"
            f"Missing: `" + "`, `".join(skipped_fg[:25]) + "`"
            + (f" … (+{len(skipped_fg) - 25} more)" if len(skipped_fg) > 25 else "")
            + "  \nFix by adding these SKUs in **🏭 Machine catalog → ➕ Add a new machine SKU**, "
            "or correct typos in the schedule."
        )
    if unknown_acc:
        st.warning(
            f"⚠️ **{len(unknown_acc)} accessory SKU(s) referenced by the schedule are not "
            "in the accessory catalog — those units were built with zero accessory labor.**  \n"
            f"Missing: `" + "`, `".join(unknown_acc[:25]) + "`"
            + (f" … (+{len(unknown_acc) - 25} more)" if len(unknown_acc) > 25 else "")
            + "  \nFix by adding these SKUs in **🔌 Accessory catalog → ➕ Add a new accessory SKU** "
            "or **➕ Add placeholder accessory (XXX)**."
        )
    if skipped_fg or unknown_acc:
        st.markdown("---")

    issues_df = validate_all(machine_df, acc_df)

    if issues_df.empty:
        if not (skipped_fg or unknown_acc):
            st.success("✅ No issues found in either catalog.")
        else:
            st.info("Catalog content itself looks clean — the warnings above are about schedule rows.")
        return

    # Summary metrics
    err_count = int((issues_df["Severity"] == "🔴 Error").sum())
    warn_count = int((issues_df["Severity"] == "🟡 Warning").sum())
    info_count = int((issues_df["Severity"] == "ℹ️ Info").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("🔴 Errors", err_count, help="Likely bugs — fix these first.")
    c2.metric("🟡 Warnings", warn_count, help="Suspicious — review and confirm.")
    c3.metric("ℹ️ Info", info_count, help="Flags or notes already known about.")

    st.markdown("---")

    # Filter
    sev_filter = st.multiselect(
        "Filter by severity",
        options=["🔴 Error", "🟡 Warning", "ℹ️ Info"],
        default=["🔴 Error", "🟡 Warning", "ℹ️ Info"],
    )
    cat_filter = st.multiselect(
        "Filter by category",
        options=sorted(issues_df["Category"].unique().tolist()),
        default=sorted(issues_df["Category"].unique().tolist()),
    )

    filtered = issues_df[
        issues_df["Severity"].isin(sev_filter) & issues_df["Category"].isin(cat_filter)
    ]
    st.dataframe(filtered, width="stretch", height=600, hide_index=True)

    # Download
    csv = filtered.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download issues CSV",
        csv,
        "catalog_issues.csv",
        "text/csv",
    )


def _tokenize_description(description: str) -> set:
    """Split an accessory description into uppercase tokens (word boundaries)."""
    import re
    if not description:
        return set()
    desc_up = str(description).upper()
    tokens = set(re.split(r"[^A-Z0-9]+", desc_up))
    tokens.discard("")
    return tokens


def _detect_items_in_description(description: str, item_master_df) -> list:
    """Unique items from the master whose Abbr appears as a whole token in the description.

    The master may have multiple rows per Abbr (one default + variants). We
    dedupe so each item appears only once in the detected list.
    """
    if item_master_df is None or item_master_df.empty:
        return []
    tokens = _tokenize_description(description)
    out = []
    seen = set()
    for a in item_master_df["Abbr"]:
        s = str(a).strip()
        if not s:
            continue
        u = s.upper()
        if u in tokens and u not in seen:
            seen.add(u)
            out.append(s)
    return out


def _detect_packages_in_description(description: str, packages_df) -> list:
    """Packages whose code appears as a whole token in the description (e.g. CWP, AWP)."""
    if packages_df is None or packages_df.empty:
        return []
    tokens = _tokenize_description(description)
    return [
        str(p).strip()
        for p in packages_df["Package"]
        if str(p).strip() and str(p).strip().upper() in tokens
    ]


def _expand_and_dedupe(items_found, packages_found, packages_df) -> list:
    """Expand each detected package into its components, union with standalone
    items, and dedupe. Preserves first-seen ordering for readable output.

    Example: items=["EBH"], packages=["AWP"] (AWP = CCV,EH,EBH,BHW,HFF,LRH)
    -> ["CCV", "EH", "EBH", "BHW", "HFF", "LRH"]  (EBH counted once)
    """
    seen = set()
    ordered = []

    def _add(abbr):
        a = str(abbr).strip()
        if not a:
            return
        key = a.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(a)

    # Expand packages first so component ordering reflects the package definition
    if packages_df is not None and not packages_df.empty:
        pkg_lookup = {
            str(r["Package"]).strip().upper(): str(r.get("Components", "") or "")
            for _, r in packages_df.iterrows()
        }
    else:
        pkg_lookup = {}

    for pkg in packages_found or []:
        comps = pkg_lookup.get(str(pkg).strip().upper(), "")
        for comp in [c.strip() for c in comps.split(",") if c.strip()]:
            _add(comp)

    # Then standalone items (deduped against package components)
    for it in items_found or []:
        _add(it)

    return ordered


def _accessory_sides(family_hint: str, sku: str) -> list:
    """Which station side(s) this accessory feeds into.

    - PDS family -> ["Compressor"]
    - SDG / BOSS non-HS -> ["Generator"]
    - HS variants (BOSSxxHS / -HS- in family) -> ["Generator", "PM"] so HS units
      with PM-only accessories still get the PM side.
    """
    fam = str(family_hint or "").upper()
    if fam.startswith("PDS"):
        return ["Compressor"]
    sides = ["Generator"]
    if "HS" in fam or "HS" in str(sku or "").upper():
        sides.append("PM")
    return sides


def _accessory_family_hint(sku: str) -> str:
    """Strip the trailing -A### / -AXXX from an accessory SKU to get the FG family.

    e.g. BOSS25-A016 -> BOSS25;  PDS185EZ-A001 -> PDS185EZ.
    """
    import re
    s = str(sku).strip()
    return re.split(r"-A", s)[0]


# KNOWN_FAMILIES, _machine_family and _machine_class are imported from
# core.sku_routing (top of this file): the routing seeder for new SKUs keys
# on family + class, so the classifiers live next to it.


def _family_battery_count(machine_df, family_prefix: str) -> int:
    """Max `Bat` among machine rows whose SKU starts with `family_prefix`.

    Returns 0 when nothing matches (e.g. PDS / SDG / unknown family) — those
    accessories simply skip the battery multiplier in the Total column.

    The machine catalog's `Bat` column (as loaded by `load_machine_labor()`)
    is the single source of truth for battery counts.
    """
    if not family_prefix or machine_df is None or len(machine_df) == 0:
        return 0
    pf = str(family_prefix).strip().upper()
    if not pf:
        return 0
    skus = machine_df["SKU"].astype(str).str.upper()
    mask = skus.str.startswith(pf)
    if not mask.any():
        return 0
    try:
        return int(machine_df.loc[mask, "Bat"].fillna(0).max())
    except (TypeError, ValueError):
        return 0


def _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc):
    """Reconciliation & Apply — packages expanded, family variants honored, PM included.

    For each accessory:
      1. Detect item abbrevs + package codes in the description
      2. Expand packages -> components, dedupe with standalone items
      3. For each side this accessory feeds, resolve each item's time from the
         unified item master (variant rows override the default row), sum, and
         compare against the aggregate in acc_clean.csv
      4. Let the user select rows to push back into acc_clean.csv.
    """
    st.subheader("🔬 Reconciliation & Apply")
    st.markdown(
        "**Cross-check the catalog aggregate against the item master + packages.**  \n"
        "Packages auto-expand into their components (no double-counting). "
        "Family-specific time variants in the item master override the default row. "
        "When the math looks right, tick rows in the **Apply** column and click "
        "**Apply selected updates** to write the new value back to `acc_clean.csv`."
    )
    st.caption(
        "Side mapping: PDS family → **Compressor** · SDG/BOSS → **Generator** "
        "(plus **PM** for HS-derived accessories). One review row per (SKU, side)."
    )

    cc1, cc2 = st.columns([2, 1])
    with cc1:
        only_used = st.checkbox(
            "Show only accessories used in current schedule", value=True,
            key="recon_only_used",
        )
    with cc2:
        side_filter = st.multiselect(
            "Sides", ["Compressor", "Generator", "PM"],
            default=["Compressor", "Generator", "PM"],
            key="recon_side_filter",
        )

    side_to_agg_col = {"Compressor": "ComAcc", "Generator": "GenAcc", "PM": "PMAcc"}

    rows = []
    for sku in acc_df.index:
        if only_used and used_acc.get(sku, 0) == 0:
            continue
        ar = acc_df.loc[sku]
        desc = str(ar.get("Description", ""))
        family_hint = _accessory_family_hint(sku)

        items_found = _detect_items_in_description(desc, item_master_df)
        pkgs_found = _detect_packages_in_description(desc, item_packages_df)
        expanded = _expand_and_dedupe(items_found, pkgs_found, item_packages_df)

        sides = _accessory_sides(family_hint, sku)
        for side in sides:
            if side not in side_filter:
                continue
            agg_col = side_to_agg_col[side]
            try:
                agg_value = float(ar[agg_col]) if agg_col in acc_df.columns else 0.0
            except (TypeError, ValueError):
                agg_value = 0.0

            item_times = []
            for abbr in expanded:
                t = resolve_item_time(abbr, family_hint, side, item_master_df)
                if t > 0:
                    item_times.append((abbr, t))
            items_sum = sum(t for _, t in item_times)
            breakdown = ", ".join(f"{a}={t:.0f}" for a, t in item_times) or "—"
            pkg_label = ", ".join(pkgs_found) if pkgs_found else ""

            diff = items_sum - agg_value
            if abs(diff) < 0.5:
                status = "✅ Match"
            elif items_sum == 0 and agg_value > 0:
                status = "⚪ No items detected"
            elif items_sum > 0 and agg_value == 0:
                status = "⚠️ Items but no aggregate"
            elif diff > 0:
                status = f"🔺 Items higher (+{diff:.0f})"
            else:
                status = f"🔻 Aggregate higher ({diff:.0f})"

            rows.append({
                "Apply": False,
                "SKU": sku,
                "Family": family_hint,
                "Side": side,
                "Aggregate (catalog)": int(round(agg_value)),
                "Sum of items": int(round(items_sum)),
                "Difference": int(round(diff)),
                "Status": status,
                "Packages": pkg_label,
                "Items detected": breakdown,
                "Description": desc,
                "Used in schedule": used_acc.get(sku, 0),
            })

    if not rows:
        st.info("No accessories to show. Uncheck the filter or widen the side selection.")
        return

    rec_df = pd.DataFrame(rows).sort_values(
        by=["Used in schedule", "Difference"], ascending=[False, True],
    ).reset_index(drop=True)

    # Top metrics
    matched = int((rec_df["Status"] == "✅ Match").sum())
    no_items = int((rec_df["Status"] == "⚪ No items detected").sum())
    mismatches = len(rec_df) - matched - no_items

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Review rows", len(rec_df))
    c2.metric("Matches", matched)
    c3.metric("Mismatches", mismatches)
    c4.metric("No items detected", no_items)

    edited = st.data_editor(
        rec_df,
        width="stretch", height=520, hide_index=True,
        num_rows="fixed",
        key=f"recon_editor_{int(only_used)}_{'-'.join(sorted(side_filter))}",
        column_config={
            "Apply": st.column_config.CheckboxColumn(
                "Apply",
                help="Tick rows whose 'Sum of items' you want to write back as the new aggregate.",
            ),
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Family": st.column_config.TextColumn("Family", disabled=True),
            "Side": st.column_config.TextColumn("Side", disabled=True),
            "Aggregate (catalog)": st.column_config.NumberColumn(
                "Aggregate (catalog)", disabled=True,
                help="Current value in acc_clean.csv.",
            ),
            "Sum of items": st.column_config.NumberColumn(
                "Sum of items", disabled=True,
                help="Sum of item times resolved from the master (family variant wins over default), with packages expanded.",
            ),
            "Difference": st.column_config.NumberColumn("Difference", disabled=True),
            "Status": st.column_config.TextColumn("Status", disabled=True),
            "Packages": st.column_config.TextColumn(
                "Packages", disabled=True,
                help="Package codes detected in the description, before expansion.",
            ),
            "Items detected": st.column_config.TextColumn(
                "Items detected (after expansion)", width="large", disabled=True,
            ),
            "Description": st.column_config.TextColumn("Description", width="large", disabled=True),
        },
    )

    # Apply button
    selected = edited[edited["Apply"] == True]  # noqa: E712
    n_sel = len(selected)
    btn_label = f"💾 Apply {n_sel} selected update(s) to SKU aggregate" if n_sel \
                else "💾 Apply selected updates to SKU aggregate"
    if st.button(btn_label, width="stretch", disabled=(n_sel == 0)):
        _apply_recon_to_acc_csv(selected, acc_df)

    # Download for offline review
    csv = edited.drop(columns=["Apply"]).to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download reconciliation CSV", csv, "accessory_reconciliation.csv", "text/csv",
    )


def _apply_recon_to_acc_csv(selected_df, acc_df):
    """Take the user's selected reconciliation rows and write the new aggregate
    cell values into acc_clean.csv on GitHub.

    Uses the same fresh-fetch + SHA-locked merge pattern as ``_save_catalog_csv``
    so a concurrent edit on an unrelated cell doesn't get clobbered. The
    in-memory ``acc_df`` is only used to compute the diff (old vs. new value)
    — the actual write is layered on top of a freshly-fetched copy from
    GitHub.
    """
    token = _github_token()
    if not token:
        st.error("GitHub token not configured. Ask your admin to add `github_token` to Streamlit Secrets.")
        return

    side_to_agg_col = {"Compressor": "ComAcc", "Generator": "GenAcc", "PM": "PMAcc"}
    file_path = "data/acc_clean.csv"

    # Compute the set of (sku, col, new_val) changes the planner selected, then
    # filter out no-ops (where the catalog already matches the items sum).
    diffs: list[tuple] = []
    for _, sel in selected_df.iterrows():
        sku = sel["SKU"]
        side = sel["Side"]
        col = side_to_agg_col.get(side)
        if not col:
            continue
        try:
            new_val = float(sel["Sum of items"])
        except (TypeError, ValueError):
            continue
        if sku not in acc_df.index or col not in acc_df.columns:
            continue
        try:
            old_val = float(acc_df.at[sku, col]) if pd.notna(acc_df.at[sku, col]) else 0.0
        except (TypeError, ValueError):
            old_val = 0.0
        if abs(new_val - old_val) < 0.5:
            continue
        diffs.append((str(sku), col, new_val))

    if not diffs:
        st.info("No actual changes detected (selected rows already match the catalog).")
        return

    today = today_local_str()
    n_diffs = len(diffs)
    response = None
    for attempt in (1, 2):
        try:
            with st.spinner(
                f"Saving {n_diffs} reconciliation cell(s) to acc_clean.csv"
                + (" — retrying" if attempt == 2 else "") + "..."
            ):
                fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                if fresh_df.empty or "SKU" not in fresh_df.columns:
                    st.error("Could not fetch the current accessory catalog from GitHub.")
                    return
                # Apply each diff to the FRESH frame so any unrelated edits
                # another user just committed stay intact.
                fresh_indexed = fresh_df.set_index("SKU", drop=False)
                if "Last Modified" not in fresh_indexed.columns:
                    fresh_indexed["Last Modified"] = ""
                touched_skus: set = set()
                for sku, col, new_val in diffs:
                    if sku not in fresh_indexed.index or col not in fresh_indexed.columns:
                        continue
                    fresh_indexed.at[sku, col] = new_val
                    fresh_indexed.at[sku, "Last Modified"] = today
                    touched_skus.add(sku)
                csv_text = fresh_indexed.drop(columns=["SKU"], errors="ignore").reset_index().to_csv(index=False)
                response = save_catalog_to_github(
                    csv_text, file_path, token,
                    message=f"Apply reconciliation: update {len(touched_skus)} SKU(s), {n_diffs} cell(s)",
                    sha=fresh_sha,
                )
            break  # success
        except GitHubConflict:
            if attempt == 2:
                st.error(
                    "❌ Save failed: another user committed concurrently. "
                    "Please refresh the page (your selections are not lost — re-check them) and try again."
                )
                return
            continue
        except Exception as e:
            _show_github_error(e, "Save")
            return

    commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
    commit_url = (response or {}).get("commit", {}).get("html_url", "")
    try:
        _clear_catalog_cache(file_path)
    except Exception:
        pass
    _mark_catalog_saved(file_path, response)
    _track_and_flush("recon_apply", file=file_path, cells=n_diffs)
    st.success(
        f"✅ Wrote {n_diffs} updated cell(s) to acc_clean.csv "
        + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
        + "  \nIn use now; teammates see them after the redeploy (~1 min)."
    )


def _render_item_master_view(item_master_df):
    """Unified items table — master defaults, family variants, AND per-accessory
    entries in one editor. Replaces the previous split between Item Master and
    Accessory Item Details views."""
    st.subheader("📒 Items")
    st.markdown(
        "**Unified table** of every installable item — master defaults, "
        "family variants, and per-accessory entries all in one place. Edit "
        "inline and click Save to persist."
    )
    st.caption(
        "Each row defines an item's labor times. Use the **FG family** and "
        "**Accessory SKU** columns to scope the row:\n"
        "- Both blank → **default row** (used when nothing more specific matches)\n"
        "- `FG family` only (e.g. `SDG13`, `PDS185EZ`) → **family variant** (longest prefix wins)\n"
        "- `Accessory SKU` populated → **per-accessory entry** (item belongs to that specific accessory)\n\n"
        "Time columns are person-minutes on each side (Compressor / Generator / PM)."
    )

    # Normalize dtypes so st.data_editor's type checker doesn't complain
    seed = item_master_df.copy()
    for c in ("Abbr", "Description", "FG family", "Accessory SKU", "Notes"):
        if c in seed.columns:
            seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})
    for c in ("Time on Compressor (min)", "Time on Generator (min)", "Time on PM (min)"):
        if c in seed.columns:
            seed[c] = pd.to_numeric(seed[c], errors="coerce").fillna(0).astype(float)

    edited = st.data_editor(
        seed,
        width="stretch",
        num_rows="dynamic",
        key="item_master_editor",
        height=560,
        column_config={
            "Abbr": st.column_config.TextColumn(
                "Abbr", help="Short code used in accessory descriptions (e.g. EBH, BHW, TEL).",
                width="small",
            ),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "FG family": st.column_config.TextColumn(
                "FG family",
                help="Leave blank for a default / per-accessory row. Set to a prefix "
                     "like SDG13 or PDS185EZ to make this row a family variant.",
                width="small",
            ),
            "Accessory SKU": st.column_config.TextColumn(
                "Accessory SKU",
                help="Leave blank for default/family rows. Populate (e.g. `BOSS25-A016`) "
                     "to declare this item belongs to a specific accessory.",
                width="small",
            ),
            "Time on Compressor (min)": st.column_config.NumberColumn(
                "Compressor min", min_value=0, step=1,
                help="Install time on a Compressor product (PDS series).",
            ),
            "Time on Generator (min)": st.column_config.NumberColumn(
                "Generator min", min_value=0, step=1,
                help="Install time on a Generator product (SDG / BOSS series).",
            ),
            "Time on PM (min)": st.column_config.NumberColumn(
                "PM min", min_value=0, step=1,
                help="Install time on a PM (head-skid / HS) unit. Used when the "
                     "accessory feeds the PM Acc station.",
            ),
            "Notes": st.column_config.TextColumn("Notes", width="large"),
        },
    )

    if st.button("💾 Save items to GitHub", width="stretch"):
        _save_simple_csv(edited, "data/item_master.csv", "items")


def _render_item_packages_view(packages_df, item_master_df):
    """Reference table of cold-weather / accessory packages (item bundles)."""
    st.subheader("📦 Item packages")
    st.markdown(
        "**Pre-defined bundles** of items often ordered together (e.g. "
        "Cold Weather Package). Each bundle's Components is a comma-separated "
        "list of item abbreviations from the Item master."
    )

    # Quick check: are all package components defined in the item master?
    if not packages_df.empty and not item_master_df.empty:
        all_abbrs = set(item_master_df["Abbr"].astype(str).str.strip())
        problems = []
        for _, row in packages_df.iterrows():
            comps = [c.strip() for c in str(row.get("Components", "")).split(",") if c.strip()]
            missing = [c for c in comps if c not in all_abbrs]
            if missing:
                problems.append(f"`{row['Package']}` references unknown item(s): {', '.join(missing)}")
        if problems:
            st.warning("⚠️ " + " · ".join(problems))

    # Normalize column dtypes so the editor's type checker is happy
    seed = packages_df.copy()
    for c in ("Package", "Description", "Components", "Notes"):
        if c in seed.columns:
            seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})
    if "Total Time (min)" in seed.columns:
        seed["Total Time (min)"] = pd.to_numeric(seed["Total Time (min)"], errors="coerce").fillna(0).astype(float)

    edited = st.data_editor(
        seed,
        width="stretch",
        num_rows="dynamic",
        key="item_packages_editor",
        height=400,
        column_config={
            "Package": st.column_config.TextColumn("Package code", width="small"),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "Total Time (min)": st.column_config.NumberColumn(
                "Total time (min)", min_value=0, step=1,
                help="Total install time for the whole package.",
            ),
            "Components": st.column_config.TextColumn(
                "Components",
                help="Comma-separated abbreviations from the Item master.",
                width="large",
            ),
            "Notes": st.column_config.TextColumn("Notes"),
        },
    )

    if st.button("💾 Save item packages to GitHub", width="stretch"):
        _save_simple_csv(edited, "data/item_packages.csv", "item packages")


def _save_simple_csv(df, file_path, label):
    """Save a reference-table DataFrame (items / item-packages) as CSV to GitHub.

    These editors are full-table (``num_rows="dynamic"``) with composite,
    non-unique keys, so a safe automatic row-merge isn't possible — a save
    writes the whole file. To avoid SILENTLY reverting a teammate's edit (the
    old version pushed this container's deploy-time table wholesale, with no
    SHA), we:
      1. fetch the latest file from GitHub + its SHA;
      2. if it differs from this container's deploy-time copy (someone changed
         it since we loaded), require an explicit second-click confirm so the
         overwrite is acknowledged, not silent;
      3. push with the fetched SHA as an optimistic lock, retrying once on a
         concurrent-commit conflict;
      4. mirror the saved content locally so a follow-up save in the same
         session doesn't re-trigger the divergence prompt.
    A fetch failure raises (caught below → "Save failed"), so a transient blip
    aborts rather than clobbering.
    """
    token = _github_token()
    if not token:
        st.error("GitHub token not configured. Ask your admin to add `github_token` to Streamlit Secrets.")
        return

    # Clean the user's frame once (drop all-empty rows, strip text cells).
    clean = df.dropna(how="all").copy()
    for c in clean.select_dtypes(include="object").columns:
        clean[c] = clean[c].astype(str).str.strip()
    csv_text = clean.to_csv(index=False)

    _local_path = DATA_DIR / Path(file_path).name

    try:
        fresh_df, fresh_sha = _read_fresh_csv(file_path, token)

        # Did the remote diverge from this container's deploy-time base copy?
        # (Both sides parsed by the same pd.read_csv, so identical content
        # compares equal; only a real change trips this.)
        diverged = False
        if fresh_sha is not None and not fresh_df.empty:
            try:
                base_text = pd.read_csv(_local_path).to_csv(index=False)
                diverged = (fresh_df.to_csv(index=False) != base_text)
            except Exception:
                diverged = False  # can't compare → rely on the SHA lock below

        if diverged and not _confirm_destructive(
            f"_simple_save_confirm_{file_path}", str(fresh_sha),
            f"⚠️ **{label}** was changed by someone else since this page loaded. "
            "Saving overwrites their version with your full table. Reload the "
            f"page to pick up their changes first, or click **💾 Save {label}** "
            "again to overwrite anyway.",
        ):
            return

        # SHA-locked push + one retry on a concurrent-commit conflict.
        for attempt in (1, 2):
            try:
                with st.spinner(f"Saving {label} to GitHub..."):
                    save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Update {label} via app",
                        sha=fresh_sha,
                    )
                break
            except GitHubConflict:
                if attempt == 2:
                    raise
                _, fresh_sha = _read_fresh_csv(file_path, token)  # refetch + retry

        # Mirror locally so the next save this session sees the new base.
        try:
            _local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_local_path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_text)
        except Exception:
            pass

        _track_and_flush("simple_save", file=file_path, label=label)
        st.success(f"✅ Saved {label}.")
        _post_save_redeploy_notice(f"simple_{Path(file_path).stem}")
    except Exception as e:
        _show_github_error(e, "Save")


# =============================================================
# Main
# =============================================================
# ---------------------------------------------------------------------------
# Top-level tab strip.
#
# Labels are module constants so the dispatcher in main(), the tests and the
# AppTest timing harness all spell them the same way.
# ---------------------------------------------------------------------------
TAB_OVERVIEW = "🏠 Overview"
TAB_EXEC_SUMMARY = "📋 Executive Summary"
TAB_CALENDAR = "📆 Calendar"
TAB_CAPACITY = "📊 Capacity"
TAB_RECOMMENDATIONS = "🔧 Recommendations"
TAB_BUILD_TIME = "⏱ Build Time"
TAB_PROCESS_FLOW = "🔀 Process Flow"
TAB_DATA_SETUP = "📁 Data & Setup"
TAB_LABELS = (
    TAB_OVERVIEW, TAB_EXEC_SUMMARY, TAB_CALENDAR, TAB_CAPACITY,
    TAB_RECOMMENDATIONS, TAB_BUILD_TIME, TAB_PROCESS_FLOW, TAB_DATA_SETUP,
)
ACTIVE_TAB_KEY = "_active_tab"
# Non-widget mirror of the strip's choice (see _render_tab_strip).
ACTIVE_TAB_MIRROR_KEY = "_active_tab_last"


def _render_tab_strip() -> str:
    """Draw the top-level tab strip and return the label of the tab in front.

    ``st.tabs`` never tells the server which tab is showing, so main() used to
    build every panel on every rerun. A ``required`` single-select segmented
    control keeps the choice in ``st.session_state[ACTIVE_TAB_KEY]`` and lets
    main() build just that body. The key is seeded before the widget exists
    (the supported way to pick the initial tab; passing ``default`` as well
    would make Streamlit warn that the value was set twice). Widgets inside a
    tab that isn't showing are not instantiated, so their state resets when
    the planner comes back — the Data & Setup view picker remembers itself via
    ``_remember_data_setup_view`` for exactly that reason. The strip remembers
    itself the same way (``ACTIVE_TAB_MIRROR_KEY``): a rerun raised before it
    is built — Sequencer ✅ Accept, 📅 Move into plan month, ➕ Import — wipes
    its widget state, and without the mirror every one of those bounced the
    planner back to Overview.
    """
    _seed = st.session_state.pop(ACTIVE_TAB_SEED_KEY, None)
    if _seed in TAB_LABELS:
        # A main-panel jump (plan guide, Overview actions) asked for this tab;
        # consumed right before the widget is built, like PLAN_MONTH_SEED_KEY.
        st.session_state[ACTIVE_TAB_KEY] = _seed
    if st.session_state.get(ACTIVE_TAB_KEY) not in TAB_LABELS:
        _last = st.session_state.get(ACTIVE_TAB_MIRROR_KEY)
        st.session_state[ACTIVE_TAB_KEY] = (
            _last if _last in TAB_LABELS else TAB_OVERVIEW
        )
    choice = st.segmented_control(
        "Section", TAB_LABELS, selection_mode="single", required=True,
        key=ACTIVE_TAB_KEY, label_visibility="collapsed", width="stretch",
    )
    choice = choice if choice in TAB_LABELS else TAB_OVERVIEW
    st.session_state[ACTIVE_TAB_MIRROR_KEY] = choice
    return choice


# =============================================================
# Plan guide — one panel at the top of the page that walks the workflow
# =============================================================
# Five steps in the order a planner works: what's loaded → which month →
# are the SKUs in the catalogs → does it fit → save it for the team. Each
# step is ONE line with the fact that matters and only the buttons that act
# on it; a step with nothing to do is a plain green line. The facts are
# built by functions that return data (testable without a browser) and
# drawn by one renderer, so the page shows one thing instead of the stack of
# independent banners this replaced. Every button here lives in main(),
# after the sidebar was built, so it may rerun at once (the sidebar's own
# handlers must not — see _defer_rerun).
_GUIDE_ICON = {"ok": "✅", "info": "ℹ️", "todo": "👉", "warn": "⚠️", "error": "❌"}


def _guide_step(n: int, title: str, status: str, fact: str, actions=None,
                details=None, details_title: "str | None" = None, *,
                inline_save: bool = False) -> dict:
    """One row of the guide. ``status`` is ok / info / todo / warn / error;
    ``actions`` are :func:`_guide_action` dicts (at most three are drawn);
    ``details`` are markdown lines for a collapsed expander under the row."""
    return {
        "n": n, "title": title, "status": status, "fact": fact,
        "actions": [a for a in (actions or []) if a],
        "details": list(details or []), "details_title": details_title,
        "details_df": None, "inline_save": inline_save,
    }


def _guide_action(label: str, *, key: str, run, type: str = "secondary",
                  help: "str | None" = None, disabled: bool = False) -> dict:
    """A button on a guide row: ``run`` is called on the click (and reruns)."""
    return {"label": label, "key": key, "run": run, "type": type, "help": help,
            "disabled": bool(disabled)}


def _guide_schedule_step(inputs: dict, schedule_df_full, schedule_load_error) -> dict:
    """Step 1 — what is loaded, how much of the file counts, and the way
    back when something went wrong or was reshaped."""
    location = inputs.get("location", "this facility")
    manual = inputs.get("schedule_mode") == SCHEDULE_MODE_MANUAL
    empty = schedule_df_full is None or schedule_df_full.empty
    if manual:
        if empty:
            return _guide_step(
                1, "Schedule", "todo",
                "Type or paste SKUs in the sidebar (**✏️ Paste or type SKUs**) — "
                "the analysis fills in as you go.",
            )
        n_rows = int(len(schedule_df_full))
        n_units = (
            int(pd.to_numeric(schedule_df_full["BUILD QTY"], errors="coerce").fillna(0).sum())
            if "BUILD QTY" in schedule_df_full.columns else n_rows
        )
        fact = f"**Typed list** · {n_rows} row(s), {n_units} unit(s)"
        actions = []
        if st.session_state.get("_manual_schedule_override") is not None:
            fact += " · reshaped since you typed it"
            actions.append(_guide_action(
                "↩️ Back to the list as typed", key="manual_override_back_btn",
                run=lambda: (_drop_manual_override(), st.rerun()),
                help="Forget the reshape laid over the grid and analyse the rows as typed.",
            ))
        return _guide_step(1, "Schedule", "ok", fact, actions)

    def _run_prev():
        if _restore_schedule_before_error():
            st.session_state["_schedule_toast"] = (
                "success", "⏮ Back to the schedule from before the upload.",
            )
        st.rerun()

    def _run_newest():
        if _queue_newest_saved_load(location):
            st.rerun()

    def _run_sample():
        if _queue_bundled_sample_load():
            st.rerun()

    has_saved = _newest_saved_schedule(location) is not None
    newest_action = _guide_action(
        "📂 Load the newest saved schedule", key="recover_newest_btn", run=_run_newest,
    ) if has_saved else None
    sample_action = _guide_action(
        "🧪 Use the bundled sample", key="recover_sample_btn", run=_run_sample,
    )

    if schedule_load_error:
        has_prev = isinstance(st.session_state.get("_schedule_before_error"), dict)
        return _guide_step(
            1, "Schedule", "error",
            f"**Couldn't read the upload** — {schedule_load_error} Remove the file "
            "from the uploader to go back to what was loaded before, or:",
            [
                _guide_action(
                    "⏮ Back to the previous schedule", key="recover_prev_btn",
                    run=_run_prev,
                    help="Puts back the schedule that was being analysed before this upload.",
                ) if has_prev else None,
                newest_action, sample_action,
            ],
        )

    if empty:
        return _guide_step(
            1, "Schedule", "warn",
            f"**No rows for {location} in this file.** Upload a file with "
            f"{location} rows — or, if the file tags them for another plant, tick "
            "**Count rows tagged for another plant** in the sidebar — or:",
            [newest_action, sample_action],
        )

    summary = st.session_state.get("_last_upload_summary") or {}
    name = summary.get("name")
    source = summary.get("source")
    if source == "saved" and name and summary.get("auto_loaded"):
        who = f"**{name}** — the newest schedule saved for {location}, loaded automatically"
    elif source == "saved" and name:
        who = f"**{name}** (saved schedule)"
    elif source == "upload":
        who = f"**{name or 'Your upload'}** (in this browser only)"
    elif source == "bundled":
        who = f"**Bundled May 2026 sample** — no saved schedules for {location} yet"
    else:
        who = "**Schedule**"
    rows = summary.get("rows", int(len(schedule_df_full)))
    src_rows = summary.get("source_rows")
    counts = (
        f"{rows} of {src_rows} rows counted"
        if isinstance(src_rows, int) and src_rows != rows else f"{rows} rows"
    )
    status = "ok"
    fact = f"{who} · {counts}"
    rec = summary.get("recovered") or {}
    n_dropped = int(sum((rec.get("unmatched") or {}).values())) + int(rec.get("no_model") or 0)
    if n_dropped:
        status = "warn"
        fact += f" · **{n_dropped} unit(s) in the file are not counted**"
    edits = _schedule_edit_count()
    actions = []
    if edits > 0:
        fact += f" · ✏️ {edits} change(s) since load, not saved"
        undo = st.session_state.get("_schedule_undo")
        if isinstance(undo, dict):
            undo_label = undo.get("label") or "the last change"
            actions.append(_guide_action(
                f"↩️ Undo: {undo_label}", key="schedule_undo_btn",
                run=lambda: _undo_last_reshape() and st.rerun(),
                help="Puts the schedule back as it was before that change.",
            ))
        if isinstance(st.session_state.get("_schedule_original"), dict):
            actions.append(_guide_action(
                "⏮ Back to as loaded", key="schedule_restore_btn",
                run=lambda: _restore_schedule_as_loaded() and st.rerun(),
                help=f"Drops all {edits} change(s) made since this schedule was loaded.",
            ))
    details = _schedule_detail_lines(summary, location)
    title = (
        f"Why {rows} of {src_rows} rows? How this file was read"
        if isinstance(src_rows, int) and src_rows != rows else "How this file was read"
    )
    return _guide_step(1, "Schedule", status, fact, actions, details, title)


def _guide_month_step(inputs: dict, schedule_df_full, machine_df, acc_df,
                      window_note: "str | None" = None) -> dict:
    """Step 2 — the Plan month against the month the file is dated for, the
    rows the calendar leaves out, auto-leveled dates, and the time window.
    Carries the "📆 Plan X instead" (seed + rerun) and "📅 Move" (reshape)
    buttons — the one-click answers to "switch the Plan month" and "those
    rows aren't on the calendar"."""
    plan_ym = inputs.get("plan_month")
    plan_lbl = inputs.get("plan_month_label", "the selected month")
    empty = schedule_df_full is None or schedule_df_full.empty
    if not plan_ym or empty:
        return _guide_step(2, "Month", "info", f"Planning **{plan_lbl}**.")

    n_move, carry_total = _plan_month_transfer_counts(schedule_df_full, plan_ym)
    sched_ym = _schedule_dominant_month(schedule_df_full)
    offer_switch = (
        sched_ym is not None
        and (int(sched_ym[0]), int(sched_ym[1])) != (int(plan_ym[0]), int(plan_ym[1]))
    )
    sched_lbl = _month_label(sched_ym) if offer_switch else ""

    parts = [f"Planning **{plan_lbl}**"]
    status = "ok"
    if offer_switch:
        parts.append(f"the file is dated **{sched_lbl}**")
        status = "warn"
    if n_move:
        status = "warn"
        or_switch = (
            f", or plan **{sched_lbl}** instead if that is the month this file is for"
            if offer_switch else
            ", or switch the **Plan month** in the sidebar to match the data"
        )
        parts.append(
            f"**{n_move} row(s) aren't on the {plan_lbl} calendar** — they're dated "
            "in another month or carried over from an earlier month, so the Calendar "
            "and the weekly view leave them out (the totals still count them). Move "
            f"them in{or_switch}"
        )
    elif carry_total:
        parts.append(f"{carry_total} carryover row(s) are scheduled in {plan_lbl}")
    auto = (
        schedule_df_full.attrs.get("auto_spread")
        if hasattr(schedule_df_full, "attrs") else None
    )
    if isinstance(auto, dict) and auto.get("count"):
        parts.append(
            f"{auto['count']} of {auto['total_rows']} rows had no PRODUCTION DAY and "
            f"were auto-leveled across {auto.get('target_month_label', plan_lbl)}"
        )
    if window_note:
        parts.append(window_note)
        if status == "ok":
            status = "warn"
    fact = " · ".join(parts) + "."

    def _run_switch():
        st.session_state[PLAN_MONTH_SEED_KEY] = (int(sched_ym[0]), int(sched_ym[1]))
        _track_and_flush(
            "plan_month_follow_schedule", facility=inputs.get("location"), month=sched_lbl,
        )
        st.rerun()

    def _run_move():
        try:
            new_df, n = _transfer_to_plan_month(
                schedule_df_full, machine_df, acc_df, inputs["location"], plan_ym,
            )
        except Exception as e:  # noqa: BLE001 — shown to the planner
            st.error(f"❌ Move failed: {e}")
            return
        if n:
            _commit_reshape(
                new_df, f"Moved {n} row(s) into {plan_lbl}",
                event="plan_month_transfer", facility=inputs.get("location"), n=n,
            )
            st.rerun()
        else:
            st.info("Nothing to move — all rows already in the plan month.")

    actions = []
    if offer_switch:
        actions.append(_guide_action(
            f"📆 Plan {sched_lbl} instead", key="plan_schedule_month_btn", run=_run_switch,
            help=(
                f"The schedule's own PRODUCTION MONTH is {sched_lbl}. Sets the sidebar's "
                "Plan month to it; the working-day count, the Calendar's weeks and where "
                "undated rows are auto-leveled all follow. Nothing in the schedule itself "
                "changes."
            ),
        ))
    if n_move:
        actions.append(_guide_action(
            f"📅 Move {n_move} row(s) into {plan_lbl}", key="move_into_plan_month_btn",
            run=_run_move, type="primary",
            help=(
                "Clears the off-month / carryover dates and level-loads those rows "
                "across the plan month's working days (auto-spread, seeded by rows "
                "already in-month). Carryover units keep their original production "
                "month, so they stay flagged and are built first. ↩️ Undo is in step 1."
            ),
        ))
    return _guide_step(2, "Month", status, fact, actions)


def _import_missing_skus_now(fg: list, acc: list, token: str) -> None:
    """The ➕ Import click: add every missing SKU to the catalogs as a
    zero-labor placeholder (the save mirrors the new rows into data/), clear
    the loader cache, stash the outcome and rerun — so the guide's step 3 is
    rebuilt from the catalogs as they are now and its units count."""
    added_all: list[str] = []
    skipped_all: list[str] = []
    try:
        with st.spinner("Importing missing SKUs into the catalogs…"):
            if fg:
                a, s = _import_missing_skus_to_catalog(
                    "data/machine_clean.csv", "machine", list(fg), token,
                )
                added_all += a
                skipped_all += s
            if acc:
                a, s = _import_missing_skus_to_catalog(
                    "data/acc_clean.csv", "accessory", list(acc), token,
                )
                added_all += a
                skipped_all += s
    except Exception as e:  # noqa: BLE001 — shown to the planner
        st.error(f"❌ Import failed: {e}")
        return
    try:
        _clear_catalog_cache("data/machine_clean.csv", "data/acc_clean.csv")
    except Exception:
        pass
    _track_and_flush("missing_sku_import", n_added=len(added_all), n_skipped=len(skipped_all))
    if added_all:
        done = list(st.session_state.get("_guide_imported_skus") or [])
        st.session_state["_guide_imported_skus"] = done + [s for s in added_all if s not in done]
        st.session_state["_import_toast"] = (
            "success",
            f"✅ Imported **{len(added_all)}** SKU(s) as zero-labor placeholders — "
            "their units count now. Set their labor times in **📁 Data & Setup → "
            "🏭 Machine catalog** / **🔌 Accessory catalog**."
            + (f"  \n_Skipped {len(skipped_all)} already-present / invalid SKU(s)._"
               if skipped_all else ""),
        )
    else:
        st.session_state["_import_toast"] = (
            "info", "Nothing imported — every missing SKU is already in the catalog.",
        )
    st.rerun()


def _guide_catalog_step(schedule_df_full, machine_df, acc_df, inputs: dict) -> dict:
    """Step 3 — SKUs the catalogs don't know (computed from the schedule, so
    the ➕ Import button is there even when every FG SKU is unknown), units on
    estimation placeholders, and placeholders imported this session that
    still need labor times."""
    _skipped_fg, _unknown_acc = _missing_catalog_skus(schedule_df_full, machine_df, acc_df)
    summary = st.session_state.get("_last_upload_summary") or {}
    rec = summary.get("recovered") or {}
    est = int(rec.get("count") or 0) if isinstance(rec, dict) else 0
    imported = list(st.session_state.get("_guide_imported_skus") or [])
    open_machine = _guide_action(
        "🏭 Open Machine catalog", key="guide_open_catalog_btn",
        run=lambda: _jump_to(TAB_DATA_SETUP, "🏭 Machine catalog"),
        help="📁 Data & Setup → 🏭 Machine catalog, where labor times are edited.",
    )
    open_acc = _guide_action(
        "🔌 Open Accessory catalog", key="guide_open_catalog_btn",
        run=lambda: _jump_to(TAB_DATA_SETUP, "🔌 Accessory catalog"),
        help="📁 Data & Setup → 🔌 Accessory catalog, where labor times are edited.",
    )

    if not _skipped_fg and not _unknown_acc:
        fact = "Every SKU in this plan is in the catalogs"
        actions = []
        status = "ok"
        if est:
            fact += f" · {est} unit(s) run on an estimation placeholder (their labor is an estimate)"
            status = "info"
        if imported:
            fact += (
                f" · **{len(imported)} placeholder(s) imported this session still have "
                "zero labor** — set their labor times"
            )
            status = "info"
            actions.append(_guide_action(
                "✏️ Set their labor times", key="guide_set_labor_btn",
                run=lambda: _jump_to(TAB_DATA_SETUP, "🏭 Machine catalog"),
            ))
        return _guide_step(3, "Catalog", status, fact + ".", actions)

    def _preview(skus):
        return ", ".join(f"`{s}`" for s in skus[:5]) + (
            f" (+{len(skus) - 5} more)" if len(skus) > 5 else ""
        )

    bits = []
    if _skipped_fg:
        bits.append(
            f"**{len(_skipped_fg)} FG SKU(s)** aren't in the machine catalog, so their "
            f"rows are **dropped**: {_preview(_skipped_fg)}"
        )
    if _unknown_acc:
        bits.append(
            f"**{len(_unknown_acc)} accessory SKU(s)** aren't in the accessory catalog, "
            f"so those units build with **zero accessory labor**: {_preview(_unknown_acc)}"
        )
    fact = "; ".join(bits) + ". Import them as zero-labor placeholders, then set their labor times."
    token = _github_token()
    n_missing = len(_skipped_fg) + len(_unknown_acc)
    actions = [
        _guide_action(
            f"➕ Import {n_missing} missing SKU(s) into the catalogs",
            key="guide_import_missing_btn",
            run=lambda: _import_missing_skus_now(_skipped_fg, _unknown_acc, token),
            type="primary", disabled=not token,
            help=(
                "Adds each missing FG SKU to the machine catalog and each missing "
                "accessory SKU to the accessory catalog as a zero-labor placeholder, "
                "committed to GitHub and in use at once."
            ),
        ),
        open_machine if _skipped_fg else open_acc,
    ]
    details = []
    if not token:
        details.append("🔒 Importing needs the GitHub token (`github_token` in Streamlit Secrets).")
    step = _guide_step(3, "Catalog", "warn", fact, actions, details,
                       "The missing SKUs, and the routing the imported ones would get")
    if _skipped_fg:
        # The routing each missing FG SKU would be seeded with, so the planner
        # sees it before clicking import (accessories have no routing).
        _seed_tbl = _routing_seed_preview(
            "data/machine_clean.csv", [{"SKU": s} for s in _skipped_fg],
        )
        if _seed_tbl is not None:
            step["details"].append(
                "🧭 Seeded from the most similar SKU already in the machine catalog "
                "(same family and class) so each plant's custom routing carries over; "
                "`defaults` means every station keeps its own team. Adjust it afterwards "
                "in **📁 Data & Setup → 🏭 Machine catalog**."
            )
            step["details_df"] = _seed_tbl
    return step


def _request_proposal() -> None:
    """The guide's "💡 Level-load the month": the Sequencer generates the
    proposal on the rerun and draws its Accept / Discard panel under the guide."""
    st.session_state["_guide_generate_proposal"] = True
    st.rerun()


def _guide_capacity_step(capacity, inputs: dict, weekly_util, units) -> dict:
    """Step 4 — the verdict: stations over / unstaffed / near capacity, people
    needed vs. on hand, the busiest week — and the controls that fix it."""
    location = inputs.get("location", "this facility")
    if units is None or units.empty or capacity is None or capacity.empty:
        return _guide_step(4, "Capacity", "info", "Load a schedule to see whether it fits.")
    status_col = capacity["overall_status"].astype(str)
    over = [s for s in capacity.index if status_col[s] == "🔴 OVER"]
    missing = [s for s in capacity.index if status_col[s] == "🔴 NO STATION"]
    near = [s for s in capacity.index if status_col[s] == "🟠 NEAR-CAP"]
    prod_hc, _supp_hc, _total_hc = _split_crew_hc(inputs["crew_config"])
    need = int(capacity["required_hc"].sum())
    gap = need - prod_hc
    people = f"need **{need}** people, have **{prod_hc}**" + (f" (short {gap})" if gap > 0 else "")

    def _names(lst):
        return ", ".join(lst[:3]) + (f" +{len(lst) - 3} more" if len(lst) > 3 else "")

    if over or missing:
        status = "error"
        bits = []
        if over:
            bits.append(f"**{len(over)} station(s) over capacity** ({_names(over)})")
        if missing:
            bits.append(f"**{len(missing)} not staffed here** ({_names(missing)})")
        fact = " · ".join(bits) + f" · {people}"
    elif near:
        status = "warn"
        fact = f"**{len(near)} station(s) near capacity** ({_names(near)}) · {people}"
    else:
        status = "ok"
        fact = f"Every station is within capacity · {people}"
    if isinstance(weekly_util, pd.DataFrame) and not weekly_util.empty and weekly_util.shape[1] > 1:
        try:
            peak = float(weekly_util.max().max())
            week = str(weekly_util.max().idxmax())
            if peak > 1.0:
                fact += f" · busiest week **{week}** at {peak:.0%}"
        except Exception:
            pass
    draft = st.session_state.get(f"_crew_draft_{location}")
    has_draft = isinstance(draft, pd.DataFrame)
    if has_draft:
        fact += " · 👥 crew edited, not saved"

    actions = []
    if over or missing or has_draft:
        actions.append(_guide_action(
            "👥 Save crew (sidebar)" if has_draft else "👥 Edit headcount",
            key="guide_edit_crew_btn", run=lambda: _jump_to(None, open_crew=True),
            help="Opens 👥 Station headcount in the sidebar.",
        ))
    if over or missing or near:
        actions.append(_guide_action(
            "🔧 Recommendations", key="guide_recommendations_btn",
            run=lambda: _jump_to(TAB_RECOMMENDATIONS),
        ))
    if (
        isinstance(weekly_util, pd.DataFrame) and not weekly_util.empty
        and st.session_state.get("_proposed_schedule") is None
    ):
        actions.append(_guide_action(
            "💡 Level-load the month", key="guide_level_load_btn", run=_request_proposal,
            help=(
                "Re-spreads every unit across the month's working days (Longest "
                "Processing Time, with a per-station weekly capacity check) and "
                "shows a proposal to accept or discard. Existing PRODUCTION DAY "
                "values are overridden in the proposal."
            ),
        ))
    return _guide_step(4, "Capacity", status, fact + ".", actions)


def _guide_save_step(inputs: dict) -> "dict | None":
    """Step 5 — only while something is unsaved: an upload that lives in this
    browser alone, or reshapes since the schedule was loaded. Draws an inline
    name + 💾 Save (the sidebar's 📂 panel does the same, two expanders down)."""
    if inputs.get("schedule_mode") == SCHEDULE_MODE_MANUAL:
        return None
    summary = st.session_state.get("_last_upload_summary") or {}
    if not summary or st.session_state.get("_active_schedule_error"):
        return None
    source = summary.get("source")
    edits = _schedule_edit_count()
    if source == "upload" and edits:
        what = f"This upload, with {edits} change(s), lives in this browser only"
    elif source == "upload":
        what = "This upload lives in this browser only"
    elif edits:
        what = f"{edits} change(s) since this schedule was loaded aren't saved"
    else:
        return None
    return _guide_step(5, "Save", "todo", f"{what} — name it and save it for the team:",
                       inline_save=True)


def _render_guide_step(step: dict, inputs: dict, uploaded) -> None:
    """Draw one guide row: the fact (a colored box when it needs attention, a
    plain line otherwise), up to three buttons, an optional details expander."""
    status = step.get("status", "ok")
    head = f"**{step['n']}. {step['title']}** — {step['fact']}"
    actions = list(step.get("actions") or [])[:3]
    location = inputs.get("location", "Henderson")

    if step.get("inline_save"):
        c_fact, c_name, c_btn = st.columns([4, 3, 2], vertical_alignment="center")
        with c_fact:
            st.info(head)
        name = c_name.text_input(
            "Schedule name", key=f"_guide_save_name_{location}",
            placeholder="e.g. October plan", label_visibility="collapsed",
        )
        if c_btn.button("💾 Save plan", key="guide_save_plan_btn", width="stretch",
                        type="primary"):
            all_saved = _load_uploaded_schedules_dict(_csv_mtime("uploaded_schedules.json"))
            by_loc = all_saved.get(location, {}) if isinstance(all_saved, dict) else {}
            names = sorted(by_loc.keys()) if isinstance(by_loc, dict) else []
            _uploaded_schedule_save(location, name, uploaded, names, rerun=st.rerun)
        return

    if actions:
        # The fact gives way as buttons are added, so three labels still fit
        # on one line at a laptop width instead of wrapping mid-word.
        fact_width = {1: 6, 2: 5, 3: 3.6}[len(actions)]
        cols = st.columns([fact_width] + [2] * len(actions), vertical_alignment="center")
        c_fact = cols[0]
    else:
        cols = []
        c_fact = st.container()
    with c_fact:
        if status == "warn":
            st.warning(head)
        elif status == "error":
            st.error(head)
        elif status == "todo":
            st.info(head)
        else:
            st.markdown(f"{_GUIDE_ICON.get(status, '')} {head}")
    for col, act in zip(cols[1:], actions):
        kwargs = {"type": act.get("type") or "secondary", "disabled": bool(act.get("disabled"))}
        if act.get("help"):
            kwargs["help"] = act["help"]
        if col.button(act["label"], key=act["key"], width="stretch", **kwargs):
            act["run"]()
    if step.get("details") or step.get("details_df") is not None:
        with st.expander(step.get("details_title") or "Details", expanded=False):
            for line in step.get("details") or []:
                st.markdown(line)
            if step.get("details_df") is not None:
                st.dataframe(step["details_df"], width="stretch", hide_index=True)


def _render_plan_guide(*, inputs: dict, schedule_df_full, units, capacity, weekly_util,
                       machine_df, acc_df, schedule_load_error, window_note,
                       empty_banner_msg, uploaded) -> None:
    """The panel at the top of the page. Drawn by main() once every frame the
    tabs use is known, into the slot reserved above the Sequencer's proposal
    panel; replays the one-shot toasts the reshape / import handlers left."""
    with st.container(border=True):
        st.markdown("##### 🧭 Plan guide")
        for key in ("_schedule_toast", "_import_toast"):
            toast = st.session_state.pop(key, None)
            if isinstance(toast, tuple) and len(toast) == 2:
                (st.success if toast[0] == "success" else st.info)(toast[1])
        steps = [
            _guide_schedule_step(inputs, schedule_df_full, schedule_load_error),
            _guide_month_step(inputs, schedule_df_full, machine_df, acc_df, window_note),
            _guide_catalog_step(schedule_df_full, machine_df, acc_df, inputs),
            _guide_capacity_step(capacity, inputs, weekly_util, units),
        ]
        save_step = _guide_save_step(inputs)
        if save_step is not None:
            steps.append(save_step)
        # A loaded schedule that still produced no units for a reason step 3
        # doesn't name (the time window, say) keeps its explanation.
        if (
            empty_banner_msg
            and schedule_df_full is not None and not schedule_df_full.empty
            and (units is None or units.empty)
            and steps[2]["status"] == "ok"
        ):
            st.info(empty_banner_msg)
        for step in steps:
            _render_guide_step(step, inputs, uploaded)


def _render_action_jumps(over, missing, near) -> None:
    """Under the Overview's recommended actions: the controls they name."""
    if not (over or missing or near):
        return
    c1, c2, _ = st.columns([2, 2, 4])
    if c1.button(
        "👥 Edit headcount", key="overview_edit_crew_btn", width="stretch",
        help="Opens 👥 Station headcount in the sidebar.",
    ):
        _jump_to(None, open_crew=True)
    if c2.button("🔧 Recommendations", key="overview_recommendations_btn", width="stretch"):
        _jump_to(TAB_RECOMMENDATIONS)


def main():
    st.title("🏭 Labor Capacity Planner")
    st.caption(
        "Plan production capacity across facilities. "
        "Set up your scenario in the sidebar, then review the tabs below."
    )

    inputs = render_sidebar()

    # Once-per-session "user opened the tool" event. Force-flush so it lands
    # in usage_log.jsonl within seconds of the first interaction.
    if not st.session_state.get("_session_logged"):
        _track_and_flush(
            "session_start",
            facility=inputs.get("location"),
            mode=inputs.get("schedule_mode"),
        )
        st.session_state["_session_logged"] = True

    # Load data with cache
    machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
    # Project the current facility's routing into legacy column names so
    # compute_unit_labor / build_capacity_table see Henderson's PDI mapping
    # at Henderson and Cypress's PDI mapping at Cypress — without changing
    # those calculators or the cache key.
    machine_df = _project_facility_routing(machine_df, inputs.get("location", "Henderson"))
    acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))
    item_master_df = _load_item_master_df(_csv_mtime("item_master.csv"))
    item_packages_df = _load_item_packages_df(_csv_mtime("item_packages.csv"))

    # ----------------------------------------------------------------
    # Load schedule (manual or CSV). If empty / invalid, we don't return —
    # instead we fall through with empty placeholders so the tabs still
    # render and the user can edit data via the 📁 Data & Setup tab.
    # ----------------------------------------------------------------
    # Display order is fixed here — the plan guide above the Sequencer's
    # proposal panel — while the code below fills the two slots in the order
    # the data needs: the Sequencer's Accept / Discard must run before anything
    # reads the proposal, and the guide needs every frame the tabs will get.
    guide_slot = st.container()
    proposal_slot = st.container()

    schedule_df = pd.DataFrame()
    empty_banner_msg = None
    schedule_load_error = None
    window_note = None

    if inputs.get("schedule_mode") == SCHEDULE_MODE_MANUAL:
        manual_entries = inputs.get("manual_entries")
        if manual_entries is None or manual_entries.empty:
            empty_banner_msg = (
                "✏️ **Manual entry mode** — fill in FG SKU and Quantity in the sidebar to populate "
                "the analysis tabs. You can still use **📁 Data & Setup** to edit catalogs."
            )
        else:
            machine_skus = set(machine_df["SKU"])
            schedule_df = build_manual_schedule(
                manual_entries, location=inputs["location"], machine_skus=machine_skus,
            )
            if schedule_df.empty:
                empty_banner_msg = (
                    "✏️ **Manual entry mode** — fill in at least one row (FG SKU + Quantity > 0) in "
                    "the sidebar. You can still use **📁 Data & Setup** to edit catalogs."
                )
        # Manual rows don't carry a PRODUCTION DAY — the resolver auto-spreads
        # them so the Calendar tab, the Sequencer Suggest button, and the
        # weekly heatmap all light up the same way they do for an uploaded
        # CSV. The sidebar "Plan month" picker is the authoritative target
        # month; fall back to a scenario-name guess only if the picker is
        # somehow missing.
        _hint = inputs.get("plan_month")
        if _hint is None:
            _last_scen = st.session_state.get(
                f"_last_loaded_scenario_name_{inputs['location']}", ""
            )
            _hint = _parse_month_from_name(_last_scen)
        # Same one-shot channel as upload mode: a Sequencer ✅ Accept, 🗓
        # Apply, 🔀 Bulk move, 🧹 Strip or 📅 Move into plan month that just
        # ran lands here as the override for this grid content (and an empty
        # grid drops any pending buffer instead of leaking it into the next
        # mode). See _resolve_manual_schedule.
        schedule_df = _resolve_manual_schedule(
            schedule_df, inputs["location"], machine_df, acc_df, plan_month=_hint,
        )
    else:
        try:
            schedule_df = _load_schedule_df(
                inputs["uploaded"], location=inputs["location"],
                target_month_hint=inputs.get("plan_month"),
            )
        except ScheduleColumnError as e:
            # Planner-friendly message with no internals leaked.
            empty_banner_msg = (
                f"📤 **We couldn't read your uploaded schedule.**  \n{e}  \n"
                "You can still use **📁 Data & Setup** to browse and edit catalogs."
            )
            schedule_load_error = str(e)
            schedule_df = pd.DataFrame()
            # The pill must never describe a schedule that didn't load.
            st.session_state.pop("_last_upload_summary", None)
        if schedule_df.empty and not empty_banner_msg:
            empty_banner_msg = (
                f"⚠️ No schedule rows found for **{inputs['location']}**. "
                "Upload a CSV that contains this location, or select a different location. "
                "You can still use **📁 Data & Setup** to edit catalogs."
            )
        # The schedule for THIS run is now known — fill the sidebar's
        # confirmation pill (and its dropped / rescued-unit warnings) with it.
        _render_upload_summary(
            inputs.get("upload_summary_slot"), inputs["location"], inputs["uploaded"],
        )

    # ----------------------------------------------------------------
    # Sequencer — Suggest button + accept/discard preview. Renders an
    # expander near the top of the page when a schedule is loaded. If
    # the planner has accepted a proposal, the accepted schedule was
    # already pushed into the upload buffer + a rerun fired, so by the
    # time we reach here it's the new sequence we're analyzing.
    # ----------------------------------------------------------------
    with proposal_slot:
        schedule_df = _render_sequencer_controls(
            schedule_df, machine_df, acc_df, inputs,
        )

    # ----------------------------------------------------------------
    # Time-window filter (Step 1 of the actuals/time-dimension work).
    # When the planner picks "This week" or "Remaining (from today)", we
    # subset the schedule to just the rows whose PRODUCTION DAY falls in
    # that window. The full schedule is preserved on `schedule_df_full`
    # for the weekly heatmap rendering.
    # ----------------------------------------------------------------
    # Carryover is Plan-month-relative: a unit whose PRODUCTION MONTH is older
    # than the chosen Plan month has carried over (it must be built in the plan
    # month). Override the loader's format-based flag so the whole app — the
    # carryover-first priority sort, banners, and reports — shares this rule.
    if not schedule_df.empty:
        schedule_df = _recompute_carryover(schedule_df, inputs.get("plan_month"))

    schedule_df_full = schedule_df.copy() if not schedule_df.empty else schedule_df
    if schedule_df.empty or "PRODUCTION DAY" not in schedule_df.columns:
        st.session_state.pop(SCHEDULE_DATED_MONTHS_KEY, None)
    if not schedule_df.empty and "PRODUCTION DAY" in schedule_df.columns:
        window = inputs.get("time_window", "Whole month")
        # Detect the schedule's actual month so "This week" / "Remaining"
        # only fire when today is inside that month. Otherwise the filter
        # would compare today's calendar week (May wk 4) against a future
        # schedule's week numbers (June rows in week 4) — silently wrong.
        _now = now_local()
        _today_date = _now.date()
        _today_ts = pd.Timestamp(_today_date)
        _sched_periods = (
            schedule_df["PRODUCTION DAY"].dropna().dt.to_period("M")
            if "PRODUCTION DAY" in schedule_df.columns else pd.Series(dtype=object)
        )
        _sched_months = {str(p) for p in _sched_periods}
        _dated_months = sorted({(int(p.year), int(p.month)) for p in _sched_periods})
        st.session_state[SCHEDULE_DATED_MONTHS_KEY] = _dated_months
        _today_in_schedule_month = _window_applies(inputs.get("plan_month"), _dated_months)

        if window == "This week":
            if not _today_in_schedule_month:
                window_note = (
                    "📅 **\"This week\" was ignored** — today isn't inside "
                    "the month this plan is dated in. Set the **Time window** "
                    "back to *Whole month*, or pick the schedule's own month "
                    "as the **Plan month**."
                )
            else:
                current_week = week_of_month(_today_date)
                # Key on (today's year, month, week) via PRODUCTION DAY — not
                # on WEEK_OF_MONTH alone, which would also pull in a carryover
                # row from an earlier month's same-numbered week.
                schedule_df = schedule_df[
                    _month_week_mask(schedule_df, _now.year, _now.month, current_week)
                ].copy()
                if schedule_df.empty and not window_note:
                    window_note = (
                        f"📅 **No units scheduled for week {current_week}** "
                        f"in {inputs['location']}'s plan. Switch the **Time window** "
                        "selector in the sidebar back to *Whole month* to see the full plan."
                    )
        elif window == "Remaining (from today)":
            schedule_df = schedule_df[
                schedule_df["PRODUCTION DAY"].notna()
                & (schedule_df["PRODUCTION DAY"] >= _today_ts)
            ].copy()
            if schedule_df.empty and not window_note:
                if not _today_in_schedule_month and _sched_months:
                    window_note = (
                        "📅 **No remaining units from today onward.** "
                        "This schedule is for a different month than today — "
                        "switch back to *Whole month* to see it."
                    )
                else:
                    window_note = (
                        "📅 **No remaining units scheduled from today onward.** "
                        "Switch the **Time window** selector to *Whole month* "
                        "to see the full plan."
                    )

    # ----------------------------------------------------------------
    # Compute analysis frames. Empty-safe: when there are no units, we
    # construct empty placeholders so downstream tabs can detect the
    # empty state and show their own banner instead of crashing.
    # ----------------------------------------------------------------
    if schedule_df.empty:
        units = pd.DataFrame()
    else:
        units = _expand_schedule(schedule_df, machine_df, acc_df)
        if units.empty:
            empty_banner_msg = (
                "⚠️ Could not expand schedule into units. Check that FG / Accessory SKUs "
                "match the catalog. You can edit catalogs in **📁 Data & Setup**."
            )

        # Track one schedule_load event per unique (mode, location, row-count)
        # signature within the session — keeps the log lean even when the user
        # toggles sliders that trigger reruns.
        try:
            _sig = (
                str(inputs.get("schedule_mode") or ""),
                str(inputs.get("location") or ""),
                int(len(schedule_df)),
            )
            if st.session_state.get("_schedule_load_sig") != _sig:
                _track_and_flush(
                    "schedule_load",
                    mode=inputs.get("schedule_mode"),
                    facility=inputs.get("location"),
                    rows=len(schedule_df),
                )
                st.session_state["_schedule_load_sig"] = _sig
        except Exception:
            pass

    if units.empty:
        capacity = pd.DataFrame()
        batt_sku = pd.DataFrame(columns=["fg_base", "batt_type", "units", "batt_per_unit",
                                           "total_batteries", "pct_of_total"])
        batt_type = pd.DataFrame(columns=["batt_type", "total_batteries", "units_count",
                                           "pct_of_total"])
    else:
        capacity = _build_capacity_table_cached(
            units, inputs["crew_config"], inputs["shift"], inputs["days"],
            inputs["safety"], inputs["efficiency"],
            inputs.get("new_hire_pct", 0.0),
            inputs.get("new_hire_productivity", 1.0),
            inputs.get("absenteeism", 0.0),
            inputs.get("overtime", 0.0),
        )
        batt_sku = battery_demand_by_sku(units)
        batt_type = battery_demand_by_type(units)

    # Build the station × week heatmap input — uses the FULL schedule (not
    # the time-window slice) so the planner can spot weekly hotspots even
    # while looking at a sub-window. Returns None when there's no
    # PRODUCTION DAY data, in which case the Overview just skips the chart.
    weekly_util = _weekly_utilization_for(
        schedule_df_full, machine_df, acc_df, inputs,
    )

    # Detect schedule month for display (Mon-YY format rows, not carryover).
    # Only accept a canonical Mon-YY token: undated units that the loader now
    # keeps (a carryover-only slice with no current month to anchor them to)
    # carry CARRYOVER=False but a blank or note PRODUCTION MONTH like
    # "NEED SN/QA" — those must not surface as the plan-month label.
    if schedule_df.empty or "CARRYOVER" not in schedule_df.columns:
        schedule_month = ""
    else:
        _cur = schedule_df.loc[~schedule_df["CARRYOVER"], "PRODUCTION MONTH"]
        current_months = [
            m for m in _cur.unique().tolist()
            if re.match(r"^[A-Za-z]{3}-\d{2}$", str(m))
        ]
        schedule_month = current_months[0] if current_months else ""

    # The plan guide — one panel for everything that used to be a stack of
    # banners above the tabs (what's loaded, the month, the catalogs, the
    # capacity verdict, saving), built from the same frames the tabs receive.
    with guide_slot:
        _render_plan_guide(
            inputs=inputs, schedule_df_full=schedule_df_full, units=units,
            capacity=capacity, weekly_util=weekly_util, machine_df=machine_df,
            acc_df=acc_df, schedule_load_error=schedule_load_error,
            window_note=window_note, empty_banner_msg=empty_banner_msg,
            uploaded=inputs.get("uploaded"),
        )

    # Top-level tabs, ordered from executive summary → planner detail → admin.
    # Batteries content folded into Capacity. Update Labor + Data Quality + Source Data
    # consolidated into the single 📁 Data & Setup tab.
    #
    # Drawn as a segmented control, not st.tabs: st.tabs can't tell the server
    # which panel is in front, so every rerun built all eight bodies and each
    # click cost their sum. The control keeps its selection in session state
    # and main() builds only the body the planner is looking at.
    active_tab = _render_tab_strip()

    # Helper — show an "empty state" message inside analysis tabs when there's no schedule
    def _empty_state(tab_name: str):
        if inputs.get("schedule_mode") == SCHEDULE_MODE_MANUAL:
            _how = (
                "• Type or paste SKUs in the sidebar (**✏️ Paste or type SKUs**), "
                "or switch to **📤 Upload schedule file**."
            )
        else:
            _how = (
                "• Upload a readable schedule, or load a saved one from the "
                "sidebar's **📂 Save / load uploaded schedules** panel — or "
                "switch to **✏️ Paste or type SKUs** for a quick what-if."
            )
        st.info(
            f"📭 **{tab_name} needs a schedule to populate.**  \n{_how}  \n"
            "• You can still edit catalogs, items, packages, and the process flow from the "
            "**📁 Data & Setup** tab without a schedule."
        )

    if active_tab == TAB_OVERVIEW:
        if units.empty:
            _empty_state("Overview")
        else:
            tab_overview(units, capacity, batt_type, inputs, schedule_month, weekly_util=weekly_util)
    elif active_tab == TAB_EXEC_SUMMARY:
        # Executive Summary — renders its own info banner when there's no
        # live schedule, and still shows saved cross-facility scenarios.
        tab_exec_summary(units, capacity, inputs, total_units=len(units))
    elif active_tab == TAB_CALENDAR:
        # Calendar — week & day rollups. ALWAYS use the full (unfiltered)
        # schedule here so the planner sees the entire month's per-week
        # rollup regardless of the sidebar Time-window setting. The helper
        # also handles the empty case with its own info banner.
        _render_calendar_view(
            schedule_df_full, machine_df, acc_df,
            time_window=inputs.get("time_window", "Whole month"),
            plan_month=inputs.get("plan_month"),
            plan_month_label=inputs.get("plan_month_label", ""),
        )
    elif active_tab == TAB_CAPACITY:
        if units.empty:
            _empty_state("Capacity")
        else:
            tab_capacity_vs_demand(capacity, inputs, batt_sku=batt_sku, batt_type=batt_type, total_units=len(units), units=units)
    elif active_tab == TAB_RECOMMENDATIONS:
        if units.empty:
            _empty_state("Recommendations")
        else:
            tab_mitigation(capacity, batt_sku, units, inputs)
    elif active_tab == TAB_BUILD_TIME:
        if units.empty:
            _empty_state("Build Time")
        else:
            tab_cycle_time(units, inputs, machine_df, acc_df)
    elif active_tab == TAB_PROCESS_FLOW:
        # Process Flow can show the editable flow even without a schedule
        tab_process_flow(units, machine_df, acc_df, inputs)
    elif active_tab == TAB_DATA_SETUP:
        tab_data_setup(
            machine_df=machine_df, acc_df=acc_df, schedule_df=schedule_df,
            item_master_df=item_master_df, item_packages_df=item_packages_df,
            units=units, schedule_df_full=schedule_df_full,
            location=inputs.get("location", "Henderson"),
            capacity=capacity, inputs=inputs,
        )

    # --- Footer ---------------------------------------------------------
    # Lightweight provenance + support line at the bottom of every page.
    st.markdown("---")
    st.caption(
        "Questions or issues? Contact "
        "[tnguyen@anacorp.com](mailto:tnguyen@anacorp.com)"
    )


if __name__ == "__main__":
    main()
