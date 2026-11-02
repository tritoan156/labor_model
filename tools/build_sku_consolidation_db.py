"""Build the SKU-consolidation routing database (2 final SKUs -> 1 Lvl 0 SKU).

Reads the "SKU Development Tracker" workbook (one row per ``Base FG + Accessory
kit`` pair that becomes a single Lvl 0 SKU), looks both halves up in the labor
model's catalogs (``data/machine_clean.csv`` / ``data/acc_clean.csv``), and
writes an Excel database with:

* SKU Map        - the pair -> new SKU, product line, editable ETO flag and
                   wire-harness component count
* WC Mapping     - labor line (+ product line) -> operation, work center and
                   routing reference (editable), plus the parameters (location
                   prefix, wire minutes per component, Pre-QC share)
* Labor Lines    - every labor line of every new SKU, minutes straight from the
                   catalogs, with a data status (OK / MISSING / SUSPECT /
                   REJECTED / ASSUMED)
* Routing Matrix - new SKU x work center minutes (SUMIFS over Labor Lines)
* Routing Lines  - one row per SKU x routing x operation, ready to filter for BC
* Source Machine / Source Acc - the catalog rows used, verbatim
* README         - decisions, assumptions, legend, open items

Usage:
    python tools/build_sku_consolidation_db.py <tracker.xlsx> <output.xlsx> [location]
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

REPO = Path(__file__).resolve().parents[1]
MACHINE_CSV = REPO / "data" / "machine_clean.csv"
ACC_CSV = REPO / "data" / "acc_clean.csv"

CUSTOMER_SUFFIXES = ("HERC", "HRC", "UR", "ES")     # core/constants.py
DEFAULT_NAMEPLATE_PREP = 10                           # core/constants.py
DEFAULT_BATT_RAW = 320                                # core/constants.py
DEFAULT_LOCATION = "HEN1"
WIRE_MIN_PER_COMPONENT = 20                           # user, 2026-11-02
PRE_QC_SHARE = 2 / 3                                  # user, 2026-11-02 (Final QC gets the rest)

# Families that run on the ETO line: large power module, special units, large
# SDG. Same BOM tree, longer lead time, different team. Editable per SKU in the
# workbook (SKU Map -> "ETO unit?").
ETO_FAMILIES = ("BOSS220", "BOSS400", "SDG125", "SDG150", "SDG220", "SDG400")

# Catalog rows the user rejected: their minutes are left blank (REJECTED) until
# someone fills them in. SKU -> reason.
REJECTED_CATALOG_ROWS = {
    "SDG220-004": "Catalog row reads 'SDG25S-8E1 TRAILER 2\" BALL HITCH' with SDG25-size minutes; "
                  "SDG220 is a large SDG. Rejected 2026-11-02 - fill in once confirmed.",
}

# Outlier rule for SUSPECT: a catalog value above this many times the median of
# the same column within the same family, and at least SUSPECT_MIN_MINUTES.
SUSPECT_FACTOR = 3.0
SUSPECT_MIN_MINUTES = 300
MACHINE_CHECK_COLS = ("FN_Assy", "Trailer", "PDI", "QC", "Wire", "ETO")
ACC_CHECK_COLS = ("GenAcc", "ComAcc", "PMAcc", "AccKIT", "BattSubRaw")

# Work centers as <location>-<code>. (code, name)
WORK_CENTERS = [
    ("WIR",  "Wire Harness"),
    ("BAT",  "Battery"),
    ("PM",   "Small Power Module & Prep Accessories"),
    ("GEN1", "Eboss Generator"),
    ("GEN2", "SDG Generator"),
    ("COM1", "Compressor Undercarriage install"),
    ("COM2", "Compressor Accessories install"),
    ("ETO",  "Large PM / Special Unit / Large SDG"),
    ("TRL",  "Trailer"),
    ("FIN",  "Final Assembly"),
    ("PDI",  "Pre-Delivery Inspection"),
]

# Operations (op code, name, operation no.). Order = sequence on the floor.
OPERATIONS = [
    ("WIRE-PREP",  "Wire harness preparation",          10),
    ("BAT-BUILD",  "Battery build",                     20),
    ("ACC-PREP",   "Prepare accessories",               30),
    ("PM-INSTALL", "Power module accessories install",  40),
    ("G-INSTALL",  "Generator accessories install",     50),
    ("C-INST-UC",  "Compressor undercarriage install",  60),
    ("C-INST-ACC", "Compressor accessories install",    70),
    ("ETO-BUILD",  "ETO engineering build",             80),
    ("T-BUILD",    "Trailer build",                     90),
    ("F-MOUNT",    "Final mount",                       100),
    ("PRE-QC",     "Pre QC",                            110),
    ("PDI",        "Pre-delivery inspection",           120),
    ("FINAL-QC",   "Final QC",                          130),
]
NOT_ROUTED = "NOT ROUTED"
NOT_MAPPED = "NOT MAPPED"

# Labor line, mapping line, op code, work-center code, routing reference, status, note.
# Routing references follow the BOM trees: R-FINAL / R-HYB-FINAL / R-PDS-FINAL
# (final product), R-GEN (generator with accessories), R-PM (power module with
# accessories), R-HYB-BATTERY, R-TRAILER, R-PDS (compressor with undercarriage),
# R-ACC-SUB (built accessory items at Prepare Accessories).
WC_MAPPING = [
    ("Wire",           "ALL",   "WIRE-PREP",  "WIR",      "R-WIRE",        "Given",   "20 min per component that needs a wire harness (generator, power module, battery, prepared accessories...). Count per SKU on SKU Map; starts every unit."),
    ("Battery",        "ALL",   "BAT-BUILD",  "BAT",      "R-HYB-BATTERY", "Given",   "Kit 'BattSubRaw' x battery count (machine 'Bat'). BOSS units only. BOM: Battery built item under PM-001."),
    ("Nameplate Prep", "ALL",   "BAT-BUILD",  "BAT",      "R-HYB-BATTERY", "Assumed", "Kit 'Nameplate Prep' (PREP-NP) - part of the battery build."),
    ("PMAcc",          "ALL",   "PM-INSTALL", "PM",       "R-PM",          "Given",   "Kit 'PMAcc'. BOM: PM-001 Power Module with Accessories Installed at PM Accessories."),
    ("PMAcc",          "ETO",   "PM-INSTALL", "ETO",      "R-PM",          "Given",   "Same routing, ETO team."),
    ("GenAcc",         "EBOSS", "G-INSTALL",  "GEN1",     "R-GEN",         "Given",   "Kit 'GenAcc' = G-INSTALL ops. BOM: Generator with Accessories Installed (hybrid)."),
    ("GenAcc",         "PM",    "PM-INSTALL", "PM",       "R-PM",          "Given",   "Kit 'GenAcc' on a PM skid = accessories installed on the power module (PM-001 branch)."),
    ("GenAcc",         "SDG",   "G-INSTALL",  "GEN2",     "R-GEN",         "Given",   "Kit 'GenAcc' = G-INSTALL ops. BOM: Generator with Accessories Installed (SDG only)."),
    ("GenAcc",         "ETO",   "G-INSTALL",  "ETO",      "R-GEN",         "Given",   "Same routing, ETO team."),
    ("AccKIT",         "ALL",   "ACC-PREP",   "PM",       "R-ACC-SUB",     "Given",   "Kit 'AccKIT' = Prepare Accessories (built accessory items R-ACC1-SUB / R-ACC2 / R-TRAILER-SUB). Combined into PM for now."),
    ("Final",          "ALL",   "F-MOUNT",    "FIN",      "R-FINAL",       "Given",   "Machine 'FN_Assy' (mount / marry). BOM: Final Assembly, F-MOUNT."),
    ("Final",          "EBOSS", "F-MOUNT",    "FIN",      "R-HYB-FINAL",   "Given",   "Hybrid final routing."),
    ("Final",          "PDS",   "C-INST-UC",  "COM1",     "R-PDS",         "Given",   "PDS 'FN_Assy' = undercarriage install. BOM: Compressor with Undercarriage Installed."),
    ("Final",          "ETO",   "F-MOUNT",    "ETO",      "R-FINAL",       "Assumed", "Final mount of ETO units by the ETO team."),
    ("ComAcc",         "ALL",   "C-INST-ACC", "COM2",     "R-PDS",         "Given",   "Kit 'ComAcc' = C-INST accessory ops on the compressor."),
    ("Trailer",        "ALL",   "T-BUILD",    "TRL",      "R-TRAILER",     "Given",   "Machine 'Trailer' minutes. BOM: Trailer-001 at Trailer."),
    ("Pre QC",         "ALL",   "PRE-QC",     "PDI",      "R-FINAL",       "Given",   "2/3 of machine 'QC' minutes, after final assembly and before PDI. Work center assumed = PDI."),
    ("Pre QC",         "EBOSS", "PRE-QC",     "PDI",      "R-HYB-FINAL",   "Given",   "Hybrid final routing."),
    ("Pre QC",         "PDS",   "PRE-QC",     "PDI",      "R-PDS-FINAL",   "Given",   "Compressor final routing."),
    ("PDI",            "ALL",   "PDI",        "PDI",      "R-FINAL",       "Given",   "Machine 'PDI' minutes."),
    ("PDI",            "EBOSS", "PDI",        "PDI",      "R-HYB-FINAL",   "Given",   "Hybrid final routing."),
    ("PDI",            "PDS",   "PDI",        "PDI",      "R-PDS-FINAL",   "Given",   "Compressor final routing."),
    ("Final QC",       "ALL",   "FINAL-QC",   "PDI",      "R-FINAL",       "Given",   "1/3 of machine 'QC' minutes, after PDI. Work center assumed = PDI."),
    ("Final QC",       "EBOSS", "FINAL-QC",   "PDI",      "R-HYB-FINAL",   "Given",   "Hybrid final routing."),
    ("Final QC",       "PDS",   "FINAL-QC",   "PDI",      "R-PDS-FINAL",   "Given",   "Compressor final routing."),
    ("ETO",            "ALL",   "ETO-BUILD",  "ETO",      "R-PM",          "Given",   "Machine 'ETO' minutes (large PM engineering work)."),
    ("Warehouse",      "ALL",   "",           NOT_ROUTED, "",              "Assumed", "Pick minutes (machine + kit). Not in the BOM trees - indirect, not a routing operation."),
    ("Ship",           "ALL",   "",           NOT_ROUTED, "",              "Assumed", "Ship minutes. Not in the BOM trees - indirect, not a routing operation."),
]

# ---------------------------------------------------------------------------
# styles
# ---------------------------------------------------------------------------
FONT = "Arial"
F_BASE = Font(name=FONT, size=10)
F_BOLD = Font(name=FONT, size=10, bold=True)
F_HEAD = Font(name=FONT, size=10, bold=True, color="FFFFFF")
F_INPUT = Font(name=FONT, size=10, color="0000FF")
F_TITLE = Font(name=FONT, size=14, bold=True)
FILL_HEAD = PatternFill("solid", fgColor="1F4E78")
FILL_EDIT = PatternFill("solid", fgColor="FFFF00")
FILL_ASK = PatternFill("solid", fgColor="F8CBAD")
FILL_ASSUMED = PatternFill("solid", fgColor="FFF2CC")
NUM_MIN = '#,##0.0;(#,##0.0);"-"'
NUM_INT = '#,##0;(#,##0);"-"'

# WC Mapping layout: parameters in M1:N3, op list from row 6 (M:O), WC list from row 6 (Q:S)
P_LOC = "'WC Mapping'!$N$1"
P_WIRE = "'WC Mapping'!$N$2"
P_PREQC = "'WC Mapping'!$N$3"
LIST_FIRST_ROW = 6


def strip_suffix(sku: str, known: set[str]) -> str:
    s = sku.strip()
    if s in known:
        return s
    for sfx in CUSTOMER_SUFFIXES:
        if s.endswith(sfx) and s[: -len(sfx)] in known:
            return s[: -len(sfx)]
    return s


def family(sku: str) -> str:
    m = re.match(r"^([A-Z]+?\d+)", str(sku).upper())
    return m.group(1) if m else str(sku)


def product_line(base: str) -> str:
    u = base.upper()
    if u.startswith("PDS"):
        return "PDS"
    if u.startswith("SDG"):
        return "SDG"
    if u.startswith("BOSS"):
        return "PM" if "HS" in u else "EBOSS"
    return "OTHER"


def num(v) -> float | None:
    try:
        return None if pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None


def mapping_for(line: str, mapping_line: str) -> tuple[str, str, str]:
    """(op code, work-center code, routing reference) the workbook's lookup resolves to."""
    rows = {(l, ml): (op, wc, rr) for l, ml, op, wc, rr, *_ in WC_MAPPING}
    return rows.get((line, mapping_line)) or rows.get((line, "ALL")) or ("", NOT_MAPPED, "")


