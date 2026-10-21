"""Excel bridge — read planner workbooks in, write the whole database out.

Two jobs, both Streamlit-free so they're unit-testable:

1. **Inbound** — :func:`upload_to_schedule_csv` turns an uploaded ``.xlsx`` /
   ``.xlsm`` workbook into the CSV bytes the schedule loader already
   understands. It scans every sheet for the first header row that carries
   the five required schedule columns (so a title row above the header, or
   the schedule living on the third tab, both just work) and normalizes Excel
   cell types (dates → ``YYYY-MM-DD``, ``5.0`` → ``5``) on the way through.
   A ``.csv`` passes straight through untouched, so every downstream path —
   the session's active schedule, the 💾 Save schedule button, the saved
   schedule JSON — keeps seeing CSV and nothing else has to change.

2. **Outbound** — :func:`database_tables` flattens every file under ``data/``
   (the four CSV catalogs plus the crew / settings / scenarios / executive
   scenarios / saved schedules / process-flow JSON) into plain DataFrames, and
   :func:`build_workbook` writes any ``{sheet: DataFrame}`` map to a single
   ``.xlsx`` with bold frozen headers and autofilters. The app uses it twice:
   once for the whole database, once for the current session's analysis
   (schedule / per-unit labor / capacity table).

3. **Write-back** — :func:`read_catalog_workbook` finds the ``Machine catalog``
   / ``Accessory catalog`` sheets in an uploaded workbook (the export, or a
   planner's own single-sheet file) and :func:`diff_catalog_sheet` turns a
   sheet into the same per-cell diffs the in-app editor saves, so Excel edits
   go through the app's fetch-fresh + SHA-locked merge instead of replacing
   the file.

:func:`raw_github_url` and :func:`power_query_m_for_csv` back the "link Excel
to the live data" panel — the data files are plain CSV/JSON on GitHub, so
Excel's *Get Data → From Web* can read them directly and refresh on demand.
"""
from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from .catalog_storage import GITHUB_BRANCH, GITHUB_REPO
from .data_loader import (
    CATALOG_COLUMN_ALIASES,
    ScheduleColumnError,
    _REQUIRED_SCHEDULE_COLUMNS,
    _find_column,
    canonical_catalog_column,
)
from .exec_scenario_storage import load_all_exec_scenarios
from .facility_settings_storage import load_all_facility_settings
from .facility_storage import load_all_facility_configs
from .process_flow_storage import load_process_flow
from .scenario_storage import load_all_scenarios
from .uploaded_schedule_storage import load_all_uploaded_schedules

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: Workbook extensions the uploader accepts. ``.xls`` (the pre-2007 binary
#: format) needs the ``xlrd`` package, which isn't a dependency — planners
#: with one of those save it as ``.xlsx`` first.
EXCEL_EXTENSIONS = (".xlsx", ".xlsm")

#: How many leading rows of a sheet to scan for the header row. Real-world
#: exports put a title, a date, and a blank line above the column names; a
#: header deeper than this is almost certainly not a schedule sheet.
HEADER_SCAN_ROWS = 20

#: Excel caps sheet names at 31 characters and bans these characters.
_SHEET_NAME_BAD_CHARS = re.compile(r"[\[\]:*?/\\]")
_SHEET_NAME_MAX = 31


# ---------------------------------------------------------------------------
# Inbound — workbook → schedule CSV bytes
# ---------------------------------------------------------------------------
def is_excel_filename(name) -> bool:
    """True when ``name`` ends in an extension :func:`upload_to_schedule_csv`
    will open as a workbook (case-insensitive)."""
    return str(name or "").strip().lower().endswith(EXCEL_EXTENSIONS)


def _cell_to_text(value) -> str:
    """Render one Excel cell the way a CSV export would.

    * blanks / NaN / NaT → ``""``
    * dates and datetimes → ``YYYY-MM-DD`` (the loader parses this for both
      PRODUCTION MONTH and PRODUCTION DAY; the midnight time part a date cell
      carries is noise)
    * whole-number floats → ``"5"`` not ``"5.0"`` (BUILD QTY reads cleaner in
      the saved CSV; the loader coerces either)
    * everything else → ``str(value)``, stripped
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass  # non-scalar (shouldn't happen for a cell) — fall through
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _row_matches_schedule_header(cells: list) -> bool:
    """True when every required schedule column fuzzy-matches one of ``cells``."""
    names = [_cell_to_text(c) for c in cells]
    names = [n for n in names if n]
    if not names:
        return False
    return all(
        _find_column(names, *patterns) is not None
        for _canonical, patterns, _label in _REQUIRED_SCHEDULE_COLUMNS
    )


def find_schedule_header_row(raw: pd.DataFrame) -> int | None:
    """Index of the first row (within :data:`HEADER_SCAN_ROWS`) that looks like
    a schedule header, or ``None`` if the sheet has no such row.

    ``raw`` is a sheet read with ``header=None`` so row 0 is the first
    spreadsheet row.
    """
    if raw is None or raw.empty:
        return None
    limit = min(len(raw), HEADER_SCAN_ROWS)
    for i in range(limit):
        if _row_matches_schedule_header(raw.iloc[i].tolist()):
            return i
    return None


def _sheet_to_csv_bytes(raw: pd.DataFrame, header_row: int) -> bytes:
    """Re-head ``raw`` at ``header_row``, drop empty rows, stringify cells, and
    serialize as UTF-8 CSV."""
    header = [_cell_to_text(c) for c in raw.iloc[header_row].tolist()]
    # Blank header cells get a stable placeholder so two of them don't collide
    # into one duplicate "" column (pandas would do the same on read_csv).
    header = [h if h else f"Unnamed: {i}" for i, h in enumerate(header)]
    body = raw.iloc[header_row + 1:].copy()
    body.columns = header
    body = body.dropna(how="all")
    for col in list(body.columns):
        body[col] = body[col].map(_cell_to_text)
    # A row that's blank in every cell after stringifying is padding — drop it.
    body = body[(body != "").any(axis=1)]
    return body.to_csv(index=False).encode("utf-8")


def excel_to_schedule_csv(raw_bytes: bytes, filename: str = "") -> tuple[bytes, str]:
    """Convert an ``.xlsx`` / ``.xlsm`` workbook to schedule CSV bytes.

    Returns ``(csv_bytes, sheet_name)`` for the first sheet, in workbook order,
    that has a header row carrying all five required schedule columns.

    Raises :class:`ScheduleColumnError` (the same planner-friendly error the
    CSV loader raises, so the upload UI surfaces it the same way) when the
    file can't be opened as a workbook or no sheet has a usable header.
    """
    label = f"**{filename}**" if filename else "the workbook"
    try:
        # ExcelFile as a context manager so the underlying zip is closed
        # deterministically (a bare read_excel on a BytesIO leaves it to the
        # GC and trips a ResourceWarning on Python 3.13).
        with pd.ExcelFile(io.BytesIO(raw_bytes), engine="openpyxl") as xf:
            book = {
                name: xf.parse(name, header=None, dtype=object)
                for name in xf.sheet_names
            }
    except Exception as exc:  # noqa: BLE001 — surface whatever openpyxl said
        raise ScheduleColumnError(
            f"We couldn't open {label} as an Excel workbook ({exc}). "
            "Save it as .xlsx (or export the sheet as CSV) and re-upload."
        ) from exc
    if not book:
        raise ScheduleColumnError(f"{label} has no sheets.")

    skipped: list[str] = []
    for sheet_name, raw in book.items():
        header_row = find_schedule_header_row(raw)
        if header_row is None:
            skipped.append(str(sheet_name))
            continue
        return _sheet_to_csv_bytes(raw, header_row), str(sheet_name)

    tried = ", ".join(f"`{s}`" for s in skipped) or "—"
    raise ScheduleColumnError(
        f"None of the sheets in {label} ({tried}) has a header row with the "
        "columns we need: **Location, FG SKU ID, Accessory SKU ID, Build Qty, "
        "Production Month** (any of their usual spellings). Put those column "
        "names in a single row on the schedule sheet and re-upload."
    )


def upload_to_schedule_csv(filename: str, raw_bytes: bytes) -> tuple[bytes, str | None]:
    """Normalize an uploaded schedule file to CSV bytes.

    * Excel workbook → converted via :func:`excel_to_schedule_csv`; returns
      ``(csv_bytes, sheet_name)``.
    * Anything else (CSV) → passed through untouched; returns
      ``(raw_bytes, None)``.
    """
    if is_excel_filename(filename):
        return excel_to_schedule_csv(raw_bytes, filename)
    return raw_bytes, None


# ---------------------------------------------------------------------------
# Outbound — every data file as a flat table
# ---------------------------------------------------------------------------
#: (sheet name, source file under data/, one-line description). Order is the
#: sheet order in the exported workbook and the row order in the link table.
DATABASE_TABLES: list[tuple[str, str, str]] = [
    ("Machine catalog", "machine_clean.csv",
     "FG SKU labor minutes by station, battery count, per-facility routing"),
    ("Accessory catalog", "acc_clean.csv",
     "Accessory SKU labor minutes by station"),
    ("Items", "item_master.csv",
     "Item master — defaults, family variants, per-accessory entries"),
    ("Item packages", "item_packages.csv",
     "Named item bundles (CWP / NE / AWP) and their components"),
    ("Crew", "facility_crew.json",
     "Headcount / concurrency / crew per station, one row per facility × station"),
    ("Facility settings", "facility_settings.json",
     "Per-facility planning assumptions (new-hire share, absenteeism, efficiency…)"),
    ("Scenarios", "scenarios.json",
     "Saved manual build plans — one row per SKU line"),
    ("Exec scenarios", "exec_scenarios.json",
     "Saved executive summaries — assumptions and frozen metrics, one row per value"),
    ("Saved schedules", "uploaded_schedules.json",
     "Index of saved uploaded schedules (facility, name, saved at, rows)"),
    ("Saved schedule rows", "uploaded_schedules.json",
     "Every row of every saved uploaded schedule"),
    ("Process flow", "process_flow.json",
     "Station precedence edges by unit class"),
    ("Sample schedule", "may_schedule.csv",
     "Bundled sample schedule (scrubbed)"),
]

#: Files Excel can link to with one *From Web* step (plain CSV). The JSON
#: files need a few Power Query transforms — the exported workbook already
#: has them flattened, so the link table points planners there instead.
CSV_LINKABLE_FILES = (
    "machine_clean.csv", "acc_clean.csv", "item_master.csv",
    "item_packages.csv", "may_schedule.csv",
)


def raw_github_url(file_name: str, repo: str = GITHUB_REPO,
                   branch: str = GITHUB_BRANCH) -> str:
    """Raw-content URL for ``data/<file_name>`` on GitHub — what Excel's
    *From Web* connector reads."""
    return f"https://raw.githubusercontent.com/{repo}/{branch}/data/{file_name}"


def power_query_m_for_csv(url: str) -> str:
    """A ready-to-paste Power Query (M) query that loads a UTF-8 CSV from
    ``url`` and promotes the first row to headers."""
    return (
        "let\n"
        f'    Source = Csv.Document(Web.Contents("{url}"),\n'
        "        [Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true])\n"
        "in\n"
        "    Headers"
    )


def _read_csv_raw(path: Path) -> pd.DataFrame:
    """Read a catalog CSV exactly as stored (all text, blanks kept) so the
    export round-trips the file byte-for-byte in spirit — no loader-side
    renames, fills or derived columns."""
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def crew_table(cfg: dict | None) -> pd.DataFrame:
    """``facility_crew.json`` → Facility / Station / HC / Conc / Crew rows."""
    rows = []
    for facility, stations in (cfg or {}).items():
        for station, vals in (stations or {}).items():
            vals = vals or {}
            rows.append({
                "Facility": facility, "Station": station,
                "HC": vals.get("HC"), "Conc": vals.get("Conc"), "Crew": vals.get("Crew"),
            })
    return pd.DataFrame(rows, columns=["Facility", "Station", "HC", "Conc", "Crew"])


def settings_table(settings: dict | None) -> pd.DataFrame:
    """``facility_settings.json`` → Facility / Setting / Value rows (lists are
    joined with ``; `` so they stay one cell)."""
    rows = []
    for facility, vals in (settings or {}).items():
        for key, val in (vals or {}).items():
            if isinstance(val, (list, tuple, set)):
                val = "; ".join(str(v) for v in val)
            elif isinstance(val, dict):
                val = json.dumps(val)
            rows.append({"Facility": facility, "Setting": key, "Value": val})
    return pd.DataFrame(rows, columns=["Facility", "Setting", "Value"])


def scenarios_table(scenarios: dict | None) -> pd.DataFrame:
    """``scenarios.json`` → one row per saved SKU line, tagged with facility,
    scenario name and save timestamp."""
    rows = []
    for facility, by_name in (scenarios or {}).items():
        for name, block in (by_name or {}).items():
            block = block or {}
            saved_at = block.get("saved_at", "")
            entries = block.get("entries") or []
            if not entries:
                rows.append({"Facility": facility, "Scenario": name, "Saved at": saved_at})
                continue
            for entry in entries:
                row = {"Facility": facility, "Scenario": name, "Saved at": saved_at}
                row.update(entry or {})
                rows.append(row)
    df = pd.DataFrame(rows)
    lead = [c for c in ("Facility", "Scenario", "Saved at") if c in df.columns]
    rest = [c for c in df.columns if c not in lead]
    return df[lead + rest] if not df.empty else pd.DataFrame(
        columns=["Facility", "Scenario", "Saved at", "FG SKU", "Accessory SKU", "Qty"])


def exec_scenarios_table(exec_scenarios: dict | None) -> pd.DataFrame:
    """``exec_scenarios.json`` → long format: one row per assumption or
    metric value. Long beats wide here — the metric set has grown over time,
    and a key/value layout pivots cleanly in Excel without blank columns."""
    rows = []
    for facility, by_name in (exec_scenarios or {}).items():
        for name, block in (by_name or {}).items():
            block = block or {}
            saved_at = block.get("saved_at", "")
            for section_key, section_label in (("assumptions", "Assumption"),
                                                ("metrics", "Metric")):
                for key, val in (block.get(section_key) or {}).items():
                    if isinstance(val, (dict, list, tuple, set)):
                        val = json.dumps(val, default=str)
                    rows.append({
                        "Facility": facility, "Scenario": name, "Saved at": saved_at,
                        "Section": section_label, "Name": key, "Value": val,
                    })
    return pd.DataFrame(
        rows, columns=["Facility", "Scenario", "Saved at", "Section", "Name", "Value"])


def saved_schedules_index(uploaded: dict | None) -> pd.DataFrame:
    """``uploaded_schedules.json`` → one row per saved schedule."""
    rows = []
    for facility, by_name in (uploaded or {}).items():
        for name, block in (by_name or {}).items():
            block = block or {}
            rows.append({
                "Facility": facility, "Schedule": name,
                "Saved at": block.get("saved_at", ""), "Rows": block.get("rows"),
            })
    return pd.DataFrame(rows, columns=["Facility", "Schedule", "Saved at", "Rows"])


def saved_schedule_rows(uploaded: dict | None) -> pd.DataFrame:
    """``uploaded_schedules.json`` → every saved schedule's rows stacked, with
    Facility + Schedule prepended. Columns are the union across schedules;
    all values stay text exactly as the planner's CSV carried them."""
    frames = []
    for facility, by_name in (uploaded or {}).items():
        for name, block in (by_name or {}).items():
            csv_text = (block or {}).get("csv_content") or ""
            if not csv_text.strip():
                continue
            try:
                df = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
            except Exception:  # noqa: BLE001 — one bad blob shouldn't sink the export
                continue
            df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]
            df.insert(0, "Schedule", name)
            df.insert(0, "Facility", facility)
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["Facility", "Schedule"])
    return pd.concat(frames, ignore_index=True, sort=False)


