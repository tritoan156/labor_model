"""Turn the SKU-consolidation routing database into a Business Central
routing import package.

Reads the *recalculated* database workbook written by
``build_sku_consolidation_db.py`` (its Routing Lines / SKU Map / WC Mapping
sheets, cached values) and writes a workbook laid out like BC configuration
packages for:

* Routing Header (table 99000763) - one routing per new SKU x routing
  reference (final product, generator, power module, battery, trailer,
  compressor, prepared accessories, wire harness), as in the BOM trees
* Routing Line   (table 99000764) - operations in sequence with Next
  Operation No., work center, run time in minutes and a routing link code
* Work Center    (table 99000754) - the work centers the lines reference
* Item routing   - which item carries which routing (sub-assembly item numbers
  left blank to fill in)
* Checks         - everything that still needs a decision before import

Usage:
    python tools/build_bc_routing_import.py <database.xlsx> <output.xlsx>
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

BC_CODE_MAX = 20          # Routing No. / Work Center No. are Code[20]
RUN_TIME_UOM = "MINUTES"
ROUTING_TYPE = "Serial"
ROUTING_STATUS = "New"    # certify in BC after review

# Routing reference -> (one-letter suffix for the Routing No., header description)
# The final-product routing carries the SKU itself as Routing No.
ROUTING_SUFFIX = {
    "R-FINAL": ("", "Final product"),
    "R-HYB-FINAL": ("", "Final product (hybrid)"),
    "R-PDS-FINAL": ("", "Final product (compressor)"),
    "R-GEN": ("G", "Generator with accessories installed"),
    "R-PM": ("P", "Power module with accessories installed"),
    "R-HYB-BATTERY": ("B", "Battery"),
    "R-TRAILER": ("T", "Trailer"),
    "R-PDS": ("C", "Compressor with undercarriage installed"),
    "R-ACC-SUB": ("A", "Prepared accessories"),
    "R-WIRE": ("W", "Wire harness"),
}

# Op code -> default routing link code (from the BOM trees; edit per component in BC)
LINK_CODES = {
    "F-MOUNT": "F-MOUNT",
    "F-DECAL": "F-DECAL",
    "G-INSTALL": "G-INSTALL-01",
    "PM-INSTALL": "PM-BUILD",
    "BAT-BUILD": "BT-BUILD",
    "ACC-PREP": "A2-BUILD",
    "T-BUILD": "T-BUILD",
    "C-INST-UC": "C-INST-01",
    "C-INST-ACC": "C-INST-02",
}

# Operations added to a routing even though the database carries no minutes
# for them (needed so BOM components can link to them). (routing refs, op no,
# op code, description, note)
EXTRA_OPS = [
    (("R-FINAL", "R-HYB-FINAL", "R-PDS-FINAL"), 105, "F-DECAL", "Decal application",
     "Decal component links here (F-DECAL in the BOM). Decal time is inside the accessories install minutes; enter a run time if you want it separate."),
]

FONT = "Arial"
F_BASE = Font(name=FONT, size=10)
F_BOLD = Font(name=FONT, size=10, bold=True)
F_HEAD = Font(name=FONT, size=10, bold=True, color="FFFFFF")
F_TITLE = Font(name=FONT, size=14, bold=True)
FILL_HEAD = PatternFill("solid", fgColor="1F4E78")
FILL_EDIT = PatternFill("solid", fgColor="FFFF00")
FILL_ASK = PatternFill("solid", fgColor="F8CBAD")
NUM_MIN = '#,##0.0;(#,##0.0);"-"'


def sheet_df(wb, name: str) -> pd.DataFrame:
    ws = wb[name]
    hdr = [c.value for c in ws[1]]
    rows = [dict(zip(hdr, r)) for r in ws.iter_rows(min_row=2, values_only=True)
            if r[0] is not None and r[0] != "TOTAL"]
    df = pd.DataFrame(rows)
    return df[df[hdr[0]].astype(str).str.strip() != ""]


def header(ws, cols, widths):
    for i, c in enumerate(cols, 1):
        cell = ws.cell(row=1, column=i, value=c)
        cell.font = F_HEAD
        cell.fill = FILL_HEAD
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def write_rows(ws, rows, cols, name, number_formats=None):
    for r, row in enumerate(rows, 2):
        for c, col in enumerate(cols, 1):
            cell = ws.cell(row=r, column=c, value=row.get(col))
            cell.font = F_BASE
            if number_formats and col in number_formats:
                cell.number_format = number_formats[col]
    if rows:
        add_table(ws, name, f"A1:{get_column_letter(len(cols))}{len(rows) + 1}")


def add_table(ws, name, ref):
    t = Table(displayName=name, ref=ref)
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(t)


def build(db_path: Path, out: Path) -> None:
    wb_db = load_workbook(db_path, data_only=True)
    rl = sheet_df(wb_db, "Routing Lines")
    sku_map = sheet_df(wb_db, "SKU Map").set_index("New SKU (Lvl 0)")
    wc_ws = wb_db["WC Mapping"]
    location = wc_ws["N1"].value
    # work-center list lives at Q6:S16 of WC Mapping
    work_centers = []
    for r in range(6, 6 + 40):
        code = wc_ws.cell(row=r, column=17).value
        if not code or code in ("NOT ROUTED", "NOT MAPPED"):
            break
        work_centers.append((wc_ws.cell(row=r, column=18).value, wc_ws.cell(row=r, column=19).value))

    if rl["Run time (min/unit)"].isna().all():
        sys.exit("Routing Lines has no cached values - recalculate the database workbook in Excel first.")

    rl = rl[rl["Include in routing?"] == True].copy()  # noqa: E712
    rl["Operation No."] = rl["Operation No."].astype(int)

    headers, lines, items, checks = [], [], [], []

    def check(sku, routing_no, what):
        checks.append({"New SKU": sku, "Routing No.": routing_no, "What to resolve": what})

    for sku, by_sku in rl.groupby("New SKU", sort=False):
        placeholder = str(sku).startswith("TBD-")
        pl = sku_map.loc[sku, "Product line"]
        n_check_sku = int(sku_map.loc[sku, "Flags"] is not None and "MISSING" in str(sku_map.loc[sku, "Flags"]))
        if placeholder:
            check(sku, "", "No Lvl 0 nomenclature yet - routing numbers below are placeholders built from the tracker pair.")
        for rr, by_rr in by_sku.groupby("Routing reference", sort=False):
            suffix, desc = ROUTING_SUFFIX.get(rr, (rr.replace("R-", "")[:1], rr))
            routing_no = f"{sku}{suffix}"
            if len(routing_no) > BC_CODE_MAX and not placeholder:
                check(sku, routing_no, f"Routing No. is {len(routing_no)} characters; BC allows {BC_CODE_MAX}. Shorten the scheme or use the sub-assembly item no.")
            headers.append({
                "No.": routing_no,
                "Description": f"{desc} - {sku}"[:100],
                "Type": ROUTING_TYPE,
                "Status": ROUTING_STATUS,
                "New SKU": sku,
                "Routing reference": rr,
                "Product line": pl,
                "Lines": 0,
                "Run time total (min)": 0.0,
                "Data status": "OK",
            })
            hdr = headers[-1]
            ops = []
            for _, L in by_rr.sort_values("Operation No.").iterrows():
                ops.append({
                    "op_no": int(L["Operation No."]), "op_code": L["Op code"], "desc": L["Operation"],
                    "wc": L["Work Center"], "run": L["Run time (min/unit)"] or 0.0,
                    "setup": L["Setup time (min)"] or 0, "status": L["Data status"],
                    "note": "",
                })
                if L["Data status"] == "CHECK":
                    hdr["Data status"] = "CHECK"
                    check(sku, routing_no, f"Op {int(L['Operation No.'])} {L['Op code']} at {L['Work Center']}: a contributing labor line is MISSING / SUSPECT / REJECTED / ASSUMED (see database Labor Lines).")
            for refs, op_no, op_code, desc_, note in EXTRA_OPS:
                if rr in refs:
                    fin_wc = next((o["wc"] for o in ops if o["op_code"] == "F-MOUNT"), f"{location}-FIN")
                    ops.append({"op_no": op_no, "op_code": op_code, "desc": desc_, "wc": fin_wc,
                                "run": 0.0, "setup": 0, "status": "OK", "note": note})
            ops.sort(key=lambda o: o["op_no"])
            for i, o in enumerate(ops):
                nxt = ops[i + 1]["op_no"] if i + 1 < len(ops) else None
                prv = ops[i - 1]["op_no"] if i > 0 else None
                lines.append({
                    "Routing No.": routing_no,
                    "Version Code": None,
                    "Operation No.": str(o["op_no"]),
                    "Next Operation No.": str(nxt) if nxt else None,
                    "Previous Operation No.": str(prv) if prv else None,
                    "Type": "Work Center",
                    "No.": o["wc"],
                    "Description": o["desc"],
                    "Setup Time": o["setup"],
                    "Run Time": round(float(o["run"]), 2),
                    "Run Time Unit of Meas. Code": RUN_TIME_UOM,
                    "Setup Time Unit of Meas. Code": RUN_TIME_UOM,
                    "Concurrent Capacities": 1,
                    "Routing Link Code": LINK_CODES.get(o["op_code"]),
                    "Op code": o["op_code"],
                    "New SKU": sku,
                    "Routing reference": rr,
                    "Data status": o["status"],
                    "Note": o["note"],
                })
                hdr["Lines"] += 1
                hdr["Run time total (min)"] += float(o["run"])
            items.append({
                "New SKU": sku,
                "Routing reference": rr,
                "Item carrying this routing": sku if suffix == "" else None,
                "Item role": "Final product (Lvl 0 SKU)" if suffix == "" else desc,
                "Routing No.": routing_no,
                "Note": "" if suffix == "" else "Fill in the sub-assembly item no. from the production BOM; if that item no. should also be the Routing No., replace it on the Routing Header / Routing Line sheets.",
            })
        if n_check_sku:
            pass

    # ---- workbook ----------------------------------------------------------
    wb = Workbook()
    ws_read = wb.active
    ws_read.title = "README"
    ws_h = wb.create_sheet("Routing Header")
    ws_l = wb.create_sheet("Routing Line")
    ws_w = wb.create_sheet("Work Center")
    ws_i = wb.create_sheet("Item routing")
    ws_c = wb.create_sheet("Checks")

    hcols = ["No.", "Description", "Type", "Status", "New SKU", "Routing reference", "Product line",
             "Lines", "Run time total (min)", "Data status"]
    header(ws_h, hcols, [24, 60, 8, 8, 30, 16, 11, 7, 14, 11])
    write_rows(ws_h, headers, hcols, "RoutingHeader", {"Run time total (min)": NUM_MIN})
    for r, h in enumerate(headers, 2):
        if len(h["No."]) > BC_CODE_MAX:
            ws_h.cell(row=r, column=1).fill = FILL_ASK
        if h["Data status"] == "CHECK":
            ws_h.cell(row=r, column=10).fill = FILL_ASK

    lcols = ["Routing No.", "Version Code", "Operation No.", "Next Operation No.", "Previous Operation No.",
             "Type", "No.", "Description", "Setup Time", "Run Time", "Run Time Unit of Meas. Code",
             "Setup Time Unit of Meas. Code", "Concurrent Capacities", "Routing Link Code",
             "Op code", "New SKU", "Routing reference", "Data status", "Note"]
    header(ws_l, lcols, [24, 8, 10, 10, 10, 12, 12, 32, 9, 9, 12, 12, 11, 14, 12, 30, 16, 10, 70])
    write_rows(ws_l, lines, lcols, "RoutingLine", {"Run Time": NUM_MIN})
    for r, L in enumerate(lines, 2):
        ws_l.cell(row=r, column=9).fill = FILL_EDIT
        ws_l.cell(row=r, column=14).fill = FILL_EDIT
        if L["Data status"] == "CHECK":
            ws_l.cell(row=r, column=10).fill = FILL_ASK
            ws_l.cell(row=r, column=18).fill = FILL_ASK

    wcols = ["No.", "Name", "Unit of Measure Code", "Capacity", "Efficiency", "Note"]
    header(ws_w, wcols, [14, 40, 12, 9, 10, 60])
    wrows = [{"No.": no, "Name": name, "Unit of Measure Code": RUN_TIME_UOM, "Capacity": 1,
              "Efficiency": 100, "Note": "Create (or confirm) before importing routing lines."}
             for no, name in work_centers]
    write_rows(ws_w, wrows, wcols, "WorkCenter")

    icols = ["New SKU", "Routing reference", "Item carrying this routing", "Item role", "Routing No.", "Note"]
    header(ws_i, icols, [30, 16, 26, 36, 24, 90])
    write_rows(ws_i, items, icols, "ItemRouting")
    for r, it in enumerate(items, 2):
        if it["Item carrying this routing"] is None:
            ws_i.cell(row=r, column=3).fill = FILL_EDIT

    ccols = ["New SKU", "Routing No.", "What to resolve"]
    header(ws_c, ccols, [30, 24, 120])
    write_rows(ws_c, checks, ccols, "Checks")

    ws_read.column_dimensions["A"].width = 120
    n_ok = sum(1 for h in headers if h["Data status"] == "OK" and not h["New SKU"].startswith("TBD-") and len(h["No."]) <= BC_CODE_MAX)
    text = [
        ("Business Central routing import package", F_TITLE),
        (f"Built {date.today().isoformat()} from '{db_path.name}' (location {location}). "
         f"{len(headers)} routings, {len(lines)} routing lines, {len(work_centers)} work centers; "
         f"{n_ok} routings are import-ready, {len(checks)} items on the Checks sheet.", F_BASE),
        ("", F_BASE),
        ("SHEETS", F_BOLD),
        ("Routing Header - one row per routing, in the column layout of BC table 99000763 (No., Description, Type, Status). Extra columns to the right are for reference and are not BC fields.", F_BASE),
        ("Routing Line   - one row per operation, layout of BC table 99000764. Next / Previous Operation No. are chained in sequence. Run Time is person-minutes per unit; Setup Time is 0 (yellow - fill in). Routing Link Code is a default per operation (yellow - adjust per component, e.g. G-INSTALL-01/02/03).", F_BASE),
        ("Work Center    - the work centers referenced by the lines (BC table 99000754). Create or confirm them first.", F_BASE),
        ("Item routing   - which item carries which routing. The final product is the Lvl 0 SKU; sub-assembly item numbers (generator with accessories, trailer, power module, battery, compressor, prepared accessories, wire harness) are yellow - fill in from the production BOM.", F_BASE),
        ("Checks         - what must be resolved before a routing is certified: missing Lvl 0 nomenclature, Routing No. over 20 characters, operations whose minutes are MISSING / SUSPECT / REJECTED / ASSUMED.", F_BASE),
        ("", F_BASE),
        ("HOW TO IMPORT", F_BOLD),
        ("1. In BC, create a configuration package with tables 99000754 (Work Center), 99000763 (Routing Header) and 99000764 (Routing Line), export the package to Excel, and paste these sheets' BC columns into the matching tabs (keep only the BC-named columns).", F_BASE),
        ("2. Import and apply: Work Center first, then Routing Header, then Routing Line.", F_BASE),
        ("3. Review each routing, set its Status to Certified, and attach it to its item (Item card -> Routing No.). Sub-assembly items need their own item cards and production BOMs first.", F_BASE),
        ("4. Routings whose Data status is CHECK can be imported now but should stay 'New' until the flagged minutes are confirmed in the database workbook; rebuild both files afterwards.", F_BASE),
        ("", F_BASE),
        ("ROUTING NUMBER SCHEME", F_BOLD),
        ("Final product routing = the Lvl 0 SKU. Sub-assembly routings = SKU + one letter: G generator, P power module, B battery, T trailer, C compressor, A prepared accessories, W wire harness. BC Routing No. holds 20 characters; over-length numbers are flagged on Checks and shaded orange. If the sub-assembly item no. is preferred as the Routing No., replace it on both routing sheets.", F_BASE),
        ("", F_BASE),
        ("RULES CARRIED OVER FROM THE DATABASE", F_BOLD),
        ("Operations per routing follow the BOM trees and the 2026-11-02 decisions: wire harness 20 min per component (R-WIRE), battery build, prepared accessories, PM / generator / compressor accessories install, trailer build, final mount -> decal -> Pre QC -> PDI -> Final QC. ETO units route their generator / PM / final work to the ETO work center under the same routing references.", F_BASE),
        ("Zero-minute operations are left out, except F-DECAL (kept so the decal component can link) and operations whose minutes are still to be filled in (Data status CHECK).", F_BASE),
    ]
    for i, (txt, font) in enumerate(text, 1):
        c = ws_read.cell(row=i, column=1, value=txt)
        c.font = font
        c.alignment = Alignment(wrap_text=True, vertical="top")

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"wrote {out}  routings={len(headers)} lines={len(lines)} work_centers={len(work_centers)} checks={len(checks)} import_ready={n_ok}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(Path(sys.argv[1]), Path(sys.argv[2]))