def routing_ops() -> list[tuple[str, str, str]]:
    """Distinct (routing reference, op code, work-center code), in operation order."""
    op_order = {code: i for i, (code, _, _) in enumerate(OPERATIONS)}
    pairs = {(rr, op, wc) for _, _, op, wc, rr, *_ in WC_MAPPING if op}
    return sorted(pairs, key=lambda p: (op_order[p[1]], p[0], p[2]))


def family_medians(df: pd.DataFrame, cols) -> dict:
    fam = df.index.to_series().map(family)
    return {c: df[c].groupby(fam).median() for c in cols if c in df.columns}


def suspect(value, fam, col, medians) -> bool:
    if value is None or col not in medians or value < SUSPECT_MIN_MINUTES:
        return False
    med = medians[col].get(fam)
    return bool(pd.notna(med) and med > 0 and value > SUSPECT_FACTOR * med)


def read_tracker(path: Path) -> list[dict]:
    wb = load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        prod = r[0]
        if not prod or "+" not in str(prod):
            continue  # e.g. the "TRAILERS (PLACE HOLDER)" line
        base, kit = [x.strip() for x in str(prod).split("+", 1)]
        rows.append({
            "pair": str(prod).strip(), "base": base, "kit": kit,
            "customer": r[1], "bu": r[2], "class": r[3], "lvl0": r[4],
            "bom": r[5], "routing": r[6], "golive": r[11],
        })
    return rows


