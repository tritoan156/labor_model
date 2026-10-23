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

- `app.py` — the entire UI (~10,000 lines): all tabs, sidebar, catalog editors.
  The top-level tab strip is a `st.segmented_control` keyed on
  `st.session_state["_active_tab"]`, and `main()` builds only the selected
  tab's body (`st.tabs` can't tell the server which tab is in front, so it
  used to build all eight on every click). Keep it that way: a new tab is a
  `TAB_*` constant plus one `elif` in `main()`.
- `core/` — `data_loader.py`, `labor_calculator.py`, `constants.py`,
  `catalog_storage.py` (the one GitHub GET/PUT every save goes through),
  plus the storage modules for scenarios, uploaded schedules, facility crew,
  facility settings, executive scenarios and the process flow,
  `excel_io.py` (Excel upload / export), `usage_tracker.py`,
  `data_validator.py`.
- `data/` — `machine_clean.csv` (FG SKU catalog), `acc_clean.csv` (accessory
  catalog), `item_master.csv` / `item_packages.csv`, and JSON for crew, crew
  settings, scenarios, executive scenarios, uploaded schedules and the flow.
  `usage_log.jsonl` is written by the running app only — git-ignored and
  untracked, so it never shows up in `git status`.
- `MAINTAINING.md` — ops checklist (PAT rotation, schema bumps, adding a
  facility, routing/pinning gotchas). **Read this first.**

## Hard rules (don't break these)

1. **Commit straight to `main`** — staging exists but is intentionally unused
   right now.
2. **`_LOADER_SCHEMA_VERSION` in `app.py` is a cache-buster.** Bump it after ANY
   CSV column add/remove/rename/type change, or users get stale cached data.
   The current value lives in `app.py` (search for the constant) — this doc
   doesn't repeat it so it can't drift. The bump takes effect on the first script run after the
   deploy — no Cloud reboot needed. The CSV loaders are cached on file mtime +
   this constant, passed as plain (non-underscore) `st.cache_data` arguments;
   `tests/test_cache_keys.py` fails if anyone re-prefixes them (Streamlit skips
   `_`-prefixed params when building the cache key, which is how this used to
   be broken).
3. **Commit ritual:** `git add` specific files → commit with a
   `Co-Authored-By: Claude` trailer → `git pull --rebase origin main` →
   `git push origin main`. (`data/usage_log.jsonl` is untracked and
   git-ignored, so there is nothing to stash first.)
4. Local dev on Windows: run Python as `./.venv/Scripts/python.exe`. Syntax
   check with `python -m compileall -q app.py`.
5. **The stale-module shim at the top of `app.py`** (bare `import core.X`
   list → `_stale` sentinels → reload list) must be kept in step whenever you
   add/remove a `core/` module or add a name `app.py` imports. See
   `MAINTAINING.md` → "The stale-module sentinel".
6. **No `st.rerun()` inside the sidebar** — handlers call `_defer_rerun()` and
   `render_sidebar()` fires it last. An early rerun wipes every sidebar widget
   built after it (the working-day count fell to 1 and every capacity number
   was ~20× off). See `MAINTAINING.md` → "Sidebar buttons never call
   `st.rerun()` directly".

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
  another team, e.g. Henderson → Com Accessories). A **new SKU** gets all 15
  cells **seeded** by `core/sku_routing.RoutingSeeder` from the most similar
  catalog row (same family + class → same family → the facility column's
  majority → the station default), on every add path — the add-SKU form, the
  XXX-placeholder form, the Overview's "Import N missing SKU(s)" button and
  the Excel import's new rows — and each preview shows what the SKU got.
  `_machine_family` / `_machine_class` live in that module too.
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
- **Plan month is a seeded widget:** `render_sidebar` seeds
  `st.session_state[PLAN_MONTH_KEY]` before building the picker (no `index=`),
  and the main-panel **📆 Plan <month> instead** button — offered when the
  loaded schedule's dominant PRODUCTION MONTH differs from the Plan month —
  switches it by writing `PLAN_MONTH_SEED_KEY` and rerunning; the picker's
  options are the rolling window plus the schedule's own months
  (`SCHEDULE_MONTHS_KEY`). The pill says "Loaded N of M rows" and lists rows
  dropped for having no BUILD QTY (`load_schedule` attrs `source_rows` /
  `no_qty_rows` / `other_location_rows`). A fresh session opens on the
  facility's newest saved schedule (`_load_schedule_df` step 3c), tagged
  `_active_schedule_auto_for` so a facility switch re-resolves while any
  upload / 📥 Load / reshape ends the automatic pick; a facility with no
  saves keeps the bundled sample. `tests/test_schedule_load_moment.py`.
- **p-min/p-hr:** all person-minute displays also show person-hours via
  `_fmt_min_hr(...)`.

## Open follow-ups (not done)

- Many tables now carry both p-min and p-hr columns — wider; a global toggle may
  help if width becomes an issue.

## Where to start reading

`MAINTAINING.md`, then `tab_overview` and the catalog-editor functions in
`app.py`.
