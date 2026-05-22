# Labor Capacity Tool

A Streamlit web app for labor capacity planning across three manufacturing facilities: **Henderson**, **Spartanburg**, and **Cypress**.

Built so planners and execs can answer:
- *"Do we have enough people and cells to hit this month's plan?"*
- *"Which stations are the bottleneck, and is the fix headcount or fixtures?"*
- *"How long does each unit take to build?"*

---

## Features

- **Multi-location** — per-facility headcount, station setup, scenarios, and saved schedules.
- **Two ways to enter a plan** — upload your normal Excel schedule (any column naming works), or type a handful of SKUs for a quick what-if.
- **Six interactive tabs:**
  - 🏠 **Overview** — executive dashboard with status banner, KPIs, recommended actions
  - 📊 **Capacity** — per-station utilization (labor + throughput), with 🔋 Battery throughput embedded
  - 🔧 **Recommendations** — bottleneck mitigation playbook + rotation candidates
  - ⏱ **Build Time** — total labor + lead time per FG/Accessory pairing
  - 🔀 **Process Flow** — editable per-class flow chart of each unit's route
  - 📁 **Data & Setup** — Schedule · Machine catalog · Accessory catalog · Items · Item packages · Reconciliation · Data Quality
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

### Don't have a CSV yet?

Click **📥 Download blank template** under the file uploader in the sidebar. You get an empty CSV with the right headers and two example rows.

### Privacy

Uploaded schedules stay **only in your browser** unless you explicitly click **💾 Save schedule** in the sidebar. Saving pushes the CSV to `data/uploaded_schedules.json` on GitHub, where teammates planning the same facility can reload it.

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
| `scenarios.json` | Saved manual-entry build plans |
| `uploaded_schedules.json` | Saved uploaded CSVs (per facility) |
| `process_flow.json` | Manufacturing precedence graph (edges, per unit class) |
| `usage_log.jsonl` | **Local-only**, gitignored — anonymous session telemetry for the admin dashboard |

---

## Configuration

- **Sidebar** — pick facility, switch between CSV upload and manual SKU entry, edit working time (days / shift / safety / efficiency), per-facility station headcount, save scenarios / schedules.
- **`core/constants.py`** — station defaults, FINAL_LABOR by class, battery count overrides, utilization thresholds (`UTIL_THRESHOLD_OVER/NEAR/TIGHT`).

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
- **🏷 Catalog & stations** — unit classes, SKU naming, the full 13-station map
- **💾 Saving your work** — what gets persisted where (scenarios vs uploaded schedules vs catalog edits)
- **🔧 For admins** — GitHub token, wild-CSV behavior, redeploy timing

---

## Repository hygiene

- Don't commit secrets — `.streamlit/secrets.toml` is in `.gitignore`.
- `data/usage_log.jsonl` is local-only (gitignored). The Streamlit Cloud container writes it for the admin dashboard; it resets on every redeploy.
- If you change a column in any CSV, **bump `_LOADER_SCHEMA_VERSION` in `app.py`** so the cached frames refresh. See [`MAINTAINING.md`](./MAINTAINING.md).