def header(ws, row, cols, widths=None):
    for i, c in enumerate(cols, 1):
        cell = ws.cell(row=row, column=i, value=c)
        cell.font = F_HEAD
        cell.fill = FILL_HEAD
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30
    if widths:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def add_table(ws, name, ref):
    t = Table(displayName=name, ref=ref)
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(t)


def wire_components(pl: str, bat: int, kit_has_accessories: bool) -> list[str]:
    """Default list of components needing a wire harness, from the BOM tree."""
    comps = []
    if pl in ("SDG", "EBOSS"):
        comps.append("generator")
    if pl in ("EBOSS", "PM"):
        comps.append("power module")
    comps += ["battery"] * bat
    if kit_has_accessories:
        comps.append("prepared accessories")
    return comps


def build_sku_rows(tracker_rows, m_idx, a_idx):
    m_known, a_known = set(m_idx.index), set(a_idx.index)
    sku_rows, seen = [], {}
    for i, t in enumerate(tracker_rows, 1):
        base_cat = strip_suffix(t["base"], m_known)
        kit_cat = strip_suffix(t["kit"], a_known)
        flags = []
        lvl0 = str(t["lvl0"]).strip() if t["lvl0"] else ""
        note = ""
        if not lvl0 or " " in lvl0:          # blank, or a free-text note
            if lvl0:
                note = lvl0
                flags.append(f"tracker note: {lvl0}")
            lvl0 = f"TBD-{t['base']}+{t['kit']}"
            flags.append("no Lvl 0 nomenclature yet")
        if lvl0 in seen:
            flags.append(f"duplicate Lvl 0 of row {seen[lvl0]}")
            lvl0 = f"{lvl0} (dup{i})"
        seen[lvl0] = i
        if base_cat not in m_known:
            flags.append("MISSING: base not in machine catalog")
        if base_cat in REJECTED_CATALOG_ROWS:
            flags.append("REJECTED: " + REJECTED_CATALOG_ROWS[base_cat])
        if kit_cat not in a_known:
            flags.append("MISSING: kit not in accessory catalog")
        if base_cat != t["base"]:
            flags.append(f"base minutes from {base_cat} (customer suffix dropped)")
        if kit_cat != t["kit"]:
            flags.append(f"kit minutes from {kit_cat} (customer suffix dropped)")
        pl = product_line(base_cat)
        is_boss = base_cat.upper().startswith("BOSS")
        bat = int(num(m_idx.loc[base_cat, "Bat"]) or 0) if (base_cat in m_known and is_boss) else 0
        kit_acc = False
        if kit_cat in a_known:
            kit_acc = any((num(a_idx.loc[kit_cat, c]) or 0) > 0 for c in ("GenAcc", "PMAcc", "ComAcc"))
        comps = wire_components(pl, bat, kit_acc)
        sku_rows.append({
            "seq": i, "new": lvl0, "base": t["base"], "base_cat": base_cat,
            "kit": t["kit"], "kit_cat": kit_cat, "customer": t["customer"],
            "bu": t["bu"], "class": t["class"], "pl": pl,
            "eto": "Y" if family(base_cat) in ETO_FAMILIES else "N",
            "family": family(base_cat), "bat": bat,
            "wire_n": len(comps), "wire_basis": ", ".join(comps) if comps else "none",
            "wire_catalog": num(m_idx.loc[base_cat, "Wire"]) if base_cat in m_known else None,
            "base_desc": m_idx.loc[base_cat, "Description"] if base_cat in m_known else "",
            "kit_desc": a_idx.loc[kit_cat, "Description"] if kit_cat in a_known else "",
            "golive": t["golive"], "routing": t["routing"], "bom": t["bom"],
            "flags": "; ".join(flags), "tracker_note": note,
            "needs_decision": any(f.startswith(("no Lvl", "duplicate", "MISSING", "REJECTED")) for f in flags),
        })
    return sku_rows


