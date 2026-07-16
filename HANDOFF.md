# Labor Capacity Model — catch-up briefing

A self-contained briefing for whoever (person or AI assistant) is picking up
this project fresh. For the day-to-day ops checklist, see `MAINTAINING.md`.

## What this is

A Streamlit labor-capacity planning app for 3 manufacturing facilities
(**Henderson, Spartanburg, Cypress**). It plans monthly production schedules
against per-station headcount + throughput capacity.

## Repo & deploy

- GitHub: `tritoan156/labor_model` (public; customer data scrubbed). Deployed on
  Streamlit Cloud, which reads/writes `data/*.csv` and `data/*.json` straight
  from the `main` branch via a GitHub PAT in Streamlit Secrets (`github_token`).
- Stack: Streamlit 1.58 (Cloud) / 1.57 (local venv), pandas, plotly, openpyxl.

## Layout

- `app.py` — the entire UI (~7,600 lines): all tabs, sidebar, catalog editors.
- `core/` — `data_loader.py`, `labor_calculator.py`, `constants.py`,
  `catalog_storage.py`, plus scenario/schedule/facility/process-flow storage,
  `usage_tracker.py`, `data_validator.py`.
- `data/` — `machine_clean.csv` (FG SKU catalog), `acc_clean.csv` (accessory
  catalog), JSON for crew/scenarios/schedules/flow, and local-only
  `usage_log.jsonl`.
- `MAINTAINING.md` — ops checklist (PAT rotation, schema bumps, adding a
  facility, routing/pinning gotchas). **Read this first.**

## Hard rules (don't break these)

1. **Commit straight to `main`** — staging exists but is intentionally unused
   right now.
2. **`_LOADER_SCHEMA_VERSION` in `app.py` is a cache-buster.** Bump it after ANY
   CSV column add/remove/rename/type change, or users get stale cached data.
   Currently `8`. A bump may require a Cloud reboot (Manage app → Reboot).
3. **Commit ritual:** stash `data/usage_log.jsonl` (local telemetry, never
   commit) → `git add` specific files → commit with a
   `Co-Authored-By: Claude` trailer → `git pull --rebase origin main` →
   `git stash pop` → `git push origin main`.
4. Local dev on Windows: run Python as `./.venv/Scripts/python.exe`. Syntax
   check with `python -m compileall -q app.py`.

## Key architecture you'll hit

- **Per-facility station routing:** stored in `machine_clean.csv` as suffixed
  columns for 5 stations × 3 facilities — `Final Station {CODE}` / `AccKIT
  Station {CODE}` / `PDI Station {CODE}` / `Wire Station {CODE}` / `Undercarriage
  Station {CODE}`, where `{CODE}` is `HND`/`SPB`/`CYP` from
  `core/constants.FACILITY_CODE`. `_project_facility_routing(...)` in `app.py`
  projects the active facility's columns onto legacy names at load so the
  compute path stays facility-agnostic. The catalog editor shows only the active
  facility's 5 routing dropdowns. `Undercarriage Station` is a second-hop
  reroute (moves what Final-Station routing dropped at Undercarriage on to
  another team, e.g. Henderson → Com Accessories).
- **Catalog editors:** the live ones are `tab_floor_verification_machine` and
  `tab_floor_verification_accessory` (dispatched from the Data & Setup radio). An
  older monolithic `tab_floor_verification` was deleted as dead code — don't
  resurrect it.
- **Frozen/pinned columns gotcha:** `st.column_config(pinned=True)` silently
  stops freezing when summed pinned-column width exceeds ~60% of the container.
  Keep pinned SKU `width="small"`, Description `width="medium"`.
- **Unit caps:** `_max_units_at_current_mix(capacity, total_units)` returns
  buildable-unit ceilings split by labor cap vs throughput cap; it powers the
  Overview hero KPIs (Units planned / Buildable / Coverage% / Total work) and the
  Capacity tab.
- **p-min/p-hr:** all person-minute displays also show person-hours via
  `_fmt_min_hr(...)`.

## Open follow-ups (not done)

- New SKUs leave the 9 routing columns blank → default routing, won't inherit
  custom per-facility routing. Consider seeding them.
- Confirm the sidebar "type a few SKUs" loader (validation-only path) never feeds
  compute without `_project_facility_routing`.
- Many tables now carry both p-min and p-hr columns — wider; a global toggle may
  help if width becomes an issue.

## Where to start reading

`MAINTAINING.md`, then `tab_overview` and the catalog-editor functions in
`app.py`.
