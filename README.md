# Labor Capacity Tool

A Streamlit web app for labor capacity planning across three manufacturing facilities: Henderson, Spartanburg, and Cypress.

## Features

- **Multi-location support** — per-facility headcount and schedules
- **Real-time analysis** — labor demand, capacity utilization, required headcount, bottleneck detection
- **Editable catalogs** — machine, accessory, items, packages, process flow — all persist to GitHub
- **Scenarios** — save and reload named build plans
- **Reconciliation** — compare item-level vs aggregate labor; apply corrections back to the catalog
- **6 interactive tabs:**
  - 🏠 **Overview** — executive dashboard with status banner, KPIs, recommended actions
  - 📊 **Capacity** — per-station utilization (labor + throughput), with 🔋 battery section embedded
  - 🔧 **Recommendations** — bottleneck mitigation playbook + rotation candidates
  - ⏱ **Build Time** — total labor and lead time per FG/Accessory pairing
  - 🔀 **Process Flow** — visual flow chart of each unit's route, with editable per-class edges
  - 📁 **Data & Setup** — Schedule · Machine catalog · Accessory catalog · Items · Item packages · Reconciliation & Apply · Data Quality

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
3. **Add a GitHub Personal Access Token** so the team can save catalog edits back to the repo:
   - Generate a token at `https://github.com/settings/tokens` with the **`repo`** scope.
   - In Streamlit Cloud → **Settings → Secrets**, add:
     ```toml
     github_token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxx"
     ```
   - Read-only browsing works without the token; only the **💾 Save** buttons need it.

## Data files (under `data/`)

| File | Purpose |
|---|---|
| `machine_clean.csv` | Machine (FG SKU) labor times by station + Bat count |
| `acc_clean.csv` | Accessory SKU labor times |
| `may_schedule.csv` | Sample production schedule |
| `item_master.csv` | Items catalog — defaults + family variants + per-accessory entries |
| `item_packages.csv` | Named bundles (CWP / NE / AWP) |
| `facility_crew.json` | Per-facility station headcount (HC / Stations / Crew) |
| `scenarios.json` | Saved manual-entry build plans |
| `process_flow.json` | Manufacturing precedence graph (edges, per unit class) |

### Upload-your-own schedule CSV

Required columns:
- `LOCATION` (`Henderson`, `Spartanburg`, or `Cypress`)
- `PRODUCTION MONTH` (e.g. `Jun-26` for current month; `26-May` for carryover)
- `FG SKU ID`
- `FG ACCRY SKU ID`
- `BUILD QTY`

## Configuration

- **Sidebar** — pick facility, switch between CSV upload and manual SKU entry, edit working time (days / shift / safety / efficiency), per-facility station headcount, save scenarios.
- **`core/constants.py`** — station defaults, FINAL_LABOR by class, battery count overrides.

## Concurrency

Catalog saves use **cell-level merge** with SHA-locked optimistic concurrency:
- Two users editing different cells → both saves succeed, no overwrites
- Two users editing the same cell → last writer wins; `Last Modified` + git history give the audit trail
- The **🔄 Refresh** banner appears when someone else commits while you have the tab open

## Glossary

See the **ℹ️ Help & glossary** expander in the sidebar for definitions of:
- `p-min` vs `cal-min`, cycle time, lead time
- Headcount / Stations / Cells / Crew per unit
- Safety + Efficiency factors
- Unit classes (STD / HS / HT)
- All 13 station keys
- Placeholder conventions (`XXX`, `A999`)
- Status colors