def build_labor_lines(sku_rows, m_idx, a_idx):
    """Mirror core/labor_calculator.compute_unit_labor, but keep every labor
    line separate (no station re-routing) so each can be mapped to an
    operation / work center in the workbook. Adds the user's rules: wire
    harness = 20 min x components, QC split 2/3 Pre QC + 1/3 Final QC, and a
    data status per line."""
    m_known, a_known = set(m_idx.index), set(a_idx.index)
    m_med = family_medians(m_idx, MACHINE_CHECK_COLS)
    a_med = family_medians(a_idx, ACC_CHECK_COLS)
    labor = []
    for s in sku_rows:
        b, k = s["base_cat"], s["kit_cat"]
        rejected = b in REJECTED_CATALOG_ROWS
        mrow = m_idx.loc[b] if (b in m_known and not rejected) else None
        arow = a_idx.loc[k] if k in a_known else None
        is_boss = b.upper().startswith("BOSS")
        fam = s["family"]

        def M(col):
            return num(mrow[col]) if mrow is not None else None

        def A(col):
            return num(arow[col]) if arow is not None else None

        def status_for(value, src, col):
            if src == "Machine":
                if rejected:
                    return "REJECTED", REJECTED_CATALOG_ROWS[b]
                if mrow is None:
                    return "MISSING", "base not in machine catalog - fill in"
                if value is None:
                    return "MISSING", f"blank '{col}' in machine catalog - fill in"
                if suspect(value, fam, col, m_med):
                    return "SUSPECT", f"{value:g} min is > {SUSPECT_FACTOR:g}x the {fam} median for '{col}' ({m_med[col].get(fam):g}) - confirm"
            if src == "Accessory":
                if arow is None:
                    return "MISSING", "kit not in accessory catalog - fill in"
                if value is None:
                    return "MISSING", f"blank '{col}' in accessory catalog - fill in"
                if suspect(value, fam, col, a_med):
                    return "SUSPECT", f"{value:g} min is > {SUSPECT_FACTOR:g}x the {fam} median for '{col}' ({a_med[col].get(fam):g}) - confirm"
            return "OK", ""

        def line(name, src, src_sku, src_col, minutes, qty=1, share=1.0, note="", status=None):
            if status is None:
                st, auto_note = status_for(minutes, src, src_col)
            else:
                st, auto_note = status, ""
            labor.append({"new": s["new"], "base": b, "kit": k, "line": name,
                          "src": src, "src_sku": src_sku, "src_col": src_col,
                          "min": minutes, "qty": qty, "share": share,
                          "status": st, "note": "; ".join(x for x in (auto_note, note) if x)})

        # --- machine side -------------------------------------------------
        line("Warehouse", "Machine", b, "Warehouse", M("Warehouse"))
        wire_ref = f"catalog Wire = {s['wire_catalog']:g} min (reference only)" if s["wire_catalog"] is not None else "catalog Wire blank"
        line("Wire", "Rule", "user 2026-11-02", "20 min x components", "=WIRE", qty="=WIRE_N",
             note=f"components: {s['wire_basis']} (default from BOM tree - confirm against detail BOM); {wire_ref}",
             status="ASSUMED")
        line("Trailer", "Machine", b, "Trailer", M("Trailer"))
        line("Final", "Machine", b, "FN_Assy", M("FN_Assy"))
        line("Pre QC", "Machine", b, "QC", M("QC"), share="=PREQC", note="2/3 of catalog QC minutes")
        line("PDI", "Machine", b, "PDI", M("PDI"))
        line("Final QC", "Machine", b, "QC", M("QC"), share="=1-PREQC", note="1/3 of catalog QC minutes")
        line("Ship", "Machine", b, "Ship", M("Ship"))
        line("ETO", "Machine", b, "ETO", M("ETO"))
        # --- accessory side -----------------------------------------------
        line("Warehouse", "Accessory", k, "Warehouse", A("Warehouse"))
        line("AccKIT", "Accessory", k, "AccKIT", A("AccKIT"))
        line("PMAcc", "Accessory", k, "PMAcc", A("PMAcc"))
        line("GenAcc", "Accessory", k, "GenAcc", A("GenAcc"))
        line("ComAcc", "Accessory", k, "ComAcc", A("ComAcc"))
        # --- battery (core/labor_calculator rule) ---------------------------
        bat = s["bat"]
        btr, prep = A("BattSubRaw"), A("Nameplate Prep")
        if not is_boss:
            line("Nameplate Prep", "Rule", "labor model", "non-BOSS", 0.0, note="non-BOSS unit: no battery (model rule)", status="OK")
            line("Battery", "Rule", "labor model", "non-BOSS", 0.0, qty=0, note="non-BOSS unit: no battery (model rule)", status="OK")
        elif rejected or mrow is None:
            line("Nameplate Prep", "Accessory", k, "Nameplate Prep", prep)
            line("Battery", "Accessory", k, "BattSubRaw", btr, qty=bat,
                 note="battery count unknown (machine row rejected / missing)",
                 status="REJECTED" if rejected else "MISSING")
        elif arow is not None and ((btr or 0) > 0 or (prep or 0) > 0):
            line("Nameplate Prep", "Accessory", k, "Nameplate Prep", prep)
            line("Battery", "Accessory", k, "BattSubRaw", btr, qty=bat, note=f"x {bat} battery(ies) from machine 'Bat'")
        elif arow is None:
            line("Nameplate Prep", "Accessory", k, "Nameplate Prep", None)
            line("Battery", "Accessory", k, "BattSubRaw", None, qty=bat)
        elif bat > 0:
            line("Nameplate Prep", "Model default", "constants.py", "DEFAULT_NAMEPLATE_PREP",
                 float(DEFAULT_NAMEPLATE_PREP), note="kit has no battery minutes; model default", status="ASSUMED")
            line("Battery", "Model default", "constants.py", "DEFAULT_BATT_RAW",
                 float(DEFAULT_BATT_RAW), qty=bat, note=f"kit has no battery minutes; model default x {bat} battery(ies)", status="ASSUMED")
        else:
            line("Nameplate Prep", "Accessory", k, "Nameplate Prep", prep or 0.0, status="OK")
            line("Battery", "Accessory", k, "BattSubRaw", btr or 0.0, qty=0, note="Bat = 0", status="OK")
    return labor


