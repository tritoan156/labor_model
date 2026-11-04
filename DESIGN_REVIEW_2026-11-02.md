# Labor Capacity Tool — design review and knowledge transfer

**Date:** 2026-11-02 · **For:** Codex (and whoever improves the tool next) · **Status:** explanation only, nothing changed
**Reviewed commit:** `6ac920c` (origin/main, 2026-11-02; 920 tests collected)

---

## 0. Read this first: provenance, and a problem with Codex's baseline

### 0.1 What I could and could not recover

The tool's first version was built on 2026-05-19 to 05-21 (initial commit `e86cceb`, then ~70 commits in three days). **Those conversations are not on this machine.** The Claude Code sessions whose transcripts still exist start on 2026-06-15; the May 26 to 29 sessions are listed but their transcripts are gone. So the original scoping discussion (who the users are, the budget, why Streamlit and GitHub) is **not recoverable as your words**. What I can trace comes from:

- your own messages in the surviving sessions (2026-06-15, 08-04, 10-20 to 10-23, 11-02);
- commit messages and PR descriptions written at the time, many of which record "per request", "per the planner's definition", "per decision";
- the README, HANDOFF.md, MAINTAINING.md, and the three MES design docs in the main checkout's untracked `docs/` folder;
- my memory notes from earlier sessions.

Every claim below carries a tag:

| Tag | Meaning |
|---|---|
| **[T]** | Traced to your words or to a commit/PR that records your decision |
| **[D]** | Rationale documented at the time by whoever wrote the commit, doc, or help text |
| **[I]** | My inference from the code and its history |
| **[N]** | Not recoverable; I say so instead of guessing |

### 0.2 Codex reviewed a stale checkout

The local main checkout at `C:/Users/ToanNguyen/.claude/Project/labor_model` is on `4f2e118` (2026-07-25), **120 commits behind origin/main**. That matches every number in Codex's report:

| Codex observation | On `4f2e118` (July) | On `6ac920c` (today) |
|---|---|---|
| 169 tests passed | 169 (the exact count after PR #5, 2026-07-17) | 920 collected |
| Suggest uses a "five-day weekly labor-capacity reference" | True | Removed 2026-10-23 (`5041ff1`); each week is priced by its own Mon–Fri count |
| Overview opens on the bundled May schedule (193 units) | True for every facility | Since 2026-10-23 (PR #19) a facility opens on its **newest saved schedule**; Henderson opens on "Hen November 11/02" (368 units). The bundled sample only loads for a facility with no saves |
| All-zero headcount still gets dates and no overloads | True | **Still true** (reproduced on today's code, see §3) |

So before Codex changes anything: `git pull` that checkout (or work in a fresh worktree from origin/main). Two of its three observations are already fixed, one is not. Everything below is written against today's `6ac920c`.

---

## 1. What problem we agreed to solve (Q1)

**Purpose.** Monthly, facility-level capacity planning: *given this month's build list, do we have enough people and cells, where is the bottleneck, and is the fix headcount or fixtures?* That is the README's own framing from day one [D, `26ad05f`], and every later feature answers a variant of it (buildable units, Required HC, What-if, Balance people).

- It was **not** built as a weekly scheduler. The week dimension, Calendar, LPT auto-spread and the Sequencer were added on a staging branch on 2026-05-22 to 05-27 as "Step 1 of the actuals/time-dimension work" [D, `836c20a`]. They level-load dates so the weekly heatmap has a baseline; they never became a constraint scheduler (see §3).
- It was **not** built to say *which specific units* can be completed. The buildable-units math scales a mix proportionally (whole-mix cap) or per production line, so it answers "how many at this mix", never "which rows". Balance people (PR #28) scales lines by share for the same reason [D].
- Rough capacity planning at monthly resolution is the honest description. The IE review on 2026-05-29 called the methodology sound and corrected only wording [D, `c0abd4e`].

**Users and decisions.** README: "planners and execs" [D]. Planners load the schedule, fix catalog gaps, set headcount and cells, run what-ifs. Executives read the Overview and the Executive Summary (a rebuild of the legacy monthly sheet: Units, Hours Earned, New Hire %, Absenteeism, Headcount Needed, Missed Units, Good/No Good) [D, `1357b20`]. Your November questions show the actual use: "if we have 73 people, what is our recommend capacity", "what about 74 people with extra cells at 18 days", "what is the efficiency to 312 units a month", "what should be the manhours/unit?" [T, 2026-11-02].

**Constraints you gave.** Budget and hosting were never stated in a surviving conversation [N]. What the record shows:
- Hosting: Streamlit Cloud, a public GitHub repo, one PAT in secrets, no database [D, `7478fa4`, README]. Pinning requirements broke the Cloud install once and was reverted to floors [D, `6823681`].
- Data entry: planners keep the build list in Excel; you asked for paste-from-Excel, workbook upload as-is, and "for excel, i don't need to connect with the schedule. I just want to connect the labor hours data, cycle time" plus "the temporary save where I can change it at that time in the tool, and can update later in the excel" [T, 2026-10-21]. That became PR #16: the IE workbook is the source of truth for minutes, with tracked in-app overrides.
- Speed: features shipped same-day; most "design" was done inside commit messages.
- Maintenance: single maintainer (you), with HANDOFF.md written on 2026-05-28 for "whoever picks this up fresh" [D].

**Deliberately out of scope** (all documented at the time):
- drag-and-drop sequencing board ("v3 polish") [D, `234f047`];
- holidays ("no holiday awareness", the planner re-dates by hand) [D, `_working_days_in_month`];
- actuals and floor data (that is the MES's job, §7) [D, `docs/MES_DESIGN.md`];
- authentication, per-user access, private repo [D, MAINTAINING];
- customer data in the repo (scrubbed 2026-05-21) [D, `a07652c`];
- the Lvl 0 consolidated SKUs: "Labor model still using a unit as BASE FG + kit ... until fully develop then we can make a switch" [T, 2026-11-02].

---

## 2. What the inputs and outputs mean (Q2)

### 2.1 Definitions as implemented

| Term | Definition in the model | Tag |
|---|---|---|
| **Headcount (People, `HC`)** | People assigned to a station for the whole shift. Production HC excludes Lead/Other support roles, which count toward total headcount only | [D] `56f6d40` |
| **Stations/Cells (`Conc`)** | Parallel bays at a station. Used only in the throughput check | [D] help text |
| **Crew per unit** | People working one unit at once. Converts person-minutes to elapsed minutes: `cycle = labor ÷ crew`. Used only for throughput and the Build Time cycle columns, never for labor capacity | [D] `cycle_for_unit` |
| **Efficiency** | Share of the paid shift that is productive. Default 0.50 "per request" [T, `59535d6`]; IE review reworded it as "folds in breaks/setup/rework/balance loss (OEE availability × performance), typical 0.80–0.90, 0.50 a deliberately conservative shop default" [D, `c0abd4e`] |
| **Safety factor** | Planning buffer multiplier on capacity. Default 1.00 "per request" [T, `59535d6`] |
| **Buildable units** | Three different numbers, §2.5 | |
| **Lead time (Build Time tab)** | `total labor ÷ (shift × efficiency)` in days, one person serial | [D] §2.4 |

Core formulas (`core/labor_calculator.py:504-645`):

```
labor_factor    = (1 − new_hire% × (1 − new_hire_productivity)) × (1 − absenteeism)
effective_shift = shift_minutes × (1 + overtime)
labor_cap_safe  = HC × effective_shift × working_days × efficiency × safety × labor_factor
required_hc     = ceil(labor_demand ÷ (effective_shift × days × efficiency × safety × labor_factor))
thru_cap_safe   = Conc × (effective_shift ÷ avg_cycle) × efficiency × safety       # no labor_factor
labor_util      = labor_demand ÷ labor_cap_safe
thru_util       = (units ÷ days) ÷ thru_cap_safe
```

Status: >100 % 🔴, >90 % 🟠, >75 % 🟡. A station with demand but Conc = 0 or an invalid cycle is forced 🔴.

### 2.2 Are catalog times person-minutes?

**Person-minutes per unit, by design and by test** [D]: the calculator docstring, the help ("1 person working 1 min = 1 p-min, independent of how many people you assign"), and the catalog editor caption ("person-minutes per unit, not hours") all say so. They are **estimates maintained by the industrial engineers in Excel** [T, PR #16]; the catalog carries a `Last Modified` stamp and git history, nothing marks a value as measured versus estimated except the `XXX` placeholder convention.

Whether the IE's standard already includes personal/fatigue/delay allowances is **not recorded** [N]. That matters for §2.6.

### 2.3 When is dividing labor by crew valid?

The model divides by crew only to get a cycle for the throughput check. It is a fair assumption for labor-paced stations where people work the same unit in parallel. The IE review already qualified the help text: "adding people doesn't help" is only true for machine- or process-paced steps like Battery; "labor-paced stations gain throughput from more crew" [D, `c0abd4e`].

Operations that do **not** speed up proportionally, and how the model handles each:
- **Battery assembly** — fixture-paced; the model ignores crew and uses a fixed 170 calendar minutes per battery per cell (§2.4). Handled.
- **HS (PM) units at Final** — the model silently uses crew = 1 regardless of the sidebar's Final crew (`HS_FINAL_CREW`, present since the initial commit). Undocumented in help, README, HANDOFF, MAINTAINING [I]. Origin: likely the original class table "HS = 1 person × 30 min" [I].
- Anything with a cure, test, charge or QC hold time, or serial sub-steps (mount, then marry) — **not modelled**. The model has no notion of non-labor dwell. The 2026-07-27 MES heijunka doc states the simplification plainly: "one cell = one person. A cycle time is person-minutes per unit" [D].
- The model never checks that `HC ≥ Conc × Crew`; the What-if tab warns about it, the Capacity table does not [I].

### 2.4 Why battery throughput is separate

Decided on day one: "Battery applies only to BOSS" and "Set Bat=0 for all PDS/SDG rows (non-BOSS units have no batteries)" [D, `71ca87f`, `88f800f`]. The rationale in the help: "170 min/cell is fixed regardless of headcount, so the right knob is Stations/Cells" [D, `b35e0a5`]. Implementation: the Battery station runs the normal labor check on `320 × batteries + 10` person-minutes, but its throughput check counts **batteries per day** against `Conc × shift ÷ 170`.

Two things to know:
- With the default crew of 2, `320 ÷ 2 + 10 = 170`, so the labor cycle and the fixed cycle agree. Change Battery's crew and only the labor side moves [I].
- On 2026-07-16 you accepted "Battery flags as the bottleneck when it isn't really binding" and added the per-facility **Exclude stations from buildable units** control as "a temporary set-aside until the underlying model is fixed" [T, `eac227d`]. That fix never happened; the exclusion is still how Battery is handled when it over-constrains.

### 2.5 Why "Lead Time" is one person

`lead_days = total_labor ÷ (shift × efficiency)`, ignoring crew, cells, safety and workforce factors. The caption has said since 2026-05-21 that it "assumes a single person building the whole unit start-to-finish ... Use lead time to compare units to each other, not to predict ship dates" [D, `6ec9dc1`], and the IE review reworded it as "single-operator labor content, not delivery lead time" [D]. The reason, inferred [I]: without a precedence model (the Process Flow graph is draw-only, §5) there is no defensible elapsed time, so the tab offers a comparable yardstick instead. Whether you asked for it this way or accepted it is **not recoverable** [N].

### 2.6 Why three capacity numbers differ

| Number | Where | What it assumes | Looser or tighter |
|---|---|---|---|
| **Aggregate staffing coverage** | Executive Summary verdict | All earned hours are poolable across stations, no cells; reproduces the legacy spreadsheet exactly [D, `1357b20`] | Loosest |
| **Buildable by production line** | Overview "Buildable units", Exec "station-aware" | Each line (Compressor / Generator / EBOSS / PM) scales to its **own** worst station; lines add up [D, `3690f1b`: "the plant runs separate parallel production lines whose outputs ADD up"] | Middle |
| **Whole-mix cap** | Overview bottleneck line, Capacity "Max units", What-if | The entire mix scales as one block until the single worst station hits 100 %; one unstaffed station zeros everything [D, `5ac4b28`] | Tightest |

They were reconciled once already (PR #3 reworded the banner so "Max units: 0" and "Buildable 52" stop looking contradictory). How to read them: the exec verdict says whether the **payroll** covers the hours; by-line says what the plant can **actually deliver** with dedicated crews; whole-mix says what the mix as scheduled can reach before the first station saturates.

Two defects found while reading [I]:
1. `_max_units_at_current_mix` does not apply the "Conc = 0 with demand is blocked" rule that the capacity table and `buildable_by_line` apply, so the whole-mix line can name a different bottleneck than the 🔴 flag.
2. `_headline_max_units` returns the **potential** cap (as if unstaffed stations were staffed) for single-line plans and for an infeasible multi-line plan, so Capacity "Max units" and the What-if can show a positive number while the Overview shows 0. The Capacity tab explains this in a caption; the What-if comparison does not.

### 2.7 Do the adjustments overlap?

By construction, no factor is applied twice inside one formula, and the capacity table and the Executive Summary use the same product (pinned by a test). What each one means [D, docstrings and sidebar captions]:

| Factor | Labor capacity | Throughput (cells) | Per-unit man-hours | Lead time |
|---|---|---|---|---|
| Efficiency | yes | yes | yes | yes |
| Safety | yes | yes | no ("a buffer, not a cost") | no |
| New-hire ramp | yes | no | yes | no |
| Absenteeism | yes | no | yes | no |
| Overtime | yes (longer shift) | yes | no | no |

Where they **can** overlap in practice [I]:
- If the IE standard already carries a PFD allowance and efficiency is 0.50, allowances are counted twice. Nobody has recorded what the IE minutes include [N].
- Efficiency and safety also cut **cell** capacity, so the 0.50 default halves every physical cell ceiling. Documented in help, but it is a modelling choice worth confirming.
- Absenteeism (default 5 %) and a conservative efficiency both stand in for "people not producing"; they are separate sliders on purpose, so the planner must not fold absence into efficiency as well.

---

## 3. Planning period versus scheduled dates (Q3)

### 3.1 How the two layers work

- **Plan month** (sidebar) is the authoritative month: it sets the working-day count, the Calendar's week split, and where undated rows are auto-spread. It is **never switched silently**: "Nothing switches on its own — a May export planned for June is a real case" [D, README; `294bb2a`]. You confirmed the related default on 2026-10-23: "start a fresh session from the facility's newest saved schedule instead of the bundled May 2026 sample" with "Do not change the Plan month default" [T].
- **Carryover** is your definition: "a unit is carryover when its PRODUCTION MONTH is earlier than the chosen Plan month (it slipped and must be built this month)" [T, `14a639d`, "per the planner's definition"]. On 2026-11-02 you added the status rule: count everything not Complete/Transferred/Shipped, On Hold included, and open rows from earlier months keep their month so they are flagged carryover and counted [T].
- **Overview totals** count every row in the loaded file. **Calendar** shows only rows whose PRODUCTION DAY falls in the plan month ("the picker is authoritative ... out-of-month rows get a top-of-page warning and are intentionally not shown here" [D]).
- The **plan guide** step 2 explains the gap and offers two buttons: "📆 Plan May 2026 instead" and "📅 Move 193 row(s) into November 2026" [D, PR #19, #21].

### 3.2 Codex's three observations

| Observation | Verdict | Detail |
|---|---|---|
| November selected; Overview shows 193 units from the May sample, Calendar shows none | **Intentional**, and partly stale | Totals count the file, the Calendar shows the selected month, the guide says so and offers the fix. On today's main a facility with saved schedules never opens on the sample; Codex saw it because its checkout predates PR #19 or because the facility had no saves. The Calendar's own empty-state message does not say "193 rows are dated in May"; only the guide does. |
| Suggest uses a five-day weekly reference and skips a station when capacity is zero | Five-day: **fixed 2026-10-23**. Zero-skip: **confirmed on today's code** | The skip is at `app.py:1333-1337`: `if cap <= 0: continue  # Station doesn't exist here — skip the constraint check (treat it like infinite capacity)`. |
| All headcount zero: still assigns dates, returns no overloads | **Defect**, reproduced on `6ac920c` | With every station at 0, every check is skipped, the first candidate day wins, `overloads` stays empty, and the diagnosis panel has nothing to show. Reachable from the UI because People allows 0. |

### 3.3 Why the zero-capacity skip exists

This is a semantics drift, not carelessness [I, strong evidence]. On 2026-05-19 the second commit after the initial one was "Allow HC=0 for stations not present at a facility" (`bfe1fa1`): HC = 0 originally meant *this plant has no such team*, which is why Cypress with no Wire team was fine. The Sequencer, written on 2026-05-22, inherited that meaning and documented it. Meanwhile the capacity side moved on: from 2026-05-27 (`cf4d78d`) an unstaffed station with demand is **infeasible** (🔴 NO STATION, buildable 0), and per-facility **routing** became the way to say "no such team here" (Wire → GenAcc at Cypress, Undercarriage → ComAcc at Henderson). The Sequencer was never updated to the new meaning.

### 3.4 What the Sequencer was meant to enforce

Its own spec [D, `234f047`]: "Constraint-aware LPT for level-loading the month ... won't place a unit on a day if its labor would tip any station's weekly utilization above 100 %." Labor only, weekly granularity, no bays, no crew, no precedence, no material. The overload diagnosis (`a69060a`) offers hire / extend / defer numbers from the same labor math.

Today it also diverges from the capacity table in four ways [I]: it ignores overtime, the new-hire/absence factor, Material Handling minutes and folding, and the throughput (cell) check. So a proposal that passes Suggest can still paint a week red on the heatmap.

**Recommended intended behaviour** (my recommendation, for you to confirm):
1. Suggest should price each station with **exactly** the capacity table's formula (one shared helper), including throughput, so "fits" means the same thing on every tab.
2. HC = 0 with demand is an overload, not infinite capacity. "Station not present" is already expressed by routing; the skip can go.
3. Keep it as a **level-loader**, not a scheduler: labor, cells, actual Mon–Fri days, overtime and absences, yes; operation precedence and material readiness, no. Those need per-unit status and kit data the tool does not have and the MES design assigns elsewhere (§7).
4. Working days: Suggest spreads across the whole plan month while the diagnosis uses the sidebar's day count (which drops to 5 under "This week"). Pick one, probably the month.

---

## 4. Incomplete data (Q4)

### 4.1 What the model does and why

| Situation | Behaviour | Why | Tag |
|---|---|---|---|
| FG SKU not in machine catalog | Unit **dropped**, SKU listed in `skipped_fg` | No labor can be computed at all; dropping is safer than a silent zero | [D] `6ec9dc1` (rationale inferred [I]) |
| Accessory SKU not in accessory catalog | Unit **kept with zero accessory labor**, listed in `unknown_acc` | The base unit is still computable; the commit itself notes this "may not be the planner's intent" | [D] |
| Row has BUILD QTY but no FG SKU | Mapped by MODEL TYPE to the family's `XXX` placeholder, paired with `AXXX`; counted as an **estimate** with a warning | You: "I thought the model can read that they are missing SKU and can ask to add placeholder or at least give a warning" | [T] 2026-08-04 |
| Placeholder labor values | **Median of the family's real SKUs, "nothing invented"**; a real SKU missing from the catalog (SDG125-A024) got a description saying it is provisional | | [D] PR #7, #8 |
| Rows tagged for another plant in this plant's file | Counted here: "For now, if they are tagged spartanburg in the henderson sheet, it will be for henderson build" (checkbox, default on, always warned) | | [T] 2026-08-04 |
| Blank LOCATION / blank or junk PRODUCTION MONTH | Folded into this facility / the plan month, with a warning: "per planner's choice — count all in plan month + warn" | | [T] PR #1, #4 |
| Renamed SKU collides with an existing one | Offer a typo-merge, keep the existing row's values: "use the one that already in the list. It is a typo" | | [T] 2026-08-04 |
| Section-subtotal rows, "Trailers" model type, rows with neither SKU nor model | Deliberately **not** covered; reported as dropped | Covering them would double count or invent data | [D] `6eaf606` |
| Blank catalog cell, missing column, Bat missing | Read as 0 | Loader convenience | [I] |
| "➕ Import N missing SKUs" | Adds **zero-labor** placeholders so units stop being dropped; the guide nags until labor is set | | [D] `6a3290a`, PR #21 |

Estimates and defaults you explicitly approved [T]: the SDG placeholders and their accessory counterparts; the Spartanburg-in-Henderson rule; count-and-warn for undated rows; efficiency 0.50 / safety 1.00 defaults; battery 320/10 fallback "per decision" (`9715f5b`); FN_Assy "used literally, including 0" (`159b1f3`, "per request"); EBOSS125-CAT at 480 ETO minutes; Material Handling 15/5/15/15 minutes per unit "find only source or something to validate my number" (the PR cites none, so treat them as your estimate).

### 4.2 "No work required" versus "time not known"

The model **cannot** tell them apart. A 0 in `FN_Assy` means "no Final labor for that SKU" by your request, while a blank cell, an imported placeholder, or a missing accessory also produce 0. Only the `XXX` naming convention and the Data Quality warnings carry the "estimate" meaning.

You have already stated the rule you want, in the SKU consolidation work on 2026-11-02: "If there are missing data, mark it missing so we can go back in and fill in later" and "Confirm it but mark if you suspect it", which became MISSING / SUSPECT / REJECTED / ASSUMED statuses in that workbook [T]. The labor model has no equivalent status. That is the gap to close, not the zero-fill itself.

### 4.3 Exploratory versus approved

There is no plan state in the tool: no "approved" flag, no sign-off, no freeze. Named scenarios and executive snapshots are the closest thing (§6). Allowing exploratory calculations on incomplete data while blocking an "approved" plan is a reasonable design, and consistent with how you use it, but it was **never discussed** [N]. See question 3 in §12.

### 4.4 Who owns verifying labor standards

- Standards live in the industrial engineers' Excel workbook; the app imports it as-is and locks the catalog to it, with in-app overrides tracked for reconciliation (`excel_source_of_truth`) [T, PR #16].
- You own "Work Center Routings" for Business Central and have been the one entering catalog corrections (dozens of "Update machine catalog via app" commits) [T, memory].
- Evidence that counts today: a value in the IE workbook, or a catalog edit with a `Last Modified` stamp and a git commit. Nothing records a time study or an observation. My recommendation: add a per-cell or per-SKU **source/status** column (measured / estimated / placeholder, plus who and when) rather than another warning.

---

## 5. Routing (Q5)

### 5.1 Why per-facility routes are catalog columns

Incremental growth, each step forced by a real plant difference [D]:
1. 2026-05-27 `bbedebf`: Cypress Final Assembly read 535 % because PDS compressors are built by the Compressor team → `Final Station` column, PDS → ComAcc.
2. Same day `3a2de49`: AccKIT and PDI reroutes (PDS/SDG kits done by the Com/Gen accessory teams).
3. 2026-05-28 `a0a141b`: three shared columns became nine, because "PDI is done by the PDI team in Henderson but by the Accessories team in Cypress". Done as columns so "the compute path stays facility-agnostic without any signature changes"; `_project_facility_routing` copies the active plant's columns onto the legacy names at load.
4. 2026-06-12: Wire columns (Cypress has no Wire team). 2026-06-12 / 07-16: Undercarriage station and its reroute.
5. 2026-10-23 PR #18: new SKUs seed their 15 routing cells from the most similar catalog row, because every add path had been writing them blank.

A normalized (SKU, facility, station) table was **never discussed** [N]. The columns were cheap (the catalog is already an editable grid on GitHub) and have cost: an editor-persist bug, a blank-seed bug, a stale "9 columns" comment, and a CSV migration plus schema bump to add a plant.

### 5.2 "Reroute through a second step"

That is the Undercarriage column. Hop 1: a PDS SKU's `Final Station` sends its FN_Assy minutes to Undercarriage. Hop 2: at a plant with no undercarriage crew, `Undercarriage Station` moves whatever landed there on to another team (Henderson → Com Accessories). Default is a no-op; totals never change [D, `b64fead`, PR #5].

### 5.3 The Process Flow graph

**Draw-only.** Its storage module says "The model uses these edges to draw the Process Flow diagram"; nothing in the calculator imports it; capacity treats every station independently [I, confirmed]. You edited the default edges on 2026-05-20 and they were adopted as the shipped default (`3ca56ec`, `21b72c7`) [T]. The tab's text ("use it to correct the manufacturing precedence for your facility") over-promises: the graph is global and changing it changes no number [I].

### 5.4 Product-family exceptions and whether they are real

| Exception | Manufacturing basis | Tag |
|---|---|---|
| Battery only on BOSS (EBOSS and PM); PDS/SDG forced to 0 | Real: diesel units have no batteries | [D] `88f800f` |
| PDS Final labor → Undercarriage → ComAcc at Henderson | Real: Henderson has no undercarriage crew | [D] PR #5 |
| SDG wire → GenAcc, PDS wire → ComAcc at Cypress | Real: Cypress has no Wire team ("per the spec") | [D] `845e7b6` |
| ETO 960 min for BOSS220/400 (now a catalog column); EBOSS125-CAT 480 min | Real: ETO team builds them | [T] `9715f5b`, PR #25 |
| HS units at Final use crew 1 | Unknown; from the original class table | [N] |
| Material Handling takes over Warehouse (Pick) and Ship | Your instruction for Henderson | [T] 2026-11-02 |
| 63 BOSS SKUs have Wire labor but no Cypress wire route | Open since 2026-06-12, "harmless unless Cypress builds BOSS" | [D] |

### 5.5 Documented process versus implemented model

1. **Final Assembly classes are dead code.** `FINAL_LABOR = {"HS": 30, "HT": 60, "STD": 156}` (mount 60 + marry 96) is still imported and still described in the help and README, but Final has read `FN_Assy` literally since 2026-05-27 [D]. The mount/marry structure exists only in the MES heijunka design doc.
2. **Crew size.** Help says crew "drives processing time"; in fact it affects only the throughput check, and HS units ignore it at Final [I].
3. **Concurrent bays.** Throughput is `Conc × shift ÷ mean cycle`; nothing checks that the plant has `Conc × Crew` people to run every bay at once (the What-if warns, the Capacity tab does not) [I].
4. **Your 2026-11-02 BOM description** (final routing mounts sub-assemblies that have their own routings; wire harness 20 min per component; Pre-QC 2/3 then PDI then Final QC 1/3, both at the PDI work center) is more precise than the labor model's flat per-station minutes. You explicitly kept the labor model on "Base FG + kit" until the Lvl 0 SKUs are developed [T]. So the mismatch is known and deferred, not accidental.
5. Two classifiers disagree: the seeder uses the description ("HYBRID", "PM SKID"), the calculator tests "HS"/"HT" in the raw SKU [I].

---

## 6. Storage and saved scenarios (Q6)

### 6.1 Why GitHub-backed files and Streamlit

Not stated in any surviving conversation [N]. The documented reasons [D]: Streamlit Cloud deploys from the repo for free, the data files are plain CSV/JSON that Excel can read over a raw URL (your request for the Excel link), git history is the audit trail and the backup ("Recovering from a bad commit" in MAINTAINING), and the PAT in secrets is the only credential. HANDOFF calls the stack a starting point only implicitly (the "Open follow-ups" list); nothing says it was meant to be permanent. My reading: **a practical starting point that has held up**, with three known costs: the >1 MB wipe incident (2026-06-15, fixed), the ~1 minute redeploy after every save (mitigated by local mirroring, PR #21), and the stale-module shim that has broken production twice.

### 6.2 What "Save scenario" preserves

- **Manual scenario** (`scenarios.json`): exactly `FG SKU, Accessory SKU, Qty` per row and a timestamp. No month, crew, settings or results [D, `5f3c9cd`: "named build-plan snapshots"].
- **Uploaded schedule** (`uploaded_schedules.json`): the raw CSV text, "so any column the original file had survives the round-trip" [D].
- **Executive scenario** (`exec_scenarios.json`): the assumptions **and** the computed metrics, "so executives see exactly the numbers as of the save, even if the catalog / crew change later" [D, `1357b20`]; from 2026-06-03 also a per-station HC snapshot [D].
- **Crew and planning assumptions**: per facility, in their own files; the sidebar sliders are seeded from them.

### 6.3 Reopen: reproduce or recalculate?

Both, deliberately asymmetric [D]:
- Manual scenarios and uploaded schedules always **recalculate** against the current catalogs, crew and sliders.
- The Executive Summary comparison shows the **frozen** metrics ("Each column is frozen as of its save"). "📥 Re-apply" restores only four sliders (new-hire %, new-hire productivity, absenteeism, overtime); shift, days, headcount, schedule and catalog stay whatever is loaded, so a re-applied scenario rarely reproduces its saved numbers [I].
- The exec schema has grown without a version (old entries lack `overtime`, `support_hc`, `crew_hc`); the code falls back to live values [I].

Whether a reopened plan *should* reproduce its result was never decided [N]. Recommendation: a saved plan should carry a full snapshot (schedule, crew, settings, catalog commit SHA) and show both "as saved" and "recomputed today" side by side. See question 4.

### 6.4 Access, concurrency, backup, deployment

- **Access control:** none. No login, one shared PAT, public repo, ungated Admin expander, destructive actions behind a double-click confirm [D, MAINTAINING, `3938ff0`]. MAINTAINING: "If you ever push real customer schedules, flip it private first."
- **Concurrent editing:** SHA-locked optimistic writes with one retry; cell-level merge for the catalogs, per-key merge for scenarios, whole-file replace for the process flow; last writer wins on the same cell [D, `b668331`]. A "🔄 Refresh" banner appears when someone else commits.
- **Backup:** git history only; the Export workbook is the only non-git copy [D].
- **Deployment:** Streamlit Cloud from `main`, redeploy ~1 minute after each commit; requirements use floors because exact pins broke the install [D].

### 6.5 Staging versus main: which is current?

- MAINTAINING.md (written 2026-05-22 with the staging branch, `836c20a`): never push experiments straight to main; verify on the staging app, then merge.
- HANDOFF.md (2026-05-28): "Commit straight to `main` — staging exists but is intentionally unused right now."

The practice since July is a third thing: every change goes through a `claude/*` branch and a GitHub PR into `main` (PR #1 on 2026-07-09 through PR #29 today), and the staging branch's last commit is 2026-05-27. **HANDOFF reflects the agreement, PRs are the mechanism, and MAINTAINING's staging section is stale.** The 2026-10-20 audit listed "drop or revive the staging workflow" as a product decision you still owe [memory]. The `github_branch` secret mechanism still works if you ever want a staging app.

---

## 7. What stays outside this tool (Q7)

No Business Central, MES or OData integration exists in code or docs of this repo [I, confirmed]. The boundaries were written down in the MES design docs in the main checkout's `docs/` folder (untracked; 2026-06-15 and 2026-07-27) [D]:

| Concern | Owner per the MES docs | Status |
|---|---|---|
| Demand (the production schedule, SKUs, quantities, months) | Business Central is the system of record; the planner exports it, the tool reads the export | Manual upload today; a BC connector is "net-new build, does not exist yet" |
| The plan (dates, leveling, `PRODUCTION DAY`) | **labor_model**: "owns the plan ... Leveling is done; do not rebuild it" | In place |
| Approved schedule | Not assigned anywhere | Open, see §12 |
| Production status, actuals, attendance, serials | **MES** (the PowerApp "Daily Production Entry" work): "owns the floor"; actuals flow back to labor_model only as suggested updates | Pre-build |
| Actual labor and efficiency numbers | Derived by the MES from events; labor_model keeps planning numbers | Pre-build |
| Labor standards | IE Excel workbook today; Business Central routings in future (the SKU consolidation database, PR #29, is the feed) | In progress |

What was approved versus exploratory:
- **Approved [T]:** Excel link for labor hours and cycle time only, not the schedule (2026-10-21); the SKU consolidation routing database for BC (2026-11-02); the labor model stays on Base FG + kit until Lvl 0 SKUs are developed.
- **Exploratory only:** pulling the schedule straight from Business Central over OData (suggested by Claude in the 2026-10-22 session, never acted on); the labor_model → Dataverse export and the BC connector in the MES docs (both flagged there as unbuilt); the drag-and-drop board.

---

## 8. Validation and what to preserve (Q8)

### 8.1 What was checked against what

| Check | Evidence | Tag |
|---|---|---|
| Executive Summary reproduces the legacy monthly sheet "exactly" (Cypress 27.46 HC, 1494.5 earned hrs, 36.7 missed; Henderson 53.49/43.0; Spartanburg 36.62/6.3) | `1357b20`, 2026-06-02 | [D] |
| IE review, two independent reviewers: "no formula bugs, the methodology is sound", wording corrected | `c0abd4e`, 2026-05-29 | [D] |
| Adversarial audit against real data, six wrong-number bugs fixed | PR #3, 2026-07-13 | [D] |
| Unit coverage of the three August 2026 schedules: 885 of 888 real units, each gap explained | PR #7, #8, 2026-08-04 | [D] |
| Your manual count for Henderson Oct/Nov 2026 (366 units/month by line) reconciled with the app's 368 | 2026-11-02 session, PR #25 | [T] |
| Tab walks with AppTest, cross-tab totals reconciled (Overview = Calendar = Exec × 60) | PR #20, 2026-10-23 | [D] |
| Test suite | 920 tests on `6ac920c`; one known pandas-3-only local failure | [D] |

Not validated by anyone [N]: the 170 min battery cycle, the 0.50 efficiency, the 320/10 battery defaults, the Material Handling minutes, lead time as a predictor, and whether the by-line buildable number matches delivered output in any month. **No month's plan has been compared to what the floor actually built.** That is the MES's purpose, and it is not built.

### 8.2 Known limitations and deferred fixes

- Battery over-constrains; the exclusion control is the stop-gap (2026-07-16).
- No holidays; Nov 26–27 2026 count as working days.
- No `HC ≥ Conc × Crew` check on the Capacity tab.
- HS Final crew override undocumented.
- Sequencer semantics drift (§3).
- Whole-mix cap ignores the Conc = 0 block; headline "Max units" can show potential instead of 0 (§2.6).
- 63 BOSS SKUs without a Cypress wire route.
- Executive scenario schema unversioned; Re-apply is partial.
- `FINAL_LABOR`, the "9 columns" comment, and the mount/marry help text are stale.
- Product decisions still open from the 2026-10-20 audit: drop the Customer-suffix view, merge Schedule into Sequence, merge manual scenarios into saved schedules, keep or drop telemetry/Admin, prune the 13 saved May–Aug schedules and the 188-row test scenario, staging.
- pandas pinned `<3` until an Arrow-string compatibility pass.
- The stale-module shim must be maintained by hand for every new core name.

### 8.3 Workflows you asked for that a redesign must keep

All [T]:
1. Upload the planner's workbook **as it is** (any header naming, header below a title row, the Combined sheet of a multi-tab file, Excel serial months).
2. Paste a SKU list straight from Excel into manual mode.
3. IE Excel as the source of truth for minutes, with a temporary in-app override reconciled later.
4. Missing SKUs: warn, offer a placeholder, count the units; typo-merge on rename.
5. Rows tagged for another plant in this plant's file count here (warned).
6. Carryover = older production month, counted in the plan month; statuses other than Complete/Transferred/Shipped count.
7. The month never switches silently; "Plan <month> instead" is one click.
8. The screen guides the user ("The screen should guild the user what need to be done ... fully close the loop", 2026-10-23); every save is in use at once; every edge case has a way back.
9. Fresh session opens on the facility's newest saved schedule.
10. Material Handling as per-unit support minutes, able to absorb Warehouse (Pick) and Ship.
11. The What-if simulator and ⚖️ Balance people with priority lines (Hybrid, Power Module, ETO first).
12. Executive Summary as one table with shaded section dividers ("per the planner's preference").
13. Sidebar handlers never call `st.rerun()` directly (a hard rule after the 20× working-day bug).

### 8.4 Codex observations to correct or qualify

1. Its baseline is `4f2e118` (2026-07-25). Pull first.
2. "Five-day weekly reference" is gone since 2026-10-23.
3. "193 units from the bundled May schedule" only happens for a facility with no saved schedules on today's main.
4. "169 tests passed" is July's count; today 920 collect.
5. The zero-headcount Sequencer result is a genuine defect on today's main and is the right thing to fix first in that area.
6. "The calculation excludes unknown finished goods and includes unknown accessories with zero labor" is accurate, and the asymmetry is explained in §4.1.

---

## 9. Decision table

| Decision | Original reason | Evidence / source | Still valid? | Recommended next step |
|---|---|---|---|---|
| Monthly facility capacity model, not a scheduler | Answer "enough people and cells for this month's plan?" | README `26ad05f` [D] | Yes | Keep. Make the Sequencer's scope explicit in its UI (level-loader, labor + cells) |
| Catalog minutes are person-minutes from the IE workbook | IE owns standards; Excel is the source of truth | PR #16 [T] | Yes | Add a source/status column (measured / estimated / placeholder) |
| Crew divides labor only for the throughput cycle | Labor capacity is people × time; cells are the physical limit | `cycle_for_unit`, help [D] | Yes for labor-paced stations | Add the `HC ≥ Conc × Crew` warning to the Capacity tab; document the HS crew-1 override or remove it |
| Battery: BOSS only, fixed 170 min/cell, batteries/day | Fixture-paced; hiring does not help | `71ca87f`, `b35e0a5` [D] | Partly; it over-constrains in practice | Confirm the cycle with the floor; replace the "exclude Battery" stop-gap with a corrected cycle or a per-facility value |
| Lead time = one person serial | No precedence model; a comparable yardstick | `6ec9dc1`, IE review [D] | Yes as a yardstick | Rename the column ("Labor days, 1 person") or compute a crew-based elapsed estimate from the longest path once precedence exists |
| Three capacity numbers (aggregate, by line, whole mix) | Legacy sheet, parallel lines add up, single bottleneck for the exec | `1357b20`, `3690f1b`, `5ac4b28` [D] | Yes | Fix the two consistency defects in §2.6; put one sentence on the Overview saying which number is which |
| Efficiency 0.50, safety 1.00 defaults | Your request | `59535d6` [T] | Unverified | Confirm against the IE standard's allowances (question 1) |
| Workforce factors labor-only; overtime lifts both | Space is judged on cells, not staffing | `1357b20`, `56f6d40` [D] | Yes | Keep |
| Plan month never switches silently | A May export planned for June is a real case | README, `294bb2a` [D] | Yes | Keep |
| Carryover = older production month; count open rows | Your definition | `14a639d`, 2026-11-02 [T] | Yes | Keep; automate the status filter in the workbook reader |
| Sequencer skips stations with zero capacity | HC = 0 once meant "no such station here" | `bfe1fa1`, `234f047` [D] | **No** | Treat HC = 0 with demand as an overload; share the capacity helper (§3.4) |
| Unknown FG dropped, unknown accessory kept at zero | No labor computable vs base unit still computable | `6ec9dc1` [D, I] | Acceptable with the warnings | Add a plan-level "incomplete data" status that blocks "approved" (question 3) |
| Placeholders = family medians, warned | "Nothing invented"; your request for placeholders | PR #7, #8 [T] | Yes | Keep; show the placeholder share on the Overview |
| Per-facility routing as 15 catalog columns | No signature change; grid is already editable | `a0a141b` [D] | Works; costly to extend | Leave until a fourth plant or the Lvl 0 switch; then move to a (SKU, facility, station) table |
| Undercarriage second-hop reroute | Henderson has no undercarriage crew | PR #5 [D] | Yes | Keep |
| Process Flow graph draw-only | Explain the process, not drive it | storage docstring [D] | Yes | Fix the tab text; decide whether precedence should ever drive lead time |
| Final reads FN_Assy literally, including 0 | Your request | `159b1f3` [T] | Yes | Delete `FINAL_LABOR` and the mount/marry help text |
| GitHub files + Streamlit Cloud, no DB, no auth | Free, team URL, git as audit and backup | README, MAINTAINING [D] | Adequate for a handful of planners | Make the repo private before real customer data; move the PAT to a service account; add an Export-workbook backup cadence |
| Manual scenario = SKU quantities only | Named build-plan snapshot | `5f3c9cd` [D] | Too thin | Save a full plan snapshot (schedule, crew, settings, catalog SHA) |
| Executive scenario freezes metrics | Execs see the numbers as of the save | `1357b20` [D] | Yes | Version the schema; make Re-apply restore everything or say what it restores |
| Commit to main via PR branches; staging unused | Single maintainer, PR review by Claude | HANDOFF [D], practice | Yes | Rewrite MAINTAINING's staging section to match |
| Labor model stays on Base FG + kit until Lvl 0 SKUs exist | Your decision | 2026-11-02 [T] | Yes | Plan the switch with the BC routing work |
| Material Handling minutes 15/5/15/15 for Henderson | Your estimate | 2026-11-02 [T] | Unverified | Validate with a week of observed receiving/move time |

---

## 10. Codex's observed issues, classified

| Issue | Classification | Note |
|---|---|---|
| Overview 193 units vs Calendar empty in November | **Intentional** (and stale on today's main) | Explained by the plan guide; Calendar empty-state could name the file's month |
| Suggest's five-day weekly reference | **Fixed** (2026-10-23) | Codex's checkout predates it |
| Suggest skips a station whose capacity is zero | **Defect by semantic drift** | Originally intentional; the model's meaning of HC = 0 changed and the Sequencer did not follow |
| Zero headcount everywhere still yields dates and no overloads | **Defect** | Same root cause; reproduced on `6ac920c` |
| Unknown FG excluded, unknown accessory included at zero | **Intentional**, with the warnings | The missing piece is a plan-level status, not a different default |
| Placeholder SKUs and fallback values | **Intentional** | Your request; medians, warned |
| Manual scenarios save only quantities while exec scenarios save metrics | **Intentional but thin** | Two different purposes; neither reproduces a plan fully |
| Staging vs main guidance disagrees | **Unfinished** | HANDOFF is current; MAINTAINING's staging section is stale |
| Whole-mix cap ignores Conc = 0; headline shows potential | **Defect** (found in this review) | §2.6 |
| HS Final crew override, dead `FINAL_LABOR`, mount/marry text | **Unfinished** cleanup | §5.5 |
| Lead time one person | **Intentional** | Yardstick, reviewed by the IE |
| Battery over-constraint handled by exclusion | **Temporary compromise** | Since 2026-07-16, never revisited |

---

## 11. Highest-value improvements, in order

1. **Fix the Sequencer's capacity semantics** (§3.4). One shared capacity helper for Suggest, the diagnosis and the capacity table; HC = 0 with demand is an overload; include the cell check. Small, well-tested area, removes a wrong-answer path.
2. **Make the three capacity numbers consistent** (§2.6): apply the Conc = 0 block in the whole-mix cap; stop the headline/What-if from showing the potential cap without saying so.
3. **Add data status to the catalog and the plan**: a per-SKU source/status (measured / estimated / placeholder / missing) and a plan-level "incomplete: N units on placeholders, M accessories at zero" badge that can gate an "approved" state. This is the rule you already set for the BC routing workbook.
4. **Save a full plan snapshot** behind "Save plan" (schedule, crew, settings, catalog commit), and show "as saved" next to "recomputed today" in the Executive Summary.
5. **Documentation truth pass**: MAINTAINING staging section, `FINAL_LABOR` and mount/marry text, HS crew override, "9 columns" comment, Process Flow tab wording, Battery help example (raw vs safe).
6. **Capacity tab warning for `HC < Conc × Crew`**, mirroring the What-if.
7. **Repo hygiene you already own**: private repo before real customer data, service-account PAT, the 2026-10-20 product decisions (prune saved schedules, suffix view, telemetry).

**Avoid changing until there is evidence:**
- The capacity formulas, the workforce-factor placement, and the by-line buildable math. They reproduce the legacy sheet, passed an IE review and an adversarial audit, and you reconcile against them monthly. Change the inputs' defaults only after question 1 is answered.
- The 170 min battery cycle and the Battery exclusion. Measure a day of battery builds first.
- The per-facility routing columns. They are ugly but correct and the seeder covers the main hazard; re-shaping them now, mid-way through the Lvl 0 SKU work, would be done twice.
- The GitHub storage model. The pain points are fixed; the next step is a snapshot format, not a database.
- Anything that re-introduces a silent month switch, a silent unit drop, or an `st.rerun()` inside the sidebar.

---

## 12. Questions only you can answer

1. **Do the IE's minutes already include allowances** (personal, fatigue, delay)? If yes, 0.50 efficiency double counts them and the default should rise toward 0.80–0.90 with absenteeism kept separate.
2. **Should HC = 0 ever mean "station not present"** anywhere, now that routing expresses that? If no, the Sequencer fix is straightforward and the Capacity tab's 🔴 NO STATION is the single meaning.
3. **Do you want an "approved plan" state?** If yes: who approves, what must be complete (no placeholders? no zero-labor accessories?), and is an approved plan frozen or recomputed when standards change?
4. **When you reopen an old saved plan, which do you expect:** the numbers as they were, today's numbers from the same SKU list, or both side by side?
5. **Battery:** is 170 calendar minutes per battery per cell still right, and is the exclusion still in use at Henderson? Per-facility cycle?
6. **Sequencer scope:** keep it as a labor-and-cells level-loader, or do you want operation precedence and material readiness enforced? The second needs kit status that only BC/MES can provide.
7. **Which system should own the approved schedule once the MES exists**: this tool (it owns the plan today), BC, or the MES's "published day"?
8. **Staging:** retire the staging branch and app, or revive it? HANDOFF says retired; MAINTAINING still says mandatory.
9. **Material Handling minutes** (15/5/15/15): your estimate or observed? Should they vary by family?
10. **Lead time:** keep the one-person yardstick, or do you want an elapsed estimate once precedence is modelled?

---

*Sources: repo history `e86cceb..6ac920c`, PRs #1–#29, README.md, HANDOFF.md, MAINTAINING.md, `docs/MES_*.md` in the main checkout, your messages in the 2026-06-15 to 2026-11-02 sessions, and a read-only pass over `app.py`, `core/` and `tests/` on `6ac920c`. One scratch script reproduced the zero-headcount Sequencer result.*