def process_flow_table(edges: list | None) -> pd.DataFrame:
    """``process_flow.json`` edges → From / To / Class rows."""
    df = pd.DataFrame(edges or [])
    if df.empty:
        return pd.DataFrame(columns=["From", "To", "Class"])
    lead = [c for c in ("From", "To", "Class") if c in df.columns]
    return df[lead + [c for c in df.columns if c not in lead]]


#: The one sheet that dominates export time and file size: every saved
#: schedule's rows (thousands of rows × every ERP column the planner's CSV
#: carried). Callers can leave it out for a fast "catalogs + config" export.
SAVED_SCHEDULE_ROWS_SHEET = "Saved schedule rows"


def database_tables(data_dir: Path | str | None = None, *,
                    include_saved_schedule_rows: bool = True) -> dict[str, pd.DataFrame]:
    """Every data file as a flat DataFrame, keyed by sheet name in
    :data:`DATABASE_TABLES` order.

    ``data_dir`` overrides the repo's ``data/`` for the CSVs (tests); the JSON
    loaders always read the repo copy, which is the same thing in production.
    ``include_saved_schedule_rows=False`` skips :data:`SAVED_SCHEDULE_ROWS_SHEET`
    (the index of saved schedules is always included).
    """
    data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
    crew = load_all_facility_configs()
    settings = load_all_facility_settings()
    scenarios = load_all_scenarios()
    exec_scen = load_all_exec_scenarios()
    uploaded = load_all_uploaded_schedules()
    edges = load_process_flow()
    builders = {
        "Machine catalog": lambda: _read_csv_raw(data_dir / "machine_clean.csv"),
        "Accessory catalog": lambda: _read_csv_raw(data_dir / "acc_clean.csv"),
        "Items": lambda: _read_csv_raw(data_dir / "item_master.csv"),
        "Item packages": lambda: _read_csv_raw(data_dir / "item_packages.csv"),
        "Crew": lambda: crew_table(crew),
        "Facility settings": lambda: settings_table(settings),
        "Scenarios": lambda: scenarios_table(scenarios),
        "Exec scenarios": lambda: exec_scenarios_table(exec_scen),
        "Saved schedules": lambda: saved_schedules_index(uploaded),
        "Saved schedule rows": lambda: saved_schedule_rows(uploaded),
        "Process flow": lambda: process_flow_table(edges),
        "Sample schedule": lambda: _read_csv_raw(data_dir / "may_schedule.csv"),
    }
    return {
        sheet: builders[sheet]()
        for sheet, _file, _desc in DATABASE_TABLES
        if include_saved_schedule_rows or sheet != SAVED_SCHEDULE_ROWS_SHEET
    }