def build(tracker: Path, out: Path, location: str = DEFAULT_LOCATION) -> None:
    machine = pd.read_csv(MACHINE_CSV)
    acc = pd.read_csv(ACC_CSV)
    m_idx = machine.set_index("SKU")
    a_idx = acc.set_index("SKU")

    sku_rows = build_sku_rows(read_tracker(tracker), m_idx, a_idx)
    labor = build_labor_lines(sku_rows, m_idx, a_idx)
    ops = routing_ops()

    wb = Workbook()
    ws_read = wb.active
    ws_read.title = "README"
    ws_map = wb.create_sheet("SKU Map")
    ws_wc = wb.create_sheet("WC Mapping")
    ws_lab = wb.create_sheet("Labor Lines")
    ws_mat = wb.create_sheet("Routing Matrix")
    ws_rl = wb.create_sheet("Routing Lines")
    ws_sm = wb.create_sheet("Source Machine")
    ws_sa = wb.create_sheet("Source Acc")

    n_sku, n_lab, n_wc = len(sku_rows), len(labor), len(WC_MAPPING)
    map_last, lab_last, wc_last = n_sku + 1, n_lab + 1, n_wc + 1
    op_first, op_last = LIST_FIRST_ROW, LIST_FIRST_ROW + len(OPERATIONS) - 1
    wcl_first, wcl_last = LIST_FIRST_ROW, LIST_FIRST_ROW + len(WORK_CENTERS) - 1
    OP_CODE_RNG = f"'WC Mapping'!$M${op_first}:$M${op_last}"
    OP_NAME_RNG = f"'WC Mapping'!$N${op_first}:$N${op_last}"
    OP_NO_RNG = f"'WC Mapping'!$O${op_first}:$O${op_last}"

    # ---- SKU Map ---------------------------------------------------------
    cols = ["Seq", "New SKU (Lvl 0)", "Base FG (tracker)", "Base catalog SKU",
            "Accessory kit (tracker)", "Kit catalog SKU", "Customer", "Bus Unit",
            "Class", "Product line", "ETO unit?", "Wire harness components",
            "Wire components basis (default)", "Catalog Wire min (ref)", "Batteries",
            "Family", "Base description", "Kit description", "Go-Live by",
            "BOM status", "Routing status", "Flags"]
    header(ws_map, 1, cols, [5, 30, 18, 16, 18, 16, 14, 16, 6, 11, 9, 11, 40, 10, 9, 10, 55, 55, 11, 11, 11, 70])
    for r, s in enumerate(sku_rows, 2):
        vals = [s["seq"], s["new"], s["base"], s["base_cat"], s["kit"], s["kit_cat"],
                s["customer"], s["bu"], s["class"], s["pl"], s["eto"], s["wire_n"],
                s["wire_basis"], s["wire_catalog"], s["bat"], s["family"],
                s["base_desc"], s["kit_desc"], s["golive"], s["bom"], s["routing"], s["flags"]]
        for c, v in enumerate(vals, 1):
            ws_map.cell(row=r, column=c, value=v).font = F_BASE
        for c in (11, 12):
            ws_map.cell(row=r, column=c).fill = FILL_EDIT
            ws_map.cell(row=r, column=c).font = F_INPUT
        ws_map.cell(row=r, column=13).fill = FILL_ASSUMED
        ws_map.cell(row=r, column=19).number_format = "yyyy-mm-dd"
        if s["flags"]:
            ws_map.cell(row=r, column=22).fill = FILL_ASK if s["needs_decision"] else FILL_ASSUMED
    add_table(ws_map, "SKUMap", f"A1:{get_column_letter(len(cols))}{map_last}")
    SKU_KEY = f"'SKU Map'!$B$2:$B${map_last}"

    def sku_lookup(col_letter: str, key_cell: str) -> str:
        return f"INDEX('SKU Map'!${col_letter}$2:${col_letter}${map_last},MATCH({key_cell},{SKU_KEY},0))"

    # ---- WC Mapping ------------------------------------------------------
    cols = ["Labor line", "Mapping line", "Key", "Op code", "Operation", "Op No.",
            "WC code", "Work Center", "Routing reference", "Status", "Note"]
    header(ws_wc, 1, cols, [15, 12, 20, 12, 30, 7, 10, 13, 15, 9, 80])
    for r, (line_, ml, op, wc, rr, status, note) in enumerate(WC_MAPPING, 2):
        ws_wc.cell(row=r, column=1, value=line_).font = F_BASE
        ws_wc.cell(row=r, column=2, value=ml).font = F_BASE
        ws_wc.cell(row=r, column=3, value=f'=A{r}&"|"&B{r}').font = F_BASE
        c = ws_wc.cell(row=r, column=4, value=op or None)
        c.font = F_INPUT
        c.fill = FILL_EDIT
        ws_wc.cell(row=r, column=5, value=f'=IFERROR(INDEX({OP_NAME_RNG},MATCH(D{r},{OP_CODE_RNG},0)),"")').font = F_BASE
        ws_wc.cell(row=r, column=6, value=f'=IFERROR(INDEX({OP_NO_RNG},MATCH(D{r},{OP_CODE_RNG},0)),"")').font = F_BASE
        c = ws_wc.cell(row=r, column=7, value=wc)
        c.font = F_INPUT
        c.fill = FILL_EDIT
        ws_wc.cell(row=r, column=8, value=(
            f'=IF(OR(G{r}="{NOT_ROUTED}",G{r}="{NOT_MAPPED}"),G{r},$N$1&"-"&G{r})')).font = F_BASE
        c = ws_wc.cell(row=r, column=9, value=rr or None)
        c.font = F_INPUT
        c.fill = FILL_EDIT
        sc = ws_wc.cell(row=r, column=10, value=status)
        sc.font = F_BOLD
        if status == "ASK":
            sc.fill = FILL_ASK
        elif status == "Assumed":
            sc.fill = FILL_ASSUMED
        ws_wc.cell(row=r, column=11, value=note).font = F_BASE
    # parameters
    params = [("Location prefix", location, "HEN1 / HEN2 / SPA / CYP - every work center follows it."),
              ("Wire harness min per component", WIRE_MIN_PER_COMPONENT, "User 2026-11-02: 20 min to prepare the harness for each component that needs one."),
              ("Pre QC share of QC minutes", PRE_QC_SHARE, "User 2026-11-02: 2/3 Pre QC (after final assembly), 1/3 Final QC (after PDI).")]
    for i, (label, val, note) in enumerate(params, 1):
        ws_wc.cell(row=i, column=13, value=label).font = F_BOLD
        c = ws_wc.cell(row=i, column=14, value=val)
        c.font = F_INPUT
        c.fill = FILL_EDIT
        ws_wc.cell(row=i, column=15, value=note).font = F_BASE
    ws_wc.cell(row=3, column=14).number_format = "0.000"
    # operation list
    for col, title in enumerate(("Op code", "Operation", "Op No."), 13):
        cell = ws_wc.cell(row=LIST_FIRST_ROW - 1, column=col, value=title)
        cell.font = F_HEAD
        cell.fill = FILL_HEAD
    for i, (code, name, opno) in enumerate(OPERATIONS):
        r = op_first + i
        ws_wc.cell(row=r, column=13, value=code).font = F_BASE
        ws_wc.cell(row=r, column=14, value=name).font = F_BASE
        c = ws_wc.cell(row=r, column=15, value=opno)
        c.font = F_INPUT
        c.fill = FILL_EDIT
    # work-center list
    for col, title in enumerate(("WC code", "Work Center", "Name"), 17):
        cell = ws_wc.cell(row=LIST_FIRST_ROW - 1, column=col, value=title)
        cell.font = F_HEAD
        cell.fill = FILL_HEAD
    for i, (code, name) in enumerate(WORK_CENTERS):
        r = wcl_first + i
        ws_wc.cell(row=r, column=17, value=code).font = F_BASE
        ws_wc.cell(row=r, column=18, value=f'=$N$1&"-"&Q{r}').font = F_BASE
        ws_wc.cell(row=r, column=19, value=name).font = F_BASE
    r = wcl_last + 1
    ws_wc.cell(row=r, column=17, value=NOT_ROUTED).font = F_BASE
    ws_wc.cell(row=r, column=19, value="Indirect labor - not a routing operation").font = F_BASE
    ws_wc.cell(row=r + 1, column=17, value=NOT_MAPPED).font = F_BASE
    ws_wc.cell(row=r + 1, column=19, value="Labor line with no mapping row").font = F_BASE
    ws_wc.cell(row=max(op_last, wcl_last) + 3, column=13, value=(
        "Lookup rule: Labor Lines looks for 'Labor line|Mapping line' first, then 'Labor line|ALL'. "
        "Mapping line = ETO when the SKU's 'ETO unit?' is Y, else its Product line.")).font = F_BASE
    for col, w in (("M", 30), ("N", 34), ("O", 9), ("Q", 11), ("R", 14), ("S", 38)):
        ws_wc.column_dimensions[col].width = w

    # ---- Labor Lines -----------------------------------------------------
    cols = ["New SKU", "Base catalog SKU", "Kit catalog SKU", "Product line", "ETO unit?",
            "Mapping line", "Labor line", "Source", "Source SKU", "Source column",
            "Minutes (source)", "Qty", "Share", "Line minutes", "Op code", "Work Center",
            "Routing reference", "Data status", "Note"]
    header(ws_lab, 1, cols, [30, 16, 16, 11, 9, 12, 15, 13, 16, 22, 12, 6, 7, 12, 12, 14, 16, 11, 70])

    def map_lookup(ret_col: str, r: int, default: str) -> str:
        rng = f"'WC Mapping'!${ret_col}$2:${ret_col}${wc_last}"
        keys = f"'WC Mapping'!$C$2:$C${wc_last}"
        return (f"=IFERROR(INDEX({rng},MATCH(G{r}&\"|\"&F{r},{keys},0)),"
                f"IFERROR(INDEX({rng},MATCH(G{r}&\"|ALL\",{keys},0)),\"{default}\"))")

    for r, L in enumerate(labor, 2):
        ws_lab.cell(row=r, column=1, value=L["new"])
        ws_lab.cell(row=r, column=2, value=L["base"])
        ws_lab.cell(row=r, column=3, value=L["kit"])
        ws_lab.cell(row=r, column=4, value="=" + sku_lookup("J", f"A{r}"))
        ws_lab.cell(row=r, column=5, value="=" + sku_lookup("K", f"A{r}"))
        ws_lab.cell(row=r, column=6, value=f'=IF(E{r}="Y","ETO",D{r})')
        ws_lab.cell(row=r, column=7, value=L["line"])
        ws_lab.cell(row=r, column=8, value=L["src"])
        ws_lab.cell(row=r, column=9, value=L["src_sku"])
        ws_lab.cell(row=r, column=10, value=L["src_col"])
        mins = L["min"]
        mc = ws_lab.cell(row=r, column=11, value=(f"={P_WIRE}" if mins == "=WIRE" else mins))
        mc.font = F_INPUT if mins != "=WIRE" else F_BASE
        mc.number_format = NUM_MIN
        qty = L["qty"]
        qc = ws_lab.cell(row=r, column=12, value=("=" + sku_lookup("L", f"A{r}") if qty == "=WIRE_N" else qty))
        qc.font = F_INPUT if qty != "=WIRE_N" else F_BASE
        share = L["share"]
        sh = ws_lab.cell(row=r, column=13, value=(
            f"={P_PREQC}" if share == "=PREQC" else (f"=1-{P_PREQC}" if share == "=1-PREQC" else share)))
        sh.number_format = "0.000"
        sh.font = F_BASE
        lm = ws_lab.cell(row=r, column=14, value=f"=N(K{r})*N(L{r})*N(M{r})")
        lm.number_format = NUM_MIN
        ws_lab.cell(row=r, column=15, value=map_lookup("D", r, ""))
        ws_lab.cell(row=r, column=16, value=map_lookup("H", r, NOT_MAPPED))
        ws_lab.cell(row=r, column=17, value=map_lookup("I", r, ""))
        st = ws_lab.cell(row=r, column=18, value=L["status"])
        st.font = F_BOLD
        if L["status"] in ("MISSING", "REJECTED", "SUSPECT"):
            st.fill = FILL_ASK
            ws_lab.cell(row=r, column=11).fill = FILL_ASK
        elif L["status"] == "ASSUMED":
            st.fill = FILL_ASSUMED
        ws_lab.cell(row=r, column=19, value=L["note"])
        for c in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 19):
            ws_lab.cell(row=r, column=c).font = F_BASE
    add_table(ws_lab, "LaborLines", f"A1:S{lab_last}")
    LL_MIN = f"'Labor Lines'!$N$2:$N${lab_last}"
    LL_SKU = f"'Labor Lines'!$A$2:$A${lab_last}"
    LL_OP = f"'Labor Lines'!$O$2:$O${lab_last}"
    LL_WC = f"'Labor Lines'!$P$2:$P${lab_last}"
    LL_RR = f"'Labor Lines'!$Q$2:$Q${lab_last}"
    LL_ST = f"'Labor Lines'!$R$2:$R${lab_last}"

    # ---- Routing Matrix --------------------------------------------------
    wc_codes = [c for c, _ in WORK_CENTERS]
    matrix_cols = wc_codes + [NOT_ROUTED, NOT_MAPPED]
    cols = (["New SKU", "Product line", "ETO unit?"] + matrix_cols
            + ["Routed minutes", "Total labor minutes", "Check", "Lines to check", "Routing status"])
    header(ws_mat, 1, cols, [30, 11, 9] + [11] * len(matrix_cols) + [13, 13, 8, 10, 12])
    first_wc = 4
    for j, code in enumerate(wc_codes):           # headers follow the location prefix
        ws_mat.cell(row=1, column=first_wc + j, value=f'={P_LOC}&"-{code}"')
    last_real = first_wc + len(wc_codes) - 1
    last_any = first_wc + len(matrix_cols) - 1
    tot_col = last_any + 1
    a_, b_, z_ = get_column_letter(first_wc), get_column_letter(last_real), get_column_letter(last_any)
    for r, s in enumerate(sku_rows, 2):
        ws_mat.cell(row=r, column=1, value=s["new"]).font = F_BASE
        ws_mat.cell(row=r, column=2, value="=" + sku_lookup("J", f"A{r}")).font = F_BASE
        ws_mat.cell(row=r, column=3, value="=" + sku_lookup("K", f"A{r}")).font = F_BASE
        for j in range(len(matrix_cols)):
            col = first_wc + j
            cl = get_column_letter(col)
            c = ws_mat.cell(row=r, column=col, value=f"=SUMIFS({LL_MIN},{LL_SKU},$A{r},{LL_WC},{cl}$1)")
            c.number_format = NUM_INT
            c.font = F_BASE
        c = ws_mat.cell(row=r, column=tot_col, value=f"=SUM({a_}{r}:{b_}{r})")
        c.number_format = NUM_INT
        c.font = F_BOLD
        c = ws_mat.cell(row=r, column=tot_col + 1, value=f"=SUMIFS({LL_MIN},{LL_SKU},$A{r})")
        c.number_format = NUM_INT
        c.font = F_BASE
        tl = get_column_letter(tot_col + 1)
        c = ws_mat.cell(row=r, column=tot_col + 2,
                        value=f'=IF(ROUND(SUM({a_}{r}:{z_}{r})-{tl}{r},3)=0,"OK","ERR")')
        c.font = F_BOLD
        c = ws_mat.cell(row=r, column=tot_col + 3, value=f'=COUNTIFS({LL_SKU},$A{r},{LL_ST},"<>OK")')
        c.font = F_BASE
        c.number_format = NUM_INT
        ws_mat.cell(row=r, column=tot_col + 4, value=s["routing"]).font = F_BASE
    tr = n_sku + 2
    ws_mat.cell(row=tr, column=1, value="TOTAL").font = F_BOLD
    for col in range(first_wc, tot_col + 2):
        cl = get_column_letter(col)
        c = ws_mat.cell(row=tr, column=col, value=f"=SUM({cl}2:{cl}{map_last})")
        c.number_format = NUM_INT
        c.font = F_BOLD
    ws_mat.cell(row=tr + 2, column=1, value=(
        "Minutes are person-minutes per unit. NOT ROUTED = indirect (warehouse pick / ship). "
        "NOT MAPPED = a labor line with no row on WC Mapping. 'Lines to check' counts Labor Lines "
        "whose Data status is not OK (MISSING / SUSPECT / REJECTED / ASSUMED).")).font = F_BASE

    # ---- Routing Lines ---------------------------------------------------
    cols = ["New SKU", "Routing reference", "Operation No.", "Op code", "Operation", "Work Center",
            "WC code", "Run time (min/unit)", "Setup time (min)", "Include in routing?",
            "Data status", "Product line", "Routing status"]
    header(ws_rl, 1, cols, [30, 16, 13, 12, 32, 14, 9, 16, 14, 16, 12, 11, 12])
    r = 2
    for s in sku_rows:
        for rr, op, code in ops:
            ws_rl.cell(row=r, column=1, value=s["new"])
            ws_rl.cell(row=r, column=2, value=rr)
            ws_rl.cell(row=r, column=3, value=f"=INDEX({OP_NO_RNG},MATCH(D{r},{OP_CODE_RNG},0))")
            ws_rl.cell(row=r, column=4, value=op)
            ws_rl.cell(row=r, column=5, value=f"=INDEX({OP_NAME_RNG},MATCH(D{r},{OP_CODE_RNG},0))")
            ws_rl.cell(row=r, column=6, value=f'={P_LOC}&"-"&G{r}')
            ws_rl.cell(row=r, column=7, value=code)
            crit = f"{LL_SKU},A{r},{LL_WC},F{r},{LL_RR},B{r},{LL_OP},D{r}"
            c = ws_rl.cell(row=r, column=8, value=f"=SUMIFS({LL_MIN},{crit})")
            c.number_format = NUM_MIN
            c = ws_rl.cell(row=r, column=9, value=0)
            c.font = F_INPUT
            c.fill = FILL_EDIT
            ws_rl.cell(row=r, column=11, value=f'=IF(COUNTIFS({crit},{LL_ST},"<>OK")>0,"CHECK","OK")')
            ws_rl.cell(row=r, column=10, value=f'=OR(H{r}>0,K{r}="CHECK")')
            ws_rl.cell(row=r, column=12, value="=" + sku_lookup("J", f"A{r}"))
            ws_rl.cell(row=r, column=13, value=s["routing"])
            for cc in range(1, 14):
                if cc != 9:
                    ws_rl.cell(row=r, column=cc).font = F_BASE
            r += 1
    add_table(ws_rl, "RoutingLines", f"A1:M{r-1}")
    ws_rl.cell(row=r + 1, column=1, value=(
        "Filter 'Include in routing?' = TRUE for the Business Central routing lines (one routing per "
        "sub-assembly, as in the BOM trees). 'CHECK' = at least one contributing labor line is MISSING / "
        "SUSPECT / REJECTED / ASSUMED - see Labor Lines. The routing x operation pairs listed here are the "
        "ones on WC Mapping when the file was built; add a new pair there and rebuild to see it here. "
        "Setup time is 0 until setup minutes are given.")).font = F_BASE

    # ---- Source sheets ---------------------------------------------------
    used_m = machine[machine["SKU"].isin({s["base_cat"] for s in sku_rows})].reset_index(drop=True)
    used_a = acc[acc["SKU"].isin({s["kit_cat"] for s in sku_rows})].reset_index(drop=True)
    for ws, df, name in ((ws_sm, used_m, "SourceMachine"), (ws_sa, used_a, "SourceAcc")):
        header(ws, 1, list(df.columns), [16, 60] + [11] * (len(df.columns) - 2))
        for i, row in enumerate(df.itertuples(index=False), 2):
            for j, v in enumerate(row, 1):
                if isinstance(v, float) and pd.isna(v):
                    v = None
                ws.cell(row=i, column=j, value=v).font = F_BASE
            if row[0] in REJECTED_CATALOG_ROWS:
                for j in range(1, len(df.columns) + 1):
                    ws.cell(row=i, column=j).fill = FILL_ASK
        add_table(ws, name, f"A1:{get_column_letter(len(df.columns))}{len(df)+1}")

    # ---- README ----------------------------------------------------------
    ws_read.column_dimensions["A"].width = 120
    n_status = pd.Series([L["status"] for L in labor]).value_counts().to_dict()
    lines = [
        ("SKU consolidation - routing database (DRAFT)", F_TITLE),
        (f"Built {date.today().isoformat()} from '{tracker.name}' + labor-model catalogs "
         f"data/machine_clean.csv and data/acc_clean.csv. Location prefix: {location} "
         "(WC Mapping!N1).", F_BASE),
        (f"{n_sku} tracker rows -> {n_sku} new Lvl 0 SKUs; {n_lab} labor lines "
         f"({', '.join(f'{k} {v}' for k, v in sorted(n_status.items()))}). "
         "Rebuild: python tools/build_sku_consolidation_db.py <tracker.xlsx> <out.xlsx> [location]", F_BASE),
        ("", F_BASE),
        ("HOW IT FITS TOGETHER", F_BOLD),
        ("SKU Map        - each tracker row: Base FG + Accessory kit -> New SKU (Lvl 0 nomenclature; "
         "'TBD-...' when the tracker has none yet). Editable: 'ETO unit?' and 'Wire harness components'.", F_BASE),
        ("Labor Lines    - every labor line of every new SKU, minutes copied from the catalogs (blue). "
         "Line minutes = minutes x qty x share. Data status says whether the value can be trusted.", F_BASE),
        ("WC Mapping     - which operation, work center and routing each labor line goes to, by product "
         "line, following the BOM trees. Parameters (location, wire minutes, Pre-QC share) at M1:N3. "
         "EDIT THE YELLOW CELLS; everything downstream recalculates.", F_BASE),
        ("Routing Matrix - new SKU x work center minutes (SUMIFS over Labor Lines) with a check that "
         "nothing is lost and a count of lines still to check.", F_BASE),
        ("Routing Lines  - one row per SKU x routing x operation. Filter 'Include in routing?' = TRUE "
         "for the BC routing lines; 'Data status' = CHECK where a contributing value is not final.", F_BASE),
        ("Source Machine / Source Acc - the catalog rows used, verbatim (rejected rows shaded orange).", F_BASE),
        ("", F_BASE),
        ("LEGEND", F_BOLD),
        ("Blue text = value copied from a catalog or a parameter (hardcoded input).  Yellow fill = meant "
         "to be edited.", F_BASE),
        ("Data status: OK = catalog value accepted | MISSING = no value, left blank to fill in | "
         "SUSPECT = kept, but > 3x the family median (confirm) | REJECTED = catalog row refused, left "
         "blank to fill in | ASSUMED = default rule, confirm (wire-harness count, model battery defaults). "
         "Orange fill = MISSING / SUSPECT / REJECTED; light yellow = ASSUMED.", F_BASE),
        ("", F_BASE),
        ("DECISIONS (user, 2026-11-02)", F_BOLD),
        ("D1. Work centers from the BOM trees: Final Assembly -> FIN | Generator Accessories -> GEN1 "
         "(hybrid) / GEN2 (SDG) | PM Accessories -> PM | Battery -> BAT | Trailer -> TRL | Prepare "
         "Accessories -> PM (combined for now) | Compressor Undercarriage -> COM1 (undercarriage "
         "install) + COM2 (compressor accessories) | ETO = same tree, ETO team, longer lead time.", F_BASE),
        ("D2. Wire harness starts every unit: 20 min to prepare the harness for each component that "
         "needs one (generator, power module, battery, prepared accessories...). The count per SKU is "
         "defaulted from the BOM tree and must be confirmed against the detail BOM; the catalog's own "
         "'Wire' minutes are shown for reference only.", F_BASE),
        ("D3. After final assembly: Pre QC (2/3 of catalog QC minutes) -> PDI -> Final QC (1/3). Both QC "
         "ops sit at the PDI work center (no QC work center given).", F_BASE),
        ("D4. Missing data is left blank and marked MISSING; suspicious values are kept and marked "
         "SUSPECT; SDG220-004's catalog row is REJECTED (reads as an SDG25S) and left blank.", F_BASE),
        ("D5. The labor model keeps pricing Base FG + kit until the Lvl 0 SKUs are fully developed; no "
         "model change now.", F_BASE),
        ("", F_BASE),
        ("ASSUMPTIONS (all editable)", F_BOLD),
        ("1. Customer-suffixed SKUs (...UR / ...ES) use the base SKU's minutes - the model's rule "
         "(core/constants.CUSTOMER_SUFFIXES). Decal time is inside the kit's GenAcc/ComAcc minutes, "
         "not split out as the F-DECAL op.", F_BASE),
        ("2. All minutes are the labor model's person-minutes per unit. The model's per-facility "
         "station re-routes are NOT applied; each labor line is shown raw and mapped here.", F_BASE),
        ("3. Battery = kit BattSubRaw x machine 'Bat' + kit Nameplate Prep; when a BOSS kit has no "
         "battery minutes the model defaults (320 + 10) are used and marked ASSUMED; non-BOSS units "
         "carry no battery (core/labor_calculator.compute_unit_labor).", F_BASE),
        (f"4. 'ETO unit?' = Y for families {', '.join(ETO_FAMILIES)}. Their GenAcc, PMAcc and Final "
         "labor route to ETO; Wire, Battery, Trailer, PDI/QC stay at their own work centers.", F_BASE),
        ("5. Blank catalog cells are MISSING (not read as 0). Setup time = 0 on every operation.", F_BASE),
        ("6. Default wire-harness components: generator (SDG / EBOSS), power module (EBOSS / PM skid), "
         "one per battery, and 'prepared accessories' when the kit carries accessory minutes. "
         "Compressors get only the accessories component.", F_BASE),
        ("", F_BASE),
        ("OPEN ITEMS", F_BOLD),
        ("O1. Confirm each SKU's wire-harness component count against its detail BOM (SKU Map column L).", F_BASE),
        ("O2. Fill in the MISSING / REJECTED values (filter Labor Lines on Data status): kit "
         "PDS400S-A006 is not in the accessory catalog; SDG220-004's machine minutes are rejected.", F_BASE),
        ("O3. Confirm the SUSPECT values (e.g. BOSS70-A017 GenAcc 1,500 min).", F_BASE),
        ("O4. Confirm the Pre QC / Final QC ops belong to the PDI work center.", F_BASE),
    ]
    for i, (txt, font) in enumerate(lines, 1):
        c = ws_read.cell(row=i, column=1, value=txt)
        c.font = font
        c.alignment = Alignment(wrap_text=True, vertical="top")

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"wrote {out}  skus={n_sku} labor_lines={n_lab} routing_ops={len(ops)} "
          f"location={location} statuses={n_status}")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    loc = sys.argv[3] if len(sys.argv) == 4 else DEFAULT_LOCATION
    build(Path(sys.argv[1]), Path(sys.argv[2]), loc)
