# Maintaining the Labor Capacity Tool

A short ops checklist for whoever is keeping this app running. If you're
just using the app, you don't need anything in here — that's the
sidebar's **ℹ️ Help & glossary** expander.

---

## Before sharing the URL with the team

1. **Footer email / contact.** `app.py` — search for `tnguyen@anacorp.com`.
   Update if the maintainer changes.
2. **GitHub PAT in Streamlit Secrets.** Confirm it exists and isn't expired.
   See "Rotating the GitHub PAT" below.
3. **Bus-factor admin.** At least one other person should be a
   **Streamlit Cloud workspace admin** (Settings → Sharing). If only you
   have access and you get hit by a bus, nobody can rotate the token.
4. **Repo visibility decision.** The repo (`tritoan156/labor_model`) is
   currently **public**. Customer data has been scrubbed; usage telemetry
   no longer commits. If you ever push real customer schedules, flip it
   private first.

---

## Rotating the GitHub PAT

The 💾 Save buttons write to GitHub via a Personal Access Token. When
the token expires, every save in the app silently fails with
*"GitHub token not configured"*. **There is no automatic warning** — you
notice when a planner reports the error.

When to rotate:
- Every 12 months (matches the longest fine-grained PAT lifetime).
- Immediately if the token is leaked (committed to the repo by mistake,
  shared in a screenshot, etc.).

How to rotate:
1. https://github.com/settings/personal-access-tokens — generate a new
   **fine-grained PAT** scoped to **just this repo** (`tritoan156/labor_model`).
   Permissions: `Contents` → `Read and write`. Set the longest expiration
   you're comfortable with.
2. Copy the token (`github_pat_…`).
3. Streamlit Cloud → app **⋮** → **Manage app** → **Settings** → **Secrets**.
   Paste:
   ```toml
   github_token = "github_pat_…"
   ```
4. Save secrets → app auto-reboots within a minute.
5. Test by editing one labor cell and clicking 💾 Save.
6. Revoke the old PAT on GitHub once the new one works.

---

## When you change a CSV column

`app.py:_LOADER_SCHEMA_VERSION` (currently `8`) is a cache-buster. Every
cached DataFrame loader includes this constant as a `@st.cache_data`
argument, so bumping it forces every user's browser session to re-read
the CSV with the new schema.

**Bump the constant whenever you:**
- Rename a column (e.g. `Compressor` → `ComAcc`).
- Add a column.
- Remove a column.
- Change the type / unit of a column (e.g. minutes → hours).

If you forget, the team will see stale data with the old column name
until they hard-refresh the browser or the Streamlit Cloud container
recycles.

---

## Per-facility station routing + frozen catalog columns

Two pieces of behavior that aren't obvious from the UI:

**Per-facility routing.** Some stations are staffed by different teams at
different plants (e.g. PDI is done by the PDI team in Henderson but by the
Accessories team in Cypress; Henderson has no dedicated undercarriage crew, so
PDS undercarriage work rolls into Com Accessories there). Routing is therefore
stored *per facility* in `data/machine_clean.csv` as suffixed columns —
**5 stations × 3 facilities = 15 columns**:
`Final Station {CODE}`, `AccKIT Station {CODE}`, `PDI Station {CODE}`,
`Wire Station {CODE}`, `Undercarriage Station {CODE}`, where `{CODE}` is the
3-letter `core/constants.FACILITY_CODE` for the plant (`HND` / `SPB` / `CYP`).
The catalog editor shows only the *active* facility's 5 routing dropdowns. At
load, `_project_facility_routing(...)` in `app.py` copies the active facility's
five columns onto the legacy `Final Station` / `AccKIT Station` / `PDI Station`
/ `Wire Station` / `Undercarriage Station` names so the rest of the compute
path stays facility-agnostic. (`Undercarriage Station` is a *second-hop*
reroute: it moves whatever the Final-Station routing dropped at the
Undercarriage station on to another team — see
`core/labor_calculator.compute_unit_labor`.)

**Frozen (pinned) catalog columns — the ~60% gotcha.** The catalog editors
pin SKU / Description (and a couple of status columns) with
`st.column_config(..., pinned=True)` so they stay visible while scrolling
horizontally. Streamlit's frontend **silently disables freezing** when the
summed width of the pinned columns exceeds ~60% of the container width
(confirmed in the bundled DataFrame JS). If pinning ever "stops working,"
that's almost always why. Keep pinned `SKU` at `width="small"` and
`Description` at `width="medium"` — widening them past the threshold turns the
pins off with no error.

