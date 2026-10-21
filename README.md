# Labor Capacity Tool

A Streamlit web app for labor capacity planning across three manufacturing facilities: **Henderson**, **Spartanburg**, and **Cypress**.

Built so planners and execs can answer:
- *"Do we have enough people and cells to hit this month's plan?"*
- *"Which stations are the bottleneck, and is the fix headcount or fixtures?"*
- *"How long does each unit take to build?"*

---

## Features

- **Multi-location** — per-facility headcount, station setup, scenarios, and saved schedules.
- **Three ways to enter a plan** — upload your normal schedule as CSV **or as the Excel workbook itself** (any column naming works, header can sit below a title row or on a later tab), **paste a SKU list straight out of a spreadsheet**, or type a handful of SKUs for a quick what-if.
- **Export & Excel link** — download the whole database (catalogs, crew, settings, scenarios, saved schedules, process flow) or the current analysis as one `.xlsx`, or point Excel at the live CSV catalogs with Power Query so a sheet refreshes itself.
- **Import from Excel** — edit labor minutes (or add a SKU row) on the exported catalog sheet, upload it back, preview exactly which cells change, and save through the same SHA-locked merge the in-app editors use.
- **Eight interactive tabs:**
  - 🏠 **Overview** — executive dashboard with status banner, KPIs, recommended actions
  - 📋 **Executive Summary** — monthly executive labor roll-up (new-hire ramp, absenteeism, overtime) saved per facility as reloadable snapshots, with a side-by-side multi-facility table
  - 📆 **Calendar** — week and day roll-ups of the whole month's plan, independent of the sidebar time window
  - 📊 **Capacity** — per-station utilization (labor + throughput), with 🔋 Battery throughput embedded
  - 🔧 **Recommendations** — bottleneck mitigation playbook + rotation candidates
  - ⏱ **Build Time** — total labor + lead time per FG/Accessory pairing
  - 🔀 **Process Flow** — editable per-class flow chart of each unit's route
  - 📁 **Data & Setup** — Schedule · Sequence · Customer suffix cleanup · Machine catalog · Accessory catalog · Items · Item packages · Reconciliation & Apply · Data Quality · Export & Excel link
- **Editable catalogs** — every CSV/JSON catalog can be edited in-app and saved back to GitHub. Cell-level merge protects concurrent edits.
- **Save / load build plans** — both manual scenarios and uploaded schedules persist per facility to GitHub so teammates can reload them.
- **Quiet analytics** — anonymous session + save telemetry under a discreet `⚙️ Admin` expander; local to each Streamlit Cloud container.

---

## Quick Start

### Local

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

### Streamlit Cloud (team deploy)

1. Push this repo to GitHub.
2. Connect the repo at [share.streamlit.io](https://share.streamlit.io).
3. **Add a GitHub Personal Access Token** so the team can save catalog edits / scenarios / schedules back to the repo:
   - Generate a fine-grained PAT at `https://github.com/settings/personal-access-tokens` scoped to just this repo, with **Contents: Read and write** permission. Set a long expiration (1 year).
   - In Streamlit Cloud → **Settings → Secrets**, add:
     ```toml
     github_token = "github_pat_xxxxxxxxxxxxxxxxxxxxxxxx"
     ```
   - Read-only browsing works without the token; only the **💾 Save** buttons need it.

See [`MAINTAINING.md`](./MAINTAINING.md) for ops tasks (rotating the PAT, recovering from a bad commit, adding a new facility, etc.).

---

## Before sharing the URL with the team

Quick pre-flight checklist — the things that quietly break first impressions:

- [ ] Footer email matches whoever the team should ping for help (`app.py` — search for the contact email).
- [ ] GitHub PAT in Streamlit Secrets is current and isn't about to expire.
- [ ] At least one **other person** has Streamlit Cloud workspace admin access. Single-person admin = bus-factor risk.
- [ ] Sample `data/may_schedule.csv` doesn't contain real customer data. (It shouldn't — see the privacy scrub commit — but worth a `grep`.)
- [ ] You know where the admin analytics live: bottom of the sidebar → `⚙️ Admin` expander.

---

## Schedule upload

### Required columns

The loader auto-matches column names from your Excel export — these are all the same column to the app:

| Internal name | Aliases the loader accepts |
|---|---|
| `LOCATION` | `Location` · `Facility` · `Site` · `Plant` |
| `FG SKU ID` | `FG SKU` · `Finished Good` · `SKU ID` |
| `FG ACCRY SKU ID` | `Accessory SKU` · `Acc SKU` · `Accry SKU` |
| `BUILD QTY` | `Qty` · `Quantity` · `Build Quantity` |
| `PRODUCTION MONTH` | `Prod Month` · `Production Date` · `Month` |
| `CUSTOMER NAME` (optional) | `Customer` · `Cust` |