def database_readme(branch: str = GITHUB_BRANCH, *,
                    include_saved_schedule_rows: bool = True) -> pd.DataFrame:
    """The README sheet for the database export: what each sheet is, which
    file it came from, and the raw URL Excel can link to."""
    rows = []
    for sheet, file_name, desc in DATABASE_TABLES:
        if not include_saved_schedule_rows and sheet == SAVED_SCHEDULE_ROWS_SHEET:
            continue
        rows.append({
            "Sheet": sheet,
            "Source file": f"data/{file_name}",
            "What it holds": desc,
            "Live link (Excel → Data → From Web)":
                raw_github_url(file_name, branch=branch)
                if file_name in CSV_LINKABLE_FILES else "— JSON; use this sheet —",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Workbook writer
# ---------------------------------------------------------------------------
def safe_sheet_name(name: str, taken: set | None = None) -> str:
    """Excel-legal sheet name: banned characters stripped, 31-char cap, and
    a numeric suffix if ``taken`` already holds it."""
    base = _SHEET_NAME_BAD_CHARS.sub("", str(name or "Sheet")).strip() or "Sheet"
    base = base[:_SHEET_NAME_MAX]
    if taken is None:
        return base
    candidate, n = base, 2
    while candidate in taken:
        suffix = f" ({n})"
        candidate = base[:_SHEET_NAME_MAX - len(suffix)] + suffix
        n += 1
    return candidate


def _excel_safe_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Make a frame openpyxl can write: tz-aware datetimes → naive, non-scalar
    objects (lists/dicts) → JSON text, index folded in when it's named."""
    if df is None:
        return pd.DataFrame()
    out = df.copy()
    if out.index.name is not None or isinstance(out.index, pd.MultiIndex):
        # Fold a named index in as leading columns; rename any level whose
        # name collides with an existing column so reset_index can't raise.
        names = [n if n is not None else f"level_{i}" for i, n in enumerate(out.index.names)]
        names = [f"{n} (index)" if n in out.columns else n for n in names]
        out.index = out.index.set_names(names)
        out = out.reset_index()
    # Duplicate column labels break both the per-column pass below (``out[c]``
    # would be a frame, not a series) and to_excel's header write — so
    # disambiguate first.
    if out.columns.duplicated().any():
        seen: dict = {}
        cols = []
        for c in out.columns:
            n = seen.get(c, 0)
            cols.append(c if n == 0 else f"{c}.{n}")
            seen[c] = n + 1
        out.columns = cols
    for col in out.columns:
        s = out[col]
        if isinstance(s.dtype, pd.DatetimeTZDtype):
            out[col] = s.dt.tz_localize(None)
        elif s.dtype == object:
            out[col] = s.map(
                lambda v: json.dumps(v, default=str)
                if isinstance(v, (list, dict, set, tuple)) else v
            )
    return out


def _polish_sheet(ws, n_cols: int, n_rows: int) -> None:
    """Bold header, freeze it, add an autofilter, and size columns to content
    (capped so a long description doesn't produce a screen-wide column)."""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if n_cols == 0:
        return
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    if n_rows > 0:
        ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{n_rows + 1}"
    for idx in range(1, n_cols + 1):
        letter = get_column_letter(idx)
        longest = 0
        # Sample the header + first 200 rows; enough to size sensibly and
        # cheap enough for the 10k-row saved-schedule sheet.
        for row in ws.iter_rows(min_col=idx, max_col=idx, min_row=1,
                                max_row=min(n_rows + 1, 201), values_only=True):
            v = row[0]
            if v is not None:
                longest = max(longest, len(str(v)))
        ws.column_dimensions[letter].width = max(8, min(60, longest + 2))


def build_workbook(tables: dict[str, pd.DataFrame],
                   readme: pd.DataFrame | None = None) -> bytes:
    """Write ``{sheet name: DataFrame}`` to one ``.xlsx`` and return its bytes.

    ``readme`` (optional) becomes the first sheet, named ``README``. Sheet
    names are made Excel-legal and de-duplicated; an empty frame still gets a
    sheet (with just its headers) so the workbook layout is stable.
    """
    buf = io.BytesIO()
    taken: set = set()
    ordered: list[tuple[str, pd.DataFrame]] = []
    if readme is not None:
        ordered.append(("README", readme))
        taken.add("README")
    for name, df in tables.items():
        sheet = safe_sheet_name(name, taken)
        taken.add(sheet)
        ordered.append((sheet, df))

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet, df in ordered:
            frame = _excel_safe_frame(df)
            frame.to_excel(writer, sheet_name=sheet, index=False)
            _polish_sheet(writer.sheets[sheet], len(frame.columns), len(frame))
    return buf.getvalue()


def build_database_workbook(data_dir: Path | str | None = None, *,
                            include_saved_schedule_rows: bool = True) -> bytes:
    """The whole ``data/`` folder as one workbook: README + one sheet per
    entry in :data:`DATABASE_TABLES` (the saved-schedule rows sheet only when
    ``include_saved_schedule_rows`` — it's the slow, big one)."""
    return build_workbook(
        database_tables(data_dir, include_saved_schedule_rows=include_saved_schedule_rows),
        readme=database_readme(include_saved_schedule_rows=include_saved_schedule_rows),
    )


# ---------------------------------------------------------------------------
# Inbound — catalog workbook → cell-level changes for the SHA-locked save
# ---------------------------------------------------------------------------
# A planner exports the database workbook, edits labor minutes (or adds a SKU
# row) on the ``Machine catalog`` / ``Accessory catalog`` sheet in Excel, and
# uploads the file back. :func:`read_catalog_workbook` finds those sheets and
# checks their header against the live CSV; :func:`diff_catalog_sheet` turns a
# sheet into the same ``(sku, column, new_value)`` cell diffs the in-app
# editor produces, so the app can push them through its existing fetch-fresh
# + SHA-locked merge instead of overwriting the file.

#: Labor (person-minute) columns of each catalog, in CSV order — the cells the
#: in-app editors let a planner type into. ``Bat`` is a battery count rather
#: than minutes, but it is edited and validated the same way (a number ≥ 0).
MACHINE_LABOR_COLUMNS: tuple[str, ...] = (
    "Warehouse", "Wire", "Trailer", "FN_Assy", "PDI", "QC", "Ship", "ETO", "Bat",
)
ACCESSORY_LABOR_COLUMNS: tuple[str, ...] = (
    "Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw", "PMAcc", "GenAcc", "ComAcc",
)

#: What the machine editor's routing dropdowns offer, per station. Mirrors the
#: ``SelectboxColumn(options=...)`` lists in ``app.tab_floor_verification_machine``
#: — tests/test_excel_catalog_import.py checks the two stay in step. A blank
#: cell is also accepted (the loader falls back to the station's own default).
ROUTING_OPTIONS: dict[str, tuple[str, ...]] = {
    "Final": ("Final", "Undercarriage", "ComAcc", "GenAcc", "PMAcc"),
    "AccKIT": ("AccKIT", "ComAcc", "GenAcc", "PMAcc", "Final"),
    "PDI": ("PDI", "ComAcc", "GenAcc", "PMAcc", "Final", "QC"),
    "Wire": ("Wire", "GenAcc", "ComAcc", "PMAcc", "Final", "Trailer"),
    "Undercarriage": ("Undercarriage", "ComAcc", "GenAcc", "PMAcc", "Final"),
}

#: ``Final Station HND`` → station ``Final``, facility code ``HND``. Any
#: facility code is accepted so a fourth plant's columns import unchanged.
_ROUTING_COLUMN_RE = re.compile(
    r"^(Final|AccKIT|PDI|Wire|Undercarriage) Station ([A-Z]{2,4})$"
)

#: A SKU may not start with these — Excel / Sheets would run the cell as a
#: formula when the CSV is opened (same guard as the in-app add-SKU forms).
FORMULA_PREFIXES = ("=", "+", "-", "@")


@dataclass(frozen=True)
class CatalogSpec:
    """One importable catalog: which sheet / file it is and what's numeric."""
    key: str
    label: str
    sheet: str
    file_name: str
    numeric_cols: tuple[str, ...]
    #: Other sheet names (lower-case) that mean this catalog, so a planner's
    #: own single-sheet file called ``machines`` is picked up by name too.
    aliases: tuple[str, ...] = ()

    @property
    def file_path(self) -> str:
        return f"data/{self.file_name}"


CATALOG_SPECS: dict[str, CatalogSpec] = {
    "machine": CatalogSpec(
        key="machine", label="machine", sheet="Machine catalog",
        file_name="machine_clean.csv", numeric_cols=MACHINE_LABOR_COLUMNS,
        aliases=("machine", "machines", "machine_clean", "fg", "fg catalog"),
    ),
    "accessory": CatalogSpec(
        key="accessory", label="accessory", sheet="Accessory catalog",
        file_name="acc_clean.csv", numeric_cols=ACCESSORY_LABOR_COLUMNS,
        aliases=("accessory", "accessories", "acc_clean", "acc", "acc catalog"),
    ),
}


class CatalogImportError(ValueError):
    """The upload can't be read as a workbook at all (garbage bytes, no
    sheets). Per-sheet trouble is reported on the :class:`CatalogWorkbook`
    instead, so the planner sees every sheet's verdict at once."""


@dataclass
class CatalogSheet:
    """One catalog sheet as read from the upload, header already reconciled
    with the live CSV. ``frame`` is all text (``_cell_to_text``), one row per
    non-blank SKU, columns in live-header order."""
    spec: CatalogSpec
    sheet_name: str
    frame: pd.DataFrame
    live_columns: list[str]
    #: Live columns the sheet carries (what the diff may touch).
    columns: list[str]
    #: Editable live columns the sheet lacks — left untouched on every row.
    missing_columns: list[str]
    #: Sheet columns the live CSV doesn't have — ignored.
    extra_columns: list[str]
    #: SKUs (upper-cased) listed more than once on the sheet.
    duplicate_skus: list[str]
    #: ``"name"`` (sheet called e.g. ``Machine catalog``) or ``"header"``
    #: (worked out from the labor columns in its header row).
    matched_by: str = "name"
    #: Sheet headers that weren't the app's own names but resolved through
    #: :data:`CATALOG_COLUMN_ALIASES` — ``{"Warehouse (PICK)": "Warehouse"}``.
    #: Shown in the preview so a planner sees the mapping rather than trusting it.
    aliased: dict[str, str] = field(default_factory=dict)

    @property
    def file_path(self) -> str:
        return self.spec.file_path


@dataclass
class CatalogWorkbook:
    sheets: dict[str, CatalogSheet]
    #: ``(sheet name, why it was not used)`` for every other sheet.
    skipped: list[tuple[str, str]]
    sheet_names: list[str]


def live_catalog_columns(data_dir: Path | str | None = None) -> dict[str, list[str]]:
    """Header of each catalog CSV as deployed, keyed like :data:`CATALOG_SPECS`.
    A missing file gives an empty list (the reader then validates against the
    spec's known columns)."""
    data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
    out: dict[str, list[str]] = {}
    for key, spec in CATALOG_SPECS.items():
        path = data_dir / spec.file_name
        if path.exists():
            head = pd.read_csv(path, nrows=0, encoding="utf-8-sig")
            out[key] = [str(c).strip() for c in head.columns]
        else:
            out[key] = []
    return out


def routing_station(column) -> str | None:
    """``"PDI Station CYP"`` → ``"PDI"``; ``None`` for a non-routing column."""
    m = _ROUTING_COLUMN_RE.match(str(column or "").strip())
    return m.group(1) if m else None


def editable_columns(spec: CatalogSpec, live_cols) -> tuple[list[str], list[str]]:
    """``(numeric, text)`` columns the import may change, in live-header
    order: the catalog's labor columns, ``Description``, and every
    per-facility routing column. ``SKU`` and ``Last Modified`` are never
    written from a sheet (the save stamps Last Modified itself)."""
    live_cols = [str(c) for c in live_cols]
    numeric = [c for c in live_cols if c in spec.numeric_cols]
    text = [c for c in live_cols if c == "Description" or routing_station(c)]
    return numeric, text


def parse_labor_value(raw) -> float | None:
    """A labor cell as a number: ``"45"`` / ``"45.0"`` / ``45`` / ``" 1,200 "``
    → ``float``; a blank cell → ``None`` (meaning *no value given*). Raises
    ``ValueError`` with a planner-readable reason for anything else, including
    negative or non-finite numbers (the editors cap at ``min_value=0``)."""
    text = _cell_to_text(raw)
    if text == "":
        return None
    cleaned = text.replace(",", "").replace(" ", "")
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"'{text}' is not a number") from None
    if not math.isfinite(value):
        raise ValueError(f"'{text}' is not a finite number")
    if value < 0:
        raise ValueError(f"'{text}' is negative — labor minutes can't be below 0")
    return value


def normalize_routing_value(column: str, raw) -> str:
    """The dropdown spelling of a routing cell (``"final"`` → ``"Final"``).
    A blank cell stays blank. Raises ``ValueError`` when the value isn't one
    of that station's options — the same list the in-app editor offers."""
    station = routing_station(column)
    text = _cell_to_text(raw)
    if text == "" or station is None:
        return text
    options = ROUTING_OPTIONS.get(station, ())
    for option in options:
        if option.lower() == text.lower():
            return option
    raise ValueError(f"'{text}' is not one of: {', '.join(options)}")


def _sheet_key_for_name(name) -> str | None:
    n = str(name or "").strip().lower()
    for key, spec in CATALOG_SPECS.items():
        if n == spec.sheet.lower() or n in spec.aliases:
            return key
    return None


#: Header cells that mean "this is the SKU column" — the app's ``SKU`` and the
#: IE workbook's ``SKU ID`` / ``FG SKU``. Exact (case-insensitive) on purpose:
#: a header cell is the whole name, and a substring test would see ``SKU`` in
#: a schedule sheet's ``SKU IN BC`` and take that tab for a catalog.
_SKU_HEADER_NAMES = frozenset(
    a.upper() for a in CATALOG_COLUMN_ALIASES["machine"]["SKU"]
)


def _is_sku_header(cell) -> bool:
    return _cell_to_text(cell).strip().upper() in _SKU_HEADER_NAMES


def find_catalog_header_row(raw: pd.DataFrame) -> int | None:
    """Index of the first row (within :data:`HEADER_SCAN_ROWS`) that has a
    SKU header cell — ``SKU`` as the app writes it, or ``SKU ID`` as the IE
    workbook does. ``raw`` is a sheet read with ``header=None``."""
    if raw is None or raw.empty:
        return None
    for i in range(min(len(raw), HEADER_SCAN_ROWS)):
        if any(_is_sku_header(c) for c in raw.iloc[i].tolist()):
            return i
    return None


def _header_names(raw: pd.DataFrame, header_row: int) -> list[str]:
    return [_cell_to_text(c) for c in raw.iloc[header_row].tolist()]


def _infer_key_from_header(header: list[str]) -> str | None:
    """Which catalog a header belongs to, judged by its labor columns: the
    spec with the most of its numeric columns present wins; it needs at least
    one, and a tie (e.g. ``SKU`` + ``Warehouse`` only) is *no* answer.

    Headers are resolved through :func:`canonical_catalog_column` first, so
    the IE workbook's ``Final Assy (FN-ASSY)`` counts as ``FN_Assy``."""
    scores = {}
    for key, spec in CATALOG_SPECS.items():
        resolved = {canonical_catalog_column(h, key) for h in header}
        scores[key] = sum(1 for c in spec.numeric_cols if c in resolved)
    best = max(scores.values()) if scores else 0
    if best == 0:
        return None
    winners = [k for k, s in scores.items() if s == best]
    return winners[0] if len(winners) == 1 else None


def _build_catalog_sheet(spec: CatalogSpec, sheet_name: str, raw: pd.DataFrame,
                         header_row: int, live_cols: list[str],
                         matched_by: str) -> CatalogSheet:
    if not live_cols:
        live_cols = ["SKU", "Description", *spec.numeric_cols, "Last Modified"]
    live_cols = [str(c) for c in live_cols]
    lookup = {c.strip().lower(): c for c in live_cols}
    lookup.setdefault("sku", "SKU")

    header = _header_names(raw, header_row)
    body = raw.iloc[header_row + 1:]
    data: dict[str, list[str]] = {}
    extra: list[str] = []
    aliased: dict[str, str] = {}
    for pos, cell in enumerate(header):
        live_name = lookup.get(cell.strip().lower())
        if live_name is None:
            # Not one of the app's own names — try the IE workbook's spelling
            # ("Warehouse (PICK)", "SKU ID", "🔋Bat" …). Only a column the live
            # CSV actually has may be targeted, so a stale header can't make
            # the import write somewhere the save wouldn't recognise.
            canonical = canonical_catalog_column(cell, spec.key)
            if canonical is not None and canonical.lower() in lookup:
                live_name = lookup[canonical.lower()]
                aliased[cell] = live_name
        if live_name is None:
            if cell:
                extra.append(cell)
            continue
        if live_name in data:
            continue  # the same header twice — first column wins
        data[live_name] = [_cell_to_text(v) for v in body.iloc[:, pos].tolist()]

    columns = [c for c in live_cols if c in data]
    if "SKU" in data and "SKU" not in columns:
        columns.insert(0, "SKU")
    frame = pd.DataFrame({c: data[c] for c in columns}, dtype=object)
    duplicates: list[str] = []
    if "SKU" in frame.columns:
        frame["SKU"] = [str(s).strip() for s in frame["SKU"]]
        frame = frame[frame["SKU"] != ""].reset_index(drop=True)
        upper = pd.Series([s.upper() for s in frame["SKU"]], dtype=object)
        duplicates = sorted(set(upper[upper.duplicated(keep=False)].tolist()))

    numeric, text = editable_columns(spec, live_cols)
    missing = [c for c in numeric + text if c not in columns]
    return CatalogSheet(
        spec=spec, sheet_name=sheet_name, frame=frame, live_columns=live_cols,
        columns=columns, missing_columns=missing, extra_columns=extra,
        duplicate_skus=duplicates, matched_by=matched_by, aliased=aliased,
    )


def read_catalog_workbook(raw_bytes: bytes, filename: str = "",
                          live_columns: dict[str, list[str]] | None = None,
                          ) -> CatalogWorkbook:
    """Find the catalog sheet(s) in an uploaded workbook.

    Sheets are matched by name first (``Machine catalog`` / ``Accessory
    catalog`` — the exported workbook's names — or a few aliases, case-
    insensitive). When no sheet is called that, a planner's own file is
    assumed and each sheet is judged by its header instead: the first row
    carrying a ``SKU`` cell is the header, and the catalog is the one whose
    labor columns it carries. A header below a title row is fine.

    Each sheet's header is reconciled with the live CSV's columns
    (``live_columns``, keyed like :data:`CATALOG_SPECS`; read from ``data/``
    when not given): matching is case-insensitive, extra columns are listed
    and ignored, missing ones are listed and left untouched.

    Raises :class:`CatalogImportError` only when the bytes aren't a workbook
    or it has no sheets. A sheet with no ``SKU`` header lands in ``skipped``
    with a reason rather than raising, so one bad tab doesn't hide a good one.
    """
    label = f"**{filename}**" if filename else "the workbook"
    live = live_columns if live_columns is not None else live_catalog_columns()
    names: list[str] = []
    sheets: dict[str, CatalogSheet] = {}
    skipped: list[tuple[str, str]] = []
    try:
        with pd.ExcelFile(io.BytesIO(raw_bytes), engine="openpyxl") as xf:
            names = [str(n) for n in xf.sheet_names]
            by_name = {n: _sheet_key_for_name(n) for n in names}
            candidates: list[tuple[str, str, str]] = []
            if any(by_name.values()):
                for n, k in by_name.items():
                    if k:
                        candidates.append((n, k, "name"))
                    else:
                        skipped.append((n, "not a catalog sheet"))
            else:
                for n in names:
                    head = xf.parse(n, header=None, dtype=object, nrows=HEADER_SCAN_ROWS)
                    hr = find_catalog_header_row(head)
                    if hr is None:
                        skipped.append((n, "no header row with a `SKU` column"))
                        continue
                    k = _infer_key_from_header(_header_names(head, hr))
                    if k is None:
                        skipped.append((
                            n, "header doesn't carry either catalog's labor columns "
                               "(or carries both)",
                        ))
                        continue
                    candidates.append((n, k, "header"))
            for n, k, how in candidates:
                spec = CATALOG_SPECS[k]
                if k in sheets:
                    skipped.append((
                        n, f"`{sheets[k].sheet_name}` already supplies the {spec.sheet}",
                    ))
                    continue
                raw = xf.parse(n, header=None, dtype=object)
                hr = find_catalog_header_row(raw)
                if hr is None:
                    skipped.append((
                        n, "no header row with a `SKU` column — keep the exported header row",
                    ))
                    continue
                sheets[k] = _build_catalog_sheet(spec, n, raw, hr, live.get(k) or [], how)
    except Exception as exc:  # noqa: BLE001 — surface whatever openpyxl said
        raise CatalogImportError(
            f"We couldn't open {label} as an Excel workbook ({exc}). "
            "Save it as .xlsx and re-upload."
        ) from exc
    if not names:
        raise CatalogImportError(f"{label} has no sheets.")
    return CatalogWorkbook(sheets=sheets, skipped=skipped, sheet_names=names)


# ---- Diff: sheet vs the loaded catalog ------------------------------------
@dataclass(frozen=True)
class CellChange:
    sku: str
    column: str
    old: object
    new: object


@dataclass(frozen=True)
class CellProblem:
    sku: str
    column: str
    value: str
    reason: str


def _fmt_cell(value) -> str:
    """Display form of an old/new value: whole floats without ``.0``."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


@dataclass
class CatalogDiff:
    """Everything applying one sheet would do — rendered as the preview, and
    (via :meth:`cell_tuples` / ``new_rows``) fed to the app's save path."""
    spec: CatalogSpec
    cells: list[CellChange]
    new_rows: list[dict]
    absent_skus: list[str]
    problems: list[CellProblem]
    duplicate_skus: list[str]
    n_rows: int
    n_existing: int
    numeric_cols: list[str]
    text_cols: list[str]

    @property
    def blocked(self) -> bool:
        """True when something on the sheet failed validation; the app refuses
        to save until the planner fixes those cells in Excel and re-uploads."""
        return bool(self.problems or self.duplicate_skus)

    @property
    def is_empty(self) -> bool:
        return not self.cells and not self.new_rows

    @property
    def new_skus(self) -> list[str]:
        return [r["SKU"] for r in self.new_rows]

    @property
    def changed_skus(self) -> list[str]:
        seen: list[str] = []
        for c in self.cells:
            if c.sku not in seen:
                seen.append(c.sku)
        return seen

    def cell_tuples(self) -> list[tuple[str, str, object]]:
        """The ``(sku, column, new_value)`` list ``_apply_diffs_and_serialize``
        in app.py consumes — identical in shape to the editor's diff."""
        return [(c.sku, c.column, c.new) for c in self.cells]

    def cells_frame(self) -> pd.DataFrame:
        rows = []
        for c in self.cells:
            delta = ""
            if isinstance(c.old, (int, float)) and isinstance(c.new, (int, float)):
                delta = _fmt_cell(float(c.new) - float(c.old))
                if not delta.startswith("-") and delta not in ("", "0"):
                    delta = "+" + delta
            rows.append({"SKU": c.sku, "Column": c.column,
                         "Old": _fmt_cell(c.old), "New": _fmt_cell(c.new), "Δ": delta})
        return pd.DataFrame(rows, columns=["SKU", "Column", "Old", "New", "Δ"])

    def new_rows_frame(self) -> pd.DataFrame:
        cols = ["SKU", *self.numeric_cols, *self.text_cols]
        rows = [{c: _fmt_cell(r.get(c, "")) for c in cols} for r in self.new_rows]
        return pd.DataFrame(rows, columns=cols)

    def problems_frame(self) -> pd.DataFrame:
        rows = [{"SKU": p.sku, "Column": p.column, "Value": p.value, "Problem": p.reason}
                for p in self.problems]
        return pd.DataFrame(rows, columns=["SKU", "Column", "Value", "Problem"])


def _live_value(live: pd.DataFrame, sku, column: str):
    if column not in live.columns:
        return None
    value = live.loc[sku, column]
    if isinstance(value, pd.Series):  # duplicate SKUs on file — first wins
        value = value.iloc[0] if len(value) else None
    return value


def _live_number(live: pd.DataFrame, sku, column: str) -> float:
    value = _live_value(live, sku, column)
    try:
        if value is None or pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _live_text(live: pd.DataFrame, sku, column: str) -> str:
    value = _live_value(live, sku, column)
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def diff_catalog_sheet(sheet: CatalogSheet, live: pd.DataFrame | None) -> CatalogDiff:
    """What applying ``sheet`` to ``live`` (the loaded catalog, indexed by SKU
    — ``load_machine_labor`` / ``load_acc_labor`` output) would change.

    The rules mirror the in-app editor's save, so the two paths can't disagree:

    * a sheet row is matched to a catalog row by SKU, case-insensitively; the
      catalog's spelling is what gets written
    * a **blank cell never changes anything** — type ``0`` to zero a value
      (this also keeps a never-edited row's blank routing cells, which the
      loader shows as the default station, from reading as a change)
    * labor columns compare as numbers (``45.0`` is ``45``); ``Description``
      as trimmed text; routing cells are normalised to the dropdown spelling
    * catalog SKUs that aren't on the sheet are left exactly as they are — an
      import never deletes a row
    * a row whose SKU isn't in the catalog becomes a new row (blank labor
      cells stay blank, which the loader reads as 0)
    * anything that doesn't validate — not a number, negative, a routing value
      the editor wouldn't offer, a formula-prefixed SKU, a SKU listed twice —
      is a :class:`CellProblem`; ``blocked`` is then True and nothing from the
      sheet should be saved until the planner fixes the file
    """
    spec = sheet.spec
    if live is None:
        live = pd.DataFrame()
    numeric_all, text_all = editable_columns(spec, sheet.live_columns)
    numeric = [c for c in numeric_all if c in sheet.columns]
    text = [c for c in text_all if c in sheet.columns]

    canon: dict[str, object] = {}
    for s in live.index:
        canon.setdefault(str(s).strip().upper(), s)
    dupes = {d.upper() for d in sheet.duplicate_skus}

    cells: list[CellChange] = []
    new_rows: list[dict] = []
    problems: list[CellProblem] = []
    seen: set[str] = set()
    n_existing = 0
    frame = sheet.frame
    if "SKU" not in frame.columns:
        frame = pd.DataFrame(columns=["SKU"])

    for row in frame.to_dict("records"):
        sku = str(row.get("SKU", "")).strip()
        key = sku.upper()
        if not sku:
            continue
        seen.add(key)  # on the sheet — so never "absent", even when duplicated
        if key in dupes:
            continue

        if key in canon:
            live_sku = canon[key]
            n_existing += 1
            for col in numeric:
                raw = row.get(col, "")
                if raw == "":
                    continue
                try:
                    new = parse_labor_value(raw)
                except ValueError as exc:
                    problems.append(CellProblem(sku, col, str(raw), str(exc)))
                    continue
                old = _live_number(live, live_sku, col)
                if new != old:
                    cells.append(CellChange(str(live_sku), col, old, new))
            for col in text:
                raw = str(row.get(col, "")).strip()
                if raw == "":
                    continue
                if routing_station(col):
                    try:
                        new = normalize_routing_value(col, raw)
                    except ValueError as exc:
                        problems.append(CellProblem(sku, col, raw, str(exc)))
                        continue
                else:
                    new = raw
                old = _live_text(live, live_sku, col)
                if new != old:
                    cells.append(CellChange(str(live_sku), col, old, new))
            continue

        # Not in the catalog → a new row.
        if sku[:1] in FORMULA_PREFIXES:
            problems.append(CellProblem(
                sku, "SKU", sku,
                "a SKU can't start with =, +, - or @ (Excel would run it as a formula)",
            ))
            continue
        new_row: dict = {"SKU": sku}
        ok = True
        for col in numeric:
            raw = row.get(col, "")
            if raw == "":
                new_row[col] = ""
                continue
            try:
                new_row[col] = parse_labor_value(raw)
            except ValueError as exc:
                problems.append(CellProblem(sku, col, str(raw), str(exc)))
                ok = False
        for col in text:
            raw = str(row.get(col, "")).strip()
            if routing_station(col):
                try:
                    new_row[col] = normalize_routing_value(col, raw)
                except ValueError as exc:
                    problems.append(CellProblem(sku, col, raw, str(exc)))
                    ok = False
            else:
                new_row[col] = raw
        if ok:
            new_rows.append(new_row)

    absent: list[str] = []
    for s in live.index:
        s_key = str(s).strip().upper()
        if s_key not in seen and str(s) not in absent:
            absent.append(str(s))

    return CatalogDiff(
        spec=spec, cells=cells, new_rows=new_rows, absent_skus=absent,
        problems=problems, duplicate_skus=list(sheet.duplicate_skus),
        n_rows=int(len(frame)), n_existing=n_existing,
        numeric_cols=numeric, text_cols=text,
    )