---

## Common save failures and what they mean

| Message | Cause | Fix |
|---|---|---|
| "GitHub token not configured" | Token missing or expired in Streamlit Secrets | Rotate the PAT (see above) |
| "❌ Save failed: another user committed concurrently" | Two users edited the same file in the same ~5 seconds | The user clicks 🔄 Refresh in the stale-data banner and re-applies their edits |
| "❌ Save failed: 403" | Token doesn't have `Contents: write` on the repo | Re-generate PAT with the right scope |
| Local file changed but no GitHub commit | A redeploy hasn't happened yet | Wait ~1 min; Streamlit Cloud picks up the commit and rebuilds |

---

## Recovering from a bad commit

If someone pushes a broken CSV or JSON to `main` and the app starts
crashing:

```bash
# Find the bad commit
git log --oneline data/the-file.csv | head -5

# Revert it (creates a new "Revert ..." commit)
git revert <bad-sha>
git push origin main
```

Streamlit Cloud will redeploy within ~1 minute with the prior version.
The revert is itself a new commit — git history shows both the bad commit
and the revert, so the audit trail is preserved.

Avoid `git push --force` on `main` unless you really know what you're
doing — it rewrites history and can lose other people's commits.

---

## Staging branch workflow (testing new features safely)

The repo has two long-lived branches:

- **`main`** — what the team uses every day. Streamlit Cloud's live app
  reads/writes here.
- **`staging`** — your experimentation branch. A second Streamlit Cloud
  app reads/writes here. The two apps share the same repo but never
  touch each other's data, because catalog/scenario/schedule saves are
  branch-aware (`core/catalog_storage.GITHUB_BRANCH` is resolved from
  `st.secrets["github_branch"]`).

### Set this up once

1. Create the branch (already done — `staging` exists on GitHub).
2. share.streamlit.io → **New app** → repo `tritoan156/labor_model`,
   branch `staging`, file `app.py`. Give it a distinct URL like
   `labor-model-staging.streamlit.app`.
3. In the new app's **Settings → Secrets**:
   ```toml
   github_token  = "<your PAT>"
   github_branch = "staging"
   ```
4. Deploy. The staging app now reads `data/*.csv` from the `staging`
   branch and writes saves back to the `staging` branch.

### Experiment-then-promote loop

```bash
# Make sure your local staging is current
git checkout staging
git pull origin staging

# (optionally) bring in the latest main work first
git merge main

# Code, commit, push to staging — only the staging Streamlit Cloud
# app sees these changes. Live users are untouched.
git push origin staging

# Once you're happy with the experiment, promote to main:
git checkout main
git pull
git merge staging
git push origin main
```

### Keep staging in sync with main

If the team commits to `main` (e.g. catalog edits land via the live
app), pull those into staging so the two stay close in step:

```bash
git checkout staging
git pull origin main   # brings main's commits into staging
git push origin staging
```

### Hard rules

- **Never** point the staging app's `github_branch` secret at `"main"`.
  That would route experimental saves into the live data file.
- **Never** push experimental commits directly to `main`. Always commit
  on `staging` first, verify on the staging URL, then merge.
- If you need to throw away an experiment, just reset the staging
  branch back to main: `git checkout staging && git reset --hard
  origin/main && git push --force-with-lease origin staging`.

---

## Adding a new facility

If the company spins up a fourth manufacturing facility:

1. **`core/constants.LOCATIONS`** — add the facility name (e.g. `"Phoenix"`).
2. **`core/constants.FACILITY_CODE`** — add a matching entry mapping the new
   facility to a unique 3-letter code (e.g. `"Phoenix": "PHX"`). This code is
   the column suffix used for per-facility station routing.
3. **`data/machine_clean.csv`** — run a migration to add the 5 routing columns
   for the new code: `Final Station PHX`, `AccKIT Station PHX`,
   `PDI Station PHX`, `Wire Station PHX`, `Undercarriage Station PHX`. Copy an
   existing facility's column values as the starting point.
   `core/data_loader.load_machine_labor` expects *all* per-facility routing
   columns to be present — a missing one breaks the load. After the CSV change,
   **bump `_LOADER_SCHEMA_VERSION`** (see "When you change a CSV column" above).
   See also "Per-facility station routing" for how these columns work.