Extra columns are silently ignored. UTF-8 BOM (Excel default on Windows) is stripped automatically. `PRODUCTION MONTH` accepts `May-26`, `26-Apr` (carryover), `5/8/2026`, `2026-05-08`, and most other date formats.

If a required column truly isn't there, the upload UI shows a yellow banner naming the missing field and listing the aliases it tried.

### Excel workbooks

Upload the `.xlsx` / `.xlsm` straight from the planner — no Save-As-CSV step. The app uses the first sheet whose header row carries the required columns, so a title row above the header or the plan living on a later tab both work. Date cells become `YYYY-MM-DD`, whole-number quantities stay whole, and what **💾 Save schedule** stores is the converted CSV. The sidebar confirmation pill names the sheet it read. (`.xls`, the pre-2007 format, isn't supported — save it as `.xlsx`.)

### Don't have a CSV yet?

Click **📥 Download blank template** under the file uploader in the sidebar. You get an empty CSV with the right headers and two example rows.

### Privacy

Uploaded schedules stay **only in your browser** unless you explicitly click **💾 Save schedule** in the sidebar. Saving pushes the CSV to `data/uploaded_schedules.json` on GitHub, where teammates planning the same facility can reload it.

---

## Paste a SKU list from Excel

For a quick plan there's no need to build a schedule file at all. Pick **✏️ Paste or type SKUs** in the sidebar, open **📋 Paste a list from Excel**, and paste the cells you copied (Ctrl+C out of Excel, Ctrl+V into the box).

### What you can paste

| What you paste | How it's read |
|---|---|
| One SKU per line | FG SKU, qty 1 each |
| `SKU` + number | FG SKU + quantity |
| `SKU` + `SKU` | FG SKU + accessory |
| `SKU` + `SKU` + number | FG SKU + accessory + quantity |

- **Column order doesn't matter.** Pasted values are matched against the machine and accessory catalogs to work out which column is which; the `-A###` accessory naming pattern is the fallback for SKUs the catalogs don't know yet.
- **A header row is optional** — `FG SKU ID` / `FG ACCRY SKU ID` / `BUILD QTY` are understood, as are friendlier spellings like `Model`, `Accessory`, `Quantity`.
- **Extra columns are ignored** — paste a whole ERP export row (LOCATION, CUSTOMER, dates…) and only the SKU / accessory / qty columns get picked up.
- **Formats:** tab-separated (the Excel default), CSV, semicolon exports, space-aligned text, or a bare list of SKUs. Excel debris — non-breaking spaces, wrapping quotes, the leading `'` text marker, `1,200` thousands separators — is cleaned up.

### Before anything is committed

The panel previews exactly what it read: row and unit counts, which column became which, duplicates combined, blank quantities counted as 1, and any SKU missing from the catalogs (those still import — their labor just isn't counted until the SKU is added under **📁 Data & Setup**). Lines it can't use are listed with their line number and the reason.

Then **➕ Add** appends to the table below and **🔁 Replace** starts fresh. Either way the rows land in the normal manual-entry table, so you can still fix a SKU, change a quantity, or add a few more by hand — and save the result as a named scenario.

### The `#` column

The table numbers its own rows. A row takes the next number as soon as it has an **FG SKU or an Accessory SKU** in it; a row with neither stays blank and doesn't consume a number, so the count always matches the rows that will actually build.

That holds however the row arrived — pasted through the panel, pasted straight into the table from Excel, or typed in by hand — and deleting a row closes the gap rather than leaving a hole. The column is display-only: it isn't part of the schedule and isn't saved with a scenario.

---

## Export & link to Excel

**📁 Data & Setup → 📤 Export & Excel link** gives three ways to get the data out:

1. **Database workbook** — one `.xlsx` with a sheet per data file: the four CSV catalogs exactly as stored, plus `facility_crew.json`, `facility_settings.json`, `scenarios.json`, `exec_scenarios.json`, `uploaded_schedules.json` and `process_flow.json` flattened into plain tables (long key/value format for the executive scenarios, one row per SKU line for scenarios, one row per facility × station for crew). A **README** sheet lists what each sheet is and which file it came from. Tick *Include every row of every saved schedule* to add the big sheet (every saved schedule's rows, union of their columns) — it's slower to build and several MB.
2. **Analysis workbook** — this session's schedule as loaded, one row per unit with its per-station labor, the capacity-by-station table behind the Capacity tab, and the crew + sidebar settings that produced them.
3. **Live link** — the CSV catalogs are plain files on GitHub, so Excel can read them with **Data → Get Data → From Web** and **Refresh All** on demand. The panel lists each file's raw URL (`https://raw.githubusercontent.com/<repo>/<branch>/data/<file>`) and a ready-to-paste Power Query script. Read-only: edits go back through the in-app editors or **📥 Import from Excel** (below), so the SHA-locked merge keeps protecting concurrent saves. The JSON files aren't one-click links (they need transform steps); use the database workbook for those. If the repo is ever made private the URLs need a token.

The workbook is built from the data files as deployed, so a save shows up in the export after the usual ~1 minute Streamlit Cloud rebuild.

### Import from Excel (write-back)

**📁 Data & Setup → 📥 Import from Excel** closes the loop. Export the database workbook, edit the **Machine catalog** / **Accessory catalog** sheet in Excel — change labor minutes, a description, a routing cell, or add a SKU row at the bottom — save, and upload the file (`.xlsx` / `.xlsm`; the whole export or a single sheet both work, and a sheet that isn't named is recognised by its header row).

- **Preview first.** The view lists every cell that would change (SKU, column, old → new), every new SKU with the values it will carry, and every catalog SKU that is *not* on the sheet. Nothing is written until you click **💾 Save**.
- **Never deletes.** SKUs missing from the sheet are left exactly as they are; columns missing from the sheet are left alone on every row; extra columns are ignored. A blank cell never changes anything — type `0` to zero a value.
- **Validated like the editor.** Labor cells must be numbers ≥ 0, routing cells (`Final Station HND` …) must be one of the options the in-app dropdown offers (case is corrected for you), a SKU may not start with `=`, `+`, `-` or `@`, and a SKU may appear only once. Any of those blocks the save until the file is fixed.
- **Same SHA-locked merge.** Save fetches the latest CSV from GitHub, applies only the previewed cells, appends the new rows (a SKU someone added in the meantime is skipped, not overwritten), and pushes with the file's SHA as an optimistic lock, retrying once on a conflict. A colleague's edit of another cell survives. The commit reads like `Update machine catalog via Excel import (3 cells, 1 new SKU(s))`, and `Last Modified` is stamped on every changed row.

The reader and the diff live in `core/excel_io.py` (`read_catalog_workbook`, `diff_catalog_sheet`); `tests/test_excel_catalog_import.py` covers header validation, numeric coercion, the untouched-row rules and the save.

---

## Data files (under `data/`)

| File | Purpose |
|---|---|
| `machine_clean.csv` | Machine (FG SKU) labor times by station + Bat count |
| `acc_clean.csv` | Accessory SKU labor times |
| `may_schedule.csv` | Sample production schedule (scrubbed of real customer data) |
| `item_master.csv` | Items catalog — defaults + family variants + per-accessory entries |
| `item_packages.csv` | Named bundles (CWP / NE / AWP) |
| `facility_crew.json` | Per-facility station headcount (HC / Stations / Crew) |
| `facility_settings.json` | Per-facility planning assumptions (new-hire share & productivity, absenteeism, overtime, efficiency, safety, excluded stations) |
| `scenarios.json` | Saved manual-entry build plans |
| `exec_scenarios.json` | Saved Executive Summary snapshots (assumptions + computed metrics, per facility) |
| `uploaded_schedules.json` | Saved uploaded CSVs (per facility) |
| `process_flow.json` | Manufacturing precedence graph (edges, per unit class) |
| `usage_log.jsonl` | **Local-only**, gitignored and untracked — anonymous session telemetry for the admin dashboard, created by the running app |

---

## Configuration

- **Sidebar** — pick facility, switch between CSV upload and manual SKU entry, edit working time (days / shift / safety / efficiency), per-facility station headcount, save scenarios / schedules.
- **`core/constants.py`** — station defaults, FINAL_LABOR by class, battery type rules, utilization thresholds (`UTIL_THRESHOLD_OVER/NEAR/TIGHT`).

---

## Concurrency

Catalog and schedule saves use **cell-level merge** with SHA-locked optimistic concurrency:

- Two users editing different cells → both saves succeed, no overwrites.
- Two users editing the same cell → last writer wins; `Last Modified` + git history give the audit trail.
- The **🔄 Refresh** banner appears when someone else commits while you have the tab open.

---

## Glossary

In the running app: open the sidebar's **ℹ️ Help & glossary** expander. It's organized into five tabs:

- **🚀 Quick start** — status colors, sidebar map, "where to find common features"
- **📐 How the model works** — time units, labor vs throughput utilization, safety / efficiency
- **🏷 Catalog & stations** — unit classes, SKU naming, the full 14-station map
- **💾 Saving your work** — what gets persisted where (scenarios vs uploaded schedules vs catalog edits)
- **🔧 For admins** — GitHub token, wild-CSV behavior, redeploy timing

---

## Repository hygiene

- Don't commit secrets — `.streamlit/secrets.toml` is in `.gitignore`.
- `data/usage_log.jsonl` is local-only (gitignored). The Streamlit Cloud container writes it for the admin dashboard; it resets on every redeploy.
- If you change a column in any CSV, **bump `_LOADER_SCHEMA_VERSION` in `app.py`** so the cached frames refresh. See [`MAINTAINING.md`](./MAINTAINING.md).