4. **`data/facility_crew.json`** — add a new top-level key with the new
   facility's per-station HC / Conc / Crew. Easiest is to copy an existing
   facility's block and tweak the numbers.
5. **`data/scenarios.json`** and **`data/uploaded_schedules.json`** —
   automatically supports the new facility once the constant is added;
   the per-location dict structure scales freely.
6. Commit + push. New facility appears in the sidebar dropdown after the
   next redeploy.

---

## Where the admin usage analytics live

Sidebar → very bottom → **⚙️ Admin** expander. Shows:
- Sessions today / this week / all-time
- 14-day sessions line chart
- Sessions by facility
- Save events (last 14 days)
- Top actions (last 14 days)
- Last 20 events (table)

The events come from `data/usage_log.jsonl`, which is **local to the
Streamlit Cloud container** (not committed to the repo as of the
pre-publish hygiene round). Implications:
- Events accumulate within one deploy (~1 min container lifetime
  between redeploys).
- Every redeploy resets the log to the empty seed.
- Anonymous UUIDs only — no PII, no IPs.

If you need persistent cross-redeploy analytics, point
`core/usage_tracker.flush_to_github` at a **private** repo or a private
gist. Currently it's local-only by design.

---

## Dependency pins

`requirements.txt` uses `>=` floor specifiers (not exact pins) so
Streamlit Cloud picks the latest compatible version. This has bitten
us twice with Python 3.14 + new pandas combinations.

If you know a major release is coming and want to verify before the
team's deploy:
1. Create a branch `staging` and push there.
2. Configure a second Streamlit Cloud app pointing at the `staging`
   branch. Test there.
3. Merge to `main` when happy.

If you want stricter pinning, edit `requirements.txt` with version
ranges like `streamlit<2,>=1.30` and re-deploy.

---

## Verifying the calculations (test suite)

A pytest suite pins the labor/capacity math (`core/labor_calculator.py`,
`core/constants.py`, and the `app.py` calc helpers) against regression. It is
**dev-only** — `pytest` is in `requirements-dev.txt`, NOT the Cloud
`requirements.txt`, so it never ships to Streamlit Cloud.

Run it locally after any change to the calculation code:

```bash
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # once
./.venv/Scripts/python.exe -m pytest -q
```

What it covers: per-unit labor (battery gating, ETO, FN_Assy, per-SKU station
routing), `unit_labor_split` summing to the total, schedule expansion, station
demand, cycle time / volume-weighted avg cycle, the full `build_capacity_table`
(labor + throughput caps, `required_hc`, battery branch), status-emoji
thresholds, battery demand, and the `app.py` helpers `_fmt_min_hr` /
`_max_units_at_current_mix`. Several tests are explicit regression guards for
audit fixes (e.g. `get_battery_type` case-normalization, `_fmt_min_hr` NaN/Inf,
`Crew=0`/`Conc=0` capacity edge cases). If you change a formula on purpose,
update the matching expected value in `tests/` in the same commit.

---

## File map (where things live)

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI, sidebar, all tabs |
| `core/data_loader.py` | CSV/JSON readers, fuzzy schedule column matching |
| `core/labor_calculator.py` | Per-unit labor, capacity table, status flags |
| `core/constants.py` | Stations, defaults, utilization thresholds, families, `FACILITY_CODE` (per-facility routing suffixes) |
| `core/catalog_storage.py` | GitHub PUT/GET (shared by all storage modules) |
| `core/scenario_storage.py` | Save/load manual scenarios |
| `core/uploaded_schedule_storage.py` | Save/load uploaded CSVs |
| `core/facility_storage.py` | Per-facility crew config |
| `core/process_flow_storage.py` | Manufacturing flow graph |
| `core/usage_tracker.py` | Anonymous telemetry (local-only) |
| `core/data_validator.py` | Catalog quality checks |
| `data/*.csv`, `data/*.json` | All persisted data |
| `.streamlit/config.toml` | Page config, max upload size |
| `.streamlit/secrets.toml` | **Not in repo** — holds `github_token` on Streamlit Cloud |
| `requirements.txt` | Python deps |
