"""Labor Capacity Tool (Henderson / Spartanburg / Cypress) — Streamlit web app.

Run with:   streamlit run app.py

The app reads CSV inputs from data/, lets the user adjust crew/safety/days
in the sidebar, and shows 8 dashboards as tabs.
"""
from __future__ import annotations

from pathlib import Path
import io

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.data_loader import (
    load_machine_labor, load_acc_labor, load_schedule, build_manual_schedule,
)
from core.labor_calculator import (
    expand_schedule, build_capacity_table,
    battery_demand_by_sku, battery_demand_by_type,
)
from core.constants import (
    LOCATIONS, STATION_DEFAULTS, DEFAULT_SHIFT_MINUTES, DEFAULT_WORKING_DAYS,
    DEFAULT_SAFETY_FACTOR, DEFAULT_EFFICIENCY_FACTOR,
)
from core.facility_storage import (
    load_facility_crew_df, save_facility_crew_to_github,
)
from core.catalog_storage import save_catalog_to_github
from core.data_validator import validate_all

DATA_DIR = Path(__file__).resolve().parent / "data"


# =============================================================
# Page config & styling
# =============================================================
st.set_page_config(
    page_title="Labor Capacity Tool",
    page_icon="🏭",
    layout="wide",
)

# A few small style tweaks for a cleaner, more dashboard-like look.
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
      [data-testid="stMetricValue"] { font-size: 1.55rem; }
      [data-testid="stMetricLabel"] { font-size: 0.85rem; opacity: 0.9; }
      h1, h2, h3 { letter-spacing: -0.01em; }
      /* Tighter tab labels */
      .stTabs [data-baseweb="tab-list"] { gap: 6px; }
      .stTabs [data-baseweb="tab"] { padding-left: 14px; padding-right: 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# =============================================================
# Data loading (cached, with file-mtime invalidation)
# =============================================================
def _csv_mtime(filename: str) -> float:
    """Return the mtime of a CSV under data/, or 0 if missing.

    Including this value as a cache argument makes Streamlit invalidate the
    cached DataFrame whenever the CSV file is updated (e.g. via git push).
    """
    p = DATA_DIR / filename
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_data(show_spinner=False)
def _load_machine_df(_mtime: float):
    return load_machine_labor()


@st.cache_data(show_spinner=False)
def _load_acc_df(_mtime: float):
    return load_acc_labor()


def _load_schedule_df(uploaded_file=None, location: str = "HENDERSON") -> pd.DataFrame:
    """Schedule loader — accepts an uploaded CSV or falls back to bundled data.

    Passes the file as a BytesIO object so no temp file is written to disk,
    which prevents race conditions when multiple users upload simultaneously.
    """
    machine_skus = set(_load_machine_df(_csv_mtime("machine_clean.csv"))["SKU"])
    if uploaded_file is not None:
        buf = io.BytesIO(uploaded_file.getvalue())
        return load_schedule(buf, location=location, machine_skus=machine_skus)
    return load_schedule(location=location, machine_skus=machine_skus)


# =============================================================
# Sidebar — global inputs
# =============================================================
def render_sidebar() -> dict:
    st.sidebar.title("🏭 Labor Planning")
    st.sidebar.caption("Set up your scenario, then review tabs on the right.")
    st.sidebar.markdown("---")

    # ----------------------------------------------------------------
    # STEP 1 — Facility (always visible)
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 1 · Pick a facility")
    location = st.sidebar.selectbox(
        "Facility",
        LOCATIONS,
        index=0,
        label_visibility="collapsed",
        help="Each facility has its own saved headcount and station setup.",
    )

    # ----------------------------------------------------------------
    # STEP 2 — Schedule (always visible)
    # ----------------------------------------------------------------
    st.sidebar.markdown("#### Step 2 · Tell us what to build")
    schedule_mode = st.sidebar.radio(
        "How would you like to enter the build plan?",
        ["📤 Upload schedule file", "✏️ Type a few SKUs"],
        index=0,
        help=(
            "Upload a full production schedule (CSV) — best for monthly planning. "
            "Or type a few SKU rows by hand — best for quick what-if scenarios."
        ),
    )

    uploaded = None
    manual_entries = None

    if schedule_mode == "📤 Upload schedule file":
        uploaded = st.sidebar.file_uploader(
            "Choose a CSV",
            type=["csv"],
            help="Expected columns: LOCATION, FG SKU ID, FG ACCRY SKU ID, BUILD QTY, PRODUCTION MONTH.",
            label_visibility="collapsed",
        )
        if uploaded is None:
            st.sidebar.info("Using the bundled May 2026 sample schedule.")
    else:
        st.sidebar.caption(
            "Enter FG SKU, Accessory SKU (optional), and Quantity for each row."
        )
        default_entries = pd.DataFrame({
            "FG SKU": [""] * 8,
            "Accessory SKU": [""] * 8,
            "Qty": [0] * 8,
        })
        manual_entries = st.sidebar.data_editor(
            default_entries,
            use_container_width=True,
            num_rows="dynamic",
            key=f"manual_entries_{location}",
            column_config={
                "FG SKU": st.column_config.TextColumn("FG SKU", help="e.g. BOSS25-006"),
                "Accessory SKU": st.column_config.TextColumn(
                    "Accessory SKU", help="e.g. BOSS25-A016 (optional)"
                ),
                "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            },
        )

    st.sidebar.markdown("---")

    # ----------------------------------------------------------------
    # STEP 3 — Settings, behind an expander
    # ----------------------------------------------------------------
    with st.sidebar.expander("⏱️ Working-time settings", expanded=False):
        st.caption("How much capacity you have per person per day.")
        days = st.number_input(
            "Working days in the period",
            min_value=1, max_value=31,
            value=DEFAULT_WORKING_DAYS,
            help="E.g. 20 for a typical month with 5-day weeks.",
        )
        shift = st.number_input(
            "Shift length (minutes)",
            min_value=60, max_value=1440,
            value=DEFAULT_SHIFT_MINUTES, step=30,
            help="480 = 8 hours.",
        )
        safety = st.slider(
            "Planning buffer (safety factor)",
            min_value=0.50, max_value=1.00,
            value=DEFAULT_SAFETY_FACTOR, step=0.05,
            help="0.85 leaves a 15% buffer. 1.00 = use 100% of capacity (no buffer).",
        )
        efficiency = st.slider(
            "Productive time (efficiency factor)",
            min_value=0.40, max_value=1.00,
            value=DEFAULT_EFFICIENCY_FACTOR, step=0.025,
            help=(
                "Fraction of the shift that is actually productive — breaks, setup, "
                "and rework reduce it. 1.00 = no loss. Use 0.625 to match the VSM standard."
            ),
        )

    # ----------------------------------------------------------------
    # STEP 4 — Station headcount (per facility), behind an expander
    # ----------------------------------------------------------------
    with st.sidebar.expander(f"👥 Station headcount for {location}", expanded=False):
        st.caption(
            "Edit how many people work at each station. Set **People = 0** "
            "for any station that doesn't exist at this facility."
        )

        crew_default_df = load_facility_crew_df(location)
        # Friendly display: rename columns for the editor
        display_crew = crew_default_df.rename(
            columns={"HC": "People", "Conc": "Stations/Cells", "Crew": "Crew per unit"}
        )

        edited_display = st.data_editor(
            display_crew,
            use_container_width=True,
            num_rows="fixed",
            key=f"crew_editor_{location}",
            column_config={
                "People": st.column_config.NumberColumn(
                    "People", min_value=0, step=1,
                    help="Total headcount available at this station.",
                ),
                "Stations/Cells": st.column_config.NumberColumn(
                    "Stations/Cells", min_value=0, step=1,
                    help="How many units can be worked on at the same time (parallel bays/cells).",
                ),
                "Crew per unit": st.column_config.NumberColumn(
                    "Crew per unit", min_value=1, step=1,
                    help="People assigned to one unit at this station.",
                ),
            },
        )
        # Convert back to internal column names
        edited = edited_display.rename(
            columns={"People": "HC", "Stations/Cells": "Conc", "Crew per unit": "Crew"}
        )

        # Save button
        if st.button(f"💾 Save crew for {location}", use_container_width=True):
            token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
            if not token:
                st.error(
                    "GitHub token not configured. Ask your admin to add "
                    "`github_token` to Streamlit Secrets."
                )
            else:
                try:
                    with st.spinner(f"Saving {location} crew to GitHub..."):
                        save_facility_crew_to_github(location, edited, token)
                    st.success(
                        f"✅ Saved! Everyone sees the new {location} values "
                        "after the app redeploys (~1 min)."
                    )
                except Exception as e:
                    st.error(f"Save failed: {e}")

        # Live totals
        total_hc = int(edited["HC"].sum())
        active_stations = int((edited["HC"] > 0).sum())
        st.markdown(
            f"**👥 Total headcount:** {total_hc}  \n"
            f"**🏭 Active stations:** {active_stations} of {len(edited)}"
        )

    # ----------------------------------------------------------------
    # Help / Glossary — always last
    # ----------------------------------------------------------------
    with st.sidebar.expander("ℹ️ Help & glossary", expanded=False):
        st.markdown(
            """
**Common terms**

- **Headcount (HC)** — number of people assigned to a station.
- **Stations/Cells (Conc)** — how many units can be worked on at the same time at one station.
- **Crew per unit** — number of people working together on one unit at a station.
- **Person-minutes (p-min)** — labor effort. 1 person working 1 min = 1 p-min.
- **Cycle time** — calendar minutes a unit physically spends at a station.
- **Lead time** — total calendar days for one person to build a unit alone.
- **Required HC** — number of people needed to meet the schedule.
- **Safety factor** — planning buffer applied to capacity.
- **Efficiency factor** — fraction of the shift that is actually productive.

**Unit classes**
- **STD** — standard trailer (full assembly with marry)
- **HS** — Head Skid only (no trailer)
- **HT** — Head + Trailer (mount only, no marry)

**Status colors**
- 🟢 OK · 🟡 Tight · 🟠 Near capacity · 🔴 Over capacity · ⚪ Not at this facility
            """
        )

    return {
        "uploaded": uploaded,
        "manual_entries": manual_entries,
        "schedule_mode": schedule_mode,
        "location": location,
        "safety": safety,
        "efficiency": efficiency,
        "days": days,
        "shift": shift,
        "crew_config": edited,
    }


# =============================================================
# Tab renderers
# =============================================================
def tab_overview(units, capacity, batt_type, inputs, schedule_month: str = ""):
    # =============================================================
    # Compute headline numbers
    # =============================================================
    total_units = len(units)
    total_labor = int(units["total_labor"].sum())
    total_batt = int(units["Bat"].where(units["Battery"] > 0, 0).sum())
    total_hc = int(inputs["crew_config"]["HC"].sum())
    active_stations = int((inputs["crew_config"]["HC"] > 0).sum())
    total_required_hc = int(capacity["required_hc"].sum())
    total_hc_gap = total_required_hc - total_hc

    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    no_station_list = capacity[capacity["overall_status"] == "🔴 NO STATION"].index.tolist()
    near_cap = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
    tight = capacity[capacity["overall_status"] == "🟡 TIGHT"].index.tolist()
    ok_stations = capacity[capacity["overall_status"] == "🟢 OK"].index.tolist()

    # =============================================================
    # Big status banner
    # =============================================================
    month_label = f" · {schedule_month}" if schedule_month else ""
    st.markdown(f"### {inputs['location']} Manufacturing{month_label}")

    has_blocker = bool(over_stations or no_station_list)
    has_warning = bool(near_cap)

    if has_blocker:
        blockers = []
        if over_stations:
            blockers.append(f"{len(over_stations)} station(s) **over capacity** ({', '.join(over_stations)})")
        if no_station_list:
            blockers.append(f"{len(no_station_list)} station(s) **not available** here ({', '.join(no_station_list)})")
        st.error(
            "🚨 **Action needed.** " + " · ".join(blockers)
            + "\n\nSee the **Capacity** and **Recommendations** tabs for fixes."
        )
    elif has_warning:
        st.warning(
            f"⚠️ **Watch closely.** {len(near_cap)} station(s) near capacity: "
            f"**{', '.join(near_cap)}**. Small changes could push them over."
        )
    else:
        st.success("✅ **All clear.** Every station is within safe capacity for this plan.")

    st.markdown("---")

    # =============================================================
    # Hero KPIs — what an executive needs at a glance
    # =============================================================
    st.markdown("#### 📦 What this plan builds")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Units to build", f"{total_units:,}",
        help="Total finished-good units in this schedule (or manual entry).",
    )
    c2.metric(
        "Total work", f"{total_labor:,} p-min",
        help="Total person-minutes of labor required across all stations.",
    )
    c3.metric(
        "Batteries required", f"{total_batt:,}",
        help="Total batteries that need to be assembled (BOSS units only).",
    )
    primary_bottleneck = over_stations[0] if over_stations else (
        near_cap[0] if near_cap else (tight[0] if tight else "None")
    )
    c4.metric(
        "Primary bottleneck", primary_bottleneck,
        help="The station closest to or over capacity. Address this first.",
    )

    st.markdown("#### 👥 Headcount picture")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric(
        "People you have", f"{total_hc}",
        help=f"Total headcount across {active_stations} active stations.",
    )
    c6.metric(
        "People you need", f"{total_required_hc}",
        help=(
            f"Sum of headcount required at every station to meet this plan, "
            f"using your buffer ({inputs['safety']:.2f}) and "
            f"productive-time settings ({inputs['efficiency']:.3f})."
        ),
    )
    gap_label = "Short" if total_hc_gap > 0 else ("Surplus" if total_hc_gap < 0 else "Even")
    c7.metric(
        gap_label, f"{abs(total_hc_gap)}",
        delta=f"{total_hc_gap:+d}" if total_hc_gap != 0 else None,
        delta_color="inverse",
        help="Difference between people you need and people you have.",
    )
    short_count = int((capacity["hc_gap"] > 0).sum())
    c8.metric(
        "Stations needing more people", f"{short_count}",
        help="Stations where required headcount exceeds what you have today.",
    )

    st.markdown("---")

    # =============================================================
    # Recommended actions — auto-generated from data
    # =============================================================
    st.markdown("#### 🎯 Recommended actions")
    actions = []
    # 1. Stations missing entirely
    if no_station_list:
        actions.append(
            f"**Fix facility setup.** {len(no_station_list)} station(s) have demand but 0 headcount: "
            f"{', '.join(no_station_list)}. Either add people in the sidebar or remove those units from the schedule."
        )
    # 2. Over-capacity stations
    if over_stations:
        actions.append(
            f"**Add capacity at {', '.join(over_stations)}.** "
            f"Options: add people, run overtime, extend working days, or defer some units. "
            f"See the **Recommendations** tab for specifics."
        )
    # 3. Headcount short overall
    if total_hc_gap > 0 and not over_stations:
        actions.append(
            f"**Plan to add ~{total_hc_gap} people** across the line to comfortably hit this plan."
        )
    # 4. Near-cap warnings
    if near_cap and not over_stations:
        actions.append(
            f"**Monitor {', '.join(near_cap)}** — these are 90%+ utilized and risky for any schedule change."
        )
    # 5. Surplus
    if total_hc_gap < -3:  # only call out meaningful surplus
        idle = capacity[capacity["labor_util_safe"] < 0.5].index.tolist()
        if idle:
            actions.append(
                f"**Surplus capacity at {', '.join(idle[:3])}** — consider cross-training "
                f"these {abs(total_hc_gap)} people to support bottleneck stations."
            )
    if not actions:
        actions.append("✅ **No action needed** — this plan is well-balanced. Maintain current staffing.")

    for i, action in enumerate(actions, 1):
        st.markdown(f"{i}. {action}")

    st.markdown("---")

    # =============================================================
    # Station status — colored cards instead of a dense table
    # =============================================================
    st.markdown("#### 📊 Station status at a glance")
    st.caption(
        "Each card shows one station. Color = overall status (labor + throughput). "
        "Number = how loaded it is."
    )

    # Build cards in rows of 4
    stations_to_show = list(capacity.index)
    color_map = {
        "🔴 OVER":       ("#E15759", "white"),
        "🔴 NO STATION": ("#999999", "white"),
        "🟠 NEAR-CAP":   ("#F0A04B", "white"),
        "🟡 TIGHT":      ("#F2D75D", "#333"),
        "🟢 OK":         ("#70AD47", "white"),
        "⚪ N/A":         ("#EEEEEE", "#666"),
    }
    for i in range(0, len(stations_to_show), 4):
        cols = st.columns(4)
        for j, st_name in enumerate(stations_to_show[i:i + 4]):
            row = capacity.loc[st_name]
            status = row["overall_status"]
            bg, fg = color_map.get(status, ("#EEEEEE", "#333"))
            util_pct = row["labor_util_safe"] * 100 if not row.get("station_missing") else 0
            hc = int(row["HC"])
            req = int(row["required_hc"])
            status_label = status.split(" ", 1)[1] if " " in status else status
            cols[j].markdown(
                f"""
<div style="background:{bg};color:{fg};padding:12px;border-radius:8px;margin-bottom:8px;">
<div style="font-weight:600;font-size:0.95rem;">{st_name}</div>
<div style="font-size:0.78rem;opacity:0.85;">{status_label}</div>
<div style="font-size:1.6rem;font-weight:700;margin-top:6px;">{util_pct:.0f}%</div>
<div style="font-size:0.78rem;opacity:0.85;">{hc} people · need {req}</div>
</div>
""",
                unsafe_allow_html=True,
            )

    st.markdown("---")

    # =============================================================
    # Battery type mix (kept — visual is clearer than table)
    # =============================================================
    st.markdown("#### 🔋 Batteries needed by type")
    fig = px.bar(
        batt_type, x="batt_type", y="total_batteries", color="batt_type",
        text="total_batteries",
        labels={"batt_type": "Battery type", "total_batteries": "Batteries"},
    )
    fig.update_traces(textposition="outside")
    fig.update_layout(showlegend=False, height=340, margin=dict(t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)


def tab_capacity_vs_demand(capacity, inputs):
    st.header("📊 Capacity vs Demand")
    st.markdown(
        "**Are stations comfortably staffed for this plan?** "
        "Each row is one station. Use the chart for a visual scan, the simple table for the headline numbers."
    )
    st.caption(
        f"Plan settings — shift: {inputs['shift']} min · "
        f"working days: {inputs['days']} · "
        f"buffer: {inputs['safety']:.2f} · "
        f"productive time: {inputs['efficiency']:.3f}"
    )

    # =============================================================
    # Utilization chart (most scannable)
    # =============================================================
    chart_data = capacity.reset_index()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["labor_util_safe"] * 100,
        name="Labor utilization",
        marker_color="#5B9BD5",
        text=[f"{v*100:.0f}%" for v in chart_data["labor_util_safe"]],
        textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["thru_util_safe"] * 100,
        name="Throughput utilization",
        marker_color="#ED7D31",
    ))
    fig.add_hline(y=100, line_dash="dash", line_color="red", annotation_text="100% (over capacity)")
    fig.add_hline(y=90, line_dash="dot", line_color="orange", annotation_text="90% (near cap)")
    fig.update_layout(
        barmode="group", height=420,
        yaxis_title="Utilization (%)",
        margin=dict(t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    # =============================================================
    # Simple, executive-friendly table
    # =============================================================
    st.markdown("#### 📋 Station summary")
    simple = pd.DataFrame(index=capacity.index)
    simple["Status"] = capacity["overall_status"]
    simple["People"] = capacity["HC"].astype(int)
    simple["Need"] = capacity["required_hc"].astype(int)
    simple["Gap"] = capacity["hc_gap"].astype(int)
    simple["Labor used %"] = (capacity["labor_util_safe"] * 100).round(0).astype(int)
    simple["Throughput used %"] = (capacity["thru_util_safe"] * 100).round(0).astype(int)
    simple["Avg cycle (min)"] = capacity["avg_cycle"].round(1)

    st.dataframe(
        simple, use_container_width=True, height=480,
        column_config={
            "Status":  st.column_config.TextColumn("Status", help="Color-coded overall health: 🟢 OK · 🟡 Tight · 🟠 Near cap · 🔴 Over · ⚪ Not at this facility."),
            "People":  st.column_config.NumberColumn("People", help="Headcount you have at this station."),
            "Need":    st.column_config.NumberColumn("Need", help="People you'd need to comfortably meet the plan."),
            "Gap":     st.column_config.NumberColumn("Gap", help="Need − People. Positive = short staffed."),
            "Labor used %": st.column_config.NumberColumn(
                "Labor used %",
                help="How much of the available labor at this station is consumed by the plan. >100% means over capacity.",
                format="%d%%",
            ),
            "Throughput used %": st.column_config.NumberColumn(
                "Throughput used %",
                help="How much of the line/cell time at this station is consumed. >100% means physical bottleneck.",
                format="%d%%",
            ),
        },
    )

    # =============================================================
    # Full detail — for engineers / planners who want all the numbers
    # =============================================================
    with st.expander("🔍 Show full detail (all columns)", expanded=False):
        disp = pd.DataFrame(index=capacity.index)
        disp["HC"] = capacity["HC"]
        disp["Required HC"] = capacity["required_hc"]
        disp["HC gap"] = capacity["hc_gap"]
        disp["Stations/Cells"] = capacity["Conc"]
        disp["Crew/unit"] = capacity["Crew"]
        disp["Labor demand (p-min)"] = capacity["labor_demand"].astype(int)
        disp["Labor capacity (with buffer)"] = capacity["labor_cap_safe"].astype(int)
        disp["Labor util (with buffer)"] = (capacity["labor_util_safe"] * 100).round(1).astype(str) + "%"
        disp["Labor status"] = capacity["labor_status_safe"]
        disp["Labor capacity (raw)"] = capacity["labor_cap_raw"].astype(int)
        disp["Labor util (raw)"] = (capacity["labor_util_raw"] * 100).round(1).astype(str) + "%"
        disp["Avg cycle (min)"] = capacity["avg_cycle"].round(1)
        disp["Units or batteries"] = capacity["units_or_batt"]
        disp["Need / day"] = capacity["need_per_day"].round(2)
        disp["Throughput cap (with buffer)"] = capacity["thru_cap_safe"].round(2)
        disp["Throughput util (with buffer)"] = (capacity["thru_util_safe"] * 100).round(1).astype(str) + "%"
        disp["Throughput status"] = capacity["thru_status_safe"]
        disp["Throughput cap (raw)"] = capacity["thru_cap_raw"].round(2)
        disp["Throughput util (raw)"] = (capacity["thru_util_raw"] * 100).round(1).astype(str) + "%"
        disp["Overall"] = capacity["overall_status"]
        st.dataframe(disp, use_container_width=True, height=480)
        st.caption(
            "**With buffer** = capacity multiplied by your safety + efficiency factors. "
            "**Raw** = theoretical maximum, no buffers applied. "
            "Battery row reports BATTERIES/day, not units/day."
        )


def tab_battery_throughput(batt_sku, batt_type, capacity, inputs):
    st.header("🔋 Battery demand & throughput")
    st.markdown(
        "**Can we build enough batteries to meet this plan?** "
        "Batteries are usually the bottleneck — this tab shows demand vs daily capacity."
    )

    total_batt = int(batt_sku["total_batteries"].sum())
    total_units = int(batt_sku["units"].sum())
    avg_per_unit = total_batt / total_units if total_units else 0
    daily_need = total_batt / inputs["days"] if inputs["days"] else 0

    cap_safe = capacity.loc["Battery Assembly", "thru_cap_safe"] if "Battery Assembly" in capacity.index else 0
    cap_raw = capacity.loc["Battery Assembly", "thru_cap_raw"] if "Battery Assembly" in capacity.index else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Batteries to build", f"{total_batt:,}",
        help="Total batteries needed across all units in this plan.",
    )
    c2.metric(
        "Avg per unit", f"{avg_per_unit:.2f}",
        help="Average batteries per BOSS unit (varies — BOSS25 = 1, BOSS220 = 3, BOSS400 = 5).",
    )
    c3.metric(
        "Need / day", f"{daily_need:.2f}",
        help=f"Daily build rate to finish in {inputs['days']} working days.",
    )
    c4.metric(
        "Have / day (with buffer)", f"{cap_safe:.1f}",
        delta=f"{(cap_safe - daily_need):+.1f} vs need",
        delta_color="normal" if cap_safe >= daily_need else "inverse",
        help="Daily capacity at the Battery Assembly cells, with your safety + efficiency buffers applied.",
    )

    if cap_safe < daily_need and cap_safe > 0:
        st.error(
            f"🚨 **Battery throughput is the bottleneck.** "
            f"You need ~{daily_need:.1f}/day but can only do ~{cap_safe:.1f}/day. "
            f"See **Recommendations** for fix options."
        )
    elif cap_safe == 0:
        st.warning(
            "⚠️ Battery Assembly is set to 0 cells/headcount for this facility. "
            "If you need batteries here, configure the station in the sidebar."
        )

    st.markdown("---")
    st.markdown("#### 📋 Battery demand by FG SKU")
    st.dataframe(batt_sku, use_container_width=True, height=360)

    st.markdown("#### 📊 Battery demand by type")
    cc1, cc2 = st.columns([2, 1])
    with cc1:
        fig = px.pie(batt_type, values="total_batteries", names="batt_type",
                     hole=0.45, title="Type mix")
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)
    with cc2:
        st.dataframe(batt_type, use_container_width=True)

    # Comparison view
    # =============================================================
    # Plain-English summary
    # =============================================================
    days_needed_safe = (total_batt / cap_safe) if cap_safe else float("inf")
    days_needed_raw = (total_batt / cap_raw) if cap_raw else float("inf")

    st.markdown("#### 📅 How many days to finish all batteries?")
    summary = pd.DataFrame([
        {
            "Scenario": "Realistic (with buffer)",
            "Capacity / day": f"{cap_safe:.1f} batt/day" if cap_safe else "—",
            "Days needed": f"{days_needed_safe:.1f} days" if days_needed_safe != float("inf") else "—",
            "Verdict": "✅ Fits" if (days_needed_safe <= inputs["days"]) else "❌ Over target",
        },
        {
            "Scenario": "Best case (no buffer)",
            "Capacity / day": f"{cap_raw:.1f} batt/day" if cap_raw else "—",
            "Days needed": f"{days_needed_raw:.1f} days" if days_needed_raw != float("inf") else "—",
            "Verdict": "✅ Fits" if (days_needed_raw <= inputs["days"]) else "❌ Over target",
        },
    ])
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.caption(
        f"Your target window is **{inputs['days']} working days**. "
        "The realistic scenario uses your safety + efficiency factors; "
        "the best case assumes everything runs flawlessly."
    )


def tab_mitigation(capacity, batt_sku, units, inputs):
    st.header("🔧 Recommendations")
    st.markdown(
        "**Where to find slack and how to close gaps.** "
        "Use this tab when one or more stations are tight, near, or over capacity."
    )

    # Quick summary of where the issues are
    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    near_stations = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
    if over_stations:
        st.error(f"🚨 Over capacity: **{', '.join(over_stations)}**. Address first.")
    if near_stations:
        st.warning(f"⚠️ Near capacity: **{', '.join(near_stations)}**. Monitor.")
    if not over_stations and not near_stations:
        st.success("✅ No stations are over or near capacity — no urgent action needed.")

    st.markdown("---")
    st.markdown("#### 👥 People you could move (rotation candidates)")
    st.caption(
        "Stations sorted from most idle to most loaded. The greener the row, the more "
        "people are free to cross-train and help bottleneck stations."
    )

    idle_rows = []
    for disp, row in capacity.iterrows():
        if row.get("station_missing", False):
            continue
        util = row["labor_util_safe"]
        idle_pct = max(0, 1 - util)
        idle_hc_eq = idle_pct * row["HC"]
        if util > 1:
            tag = "🔴 Bottleneck"
            note = "Needs help — add capacity here"
        elif idle_pct > 0.5:
            tag = "✅ Lots of slack"
            note = f"~{idle_hc_eq:.1f} people equivalent free"
        elif idle_pct > 0.3:
            tag = "🟡 Some slack"
            note = f"{idle_pct*100:.0f}% of capacity is idle"
        else:
            tag = "🟠 Busy"
            note = "Limited rotation potential"
        idle_rows.append({
            "Station": disp,
            "People": int(row["HC"]),
            "Used %": int(round(util * 100)),
            "Idle %": int(round(idle_pct * 100)),
            "Rotation potential": tag,
            "Notes": note,
            "_sort": idle_pct,
        })
    idle_df = pd.DataFrame(idle_rows).sort_values("_sort", ascending=False).drop(columns="_sort")
    st.dataframe(
        idle_df, use_container_width=True, height=420, hide_index=True,
        column_config={
            "Used %": st.column_config.NumberColumn("Used %", format="%d%%"),
            "Idle %": st.column_config.NumberColumn("Idle %", format="%d%%"),
        },
    )

    # =============================================================
    # Fix playbook — scenario-aware (works for any bottleneck)
    # =============================================================
    st.markdown("---")
    st.markdown("#### 🛠️ Standard fix options")
    st.caption(
        "Each option closes a capacity gap differently. Pick the right one based on "
        "whether you need a permanent vs. temporary fix and how much capex you can spend."
    )

    bottleneck = (over_stations[0] if over_stations
                  else (near_stations[0] if near_stations else "the bottleneck station"))

    playbook = pd.DataFrame([
        {
            "#": 1,
            "Option": f"Cross-train and rotate people to {bottleneck}",
            "What it does": "Move people from idle stations to where they're needed.",
            "Effort": "Low — uses the rotation table above",
            "Speed": "Days",
            "When to use": "🟢 Best first move — no capex, immediate",
        },
        {
            "#": 2,
            "Option": "Defer some units to next period",
            "What it does": "Pull the lowest-priority units out of this schedule.",
            "Effort": "Low — schedule change only",
            "Speed": "Immediate",
            "When to use": "🟢 When customer lead time can absorb it",
        },
        {
            "#": 3,
            "Option": "Overtime at the bottleneck",
            "What it does": f"Extra hours per day on {bottleneck}.",
            "Effort": "Low — staffing-only change",
            "Speed": "Days",
            "When to use": "🟡 Temporary fix; budget for OT premium",
        },
        {
            "#": 4,
            "Option": "Add Saturday shifts",
            "What it does": "Extra working days in the month.",
            "Effort": "Medium — touches whole line",
            "Speed": "Weeks",
            "When to use": "🟡 When OT isn't enough; weekend premium",
        },
        {
            "#": 5,
            "Option": "Hire / add a cell at the bottleneck",
            "What it does": "Permanent capacity increase.",
            "Effort": "High — hiring, training, possibly capex",
            "Speed": "Months",
            "When to use": "🟢 If demand is sustained beyond this period",
        },
    ]).set_index("#")
    st.dataframe(playbook, use_container_width=True, height=260)

    st.info(
        "**Tip for planners:** Combine a short-term move (rotation or overtime) "
        "with a structural decision (hiring or scheduling change) so you handle "
        "this month *and* set up the next one for success."
    )


def tab_floor_verification(machine_df, acc_df, schedule_df):
    st.header("✅ Floor Verification — Update Labor Catalogs")
    st.caption(
        "Edit labor values directly in the Machine or Accessory catalog. "
        "SKUs used in the current schedule are highlighted. "
        "Click **Save** to push changes to GitHub — all users see the new values "
        "after the app redeploys (~1 min)."
    )

    # Aggregate qty per SKU from the schedule for highlighting
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    catalog = st.radio(
        "Catalog to edit", ["Machine (FG SKU)", "Accessory (Acc SKU)"], horizontal=True,
    )

    if catalog == "Machine (FG SKU)":
        # Machine labor columns the user can edit
        editable_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy_old", "PDI", "QC", "Ship", "Bat"]
        display_df = machine_df.reset_index(drop=True).copy()
        display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_fg.get(s, 0)))
        display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="fv_m_only_used"
        )
        if only_used:
            display_df = display_df[display_df["In schedule"]].copy()

        # Sort: used first, then by SKU
        display_df = display_df.sort_values(
            by=["In schedule", "Used (qty)"], ascending=[False, False]
        ).reset_index(drop=True)

        # Column config: read-only for SKU/Description, editable for labor
        col_cfg = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
            "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
        }
        for c in editable_cols:
            col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

        edited = st.data_editor(
            display_df,
            use_container_width=True,
            num_rows="fixed",
            key="fv_machine_editor",
            height=600,
            column_config=col_cfg,
            hide_index=True,
        )

        # Save button
        if st.button("💾 Save updated Machine catalog to GitHub", use_container_width=True):
            _save_catalog_csv(
                edited=edited,
                source_df=machine_df,
                editable_cols=editable_cols,
                file_path="data/machine_clean.csv",
                label="machine",
            )

    else:  # Accessory
        editable_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                         "PMAcc", "GenAcc", "Compressor"]
        display_df = acc_df.reset_index(drop=True).copy()
        display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_acc.get(s, 0)))
        display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="fv_a_only_used"
        )
        if only_used:
            display_df = display_df[display_df["In schedule"]].copy()

        display_df = display_df.sort_values(
            by=["In schedule", "Used (qty)"], ascending=[False, False]
        ).reset_index(drop=True)

        # Add a live total — sum of all labor columns (per accessory, 1 battery basis)
        display_df["Total (1 batt)"] = display_df[editable_cols].fillna(0).sum(axis=1).astype(int)

        col_cfg = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
            "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
            "Total (1 batt)": st.column_config.NumberColumn(
                "Total (1 batt)", disabled=True,
                help="Sum of all labor columns for this accessory assuming 1 battery. "
                     "Multi-battery units scale the BattSubRaw portion accordingly.",
            ),
        }
        for c in editable_cols:
            col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

        edited = st.data_editor(
            display_df,
            use_container_width=True,
            num_rows="fixed",
            key="fv_acc_editor",
            height=600,
            column_config=col_cfg,
            hide_index=True,
        )

        st.caption(
            "💡 The **Total (1 batt)** column is the sum of all labor for one accessory, "
            "assuming 1 battery. For BOSS220 (3 batt) or BOSS400 (5 batt), the "
            "`BattSubRaw` portion is multiplied by the actual battery count."
        )

        if st.button("💾 Save updated Accessory catalog to GitHub", use_container_width=True):
            _save_catalog_csv(
                edited=edited,
                source_df=acc_df,
                editable_cols=editable_cols,
                file_path="data/acc_clean.csv",
                label="accessory",
            )


def _save_catalog_csv(edited, source_df, editable_cols, file_path, label):
    """Merge the edited values into the source DataFrame and push to GitHub."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    # Drop UI-only / derived columns before merging (Total (1 batt), Used (qty), In schedule)
    drop_cols = [c for c in ["Total (1 batt)", "Used (qty)", "In schedule"]
                 if c in edited.columns]
    edited = edited.drop(columns=drop_cols)

    # Reset edited to be keyed by SKU; pull labor edits
    edited_indexed = edited.set_index("SKU")
    out = source_df.copy()
    changed_rows = 0
    for sku in edited_indexed.index:
        if sku not in out.index:
            continue
        for col in editable_cols:
            new_val = edited_indexed.loc[sku, col]
            old_val = out.loc[sku, col]
            if pd.notna(new_val) and float(new_val) != float(old_val):
                out.loc[sku, col] = new_val
                changed_rows += 1

    if changed_rows == 0:
        st.info("No changes detected.")
        return

    # Write to CSV string (friendly column names — data_loader handles both formats)
    csv_text = out.drop(columns=["SKU"], errors="ignore").reset_index().to_csv(index=False)

    try:
        with st.spinner(f"Saving {label} catalog to GitHub ({changed_rows} cells changed)..."):
            save_catalog_to_github(
                csv_text, file_path, token,
                message=f"Update {label} catalog labor values via app ({changed_rows} cells)",
            )
        st.success(
            f"✅ Saved! {changed_rows} cells updated. All users see new values after redeploy (~1 min)."
        )
    except Exception as e:
        st.error(f"Save failed: {e}")


def tab_cycle_time(units, inputs):
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW

    st.header("⏱ Build Time per Unit")
    st.markdown(
        "**How long does it take to build one of each unit?**  \n"
        "• **Total Labor** — sum of person-minutes across every station the unit touches.  \n"
        "• **Lead Time (days)** — wall-clock days if **one person** built the whole unit "
        f"(`Total Labor ÷ {inputs['shift']} min × efficiency`).  \n"
        "• **Sum of Cycles** — wall-clock minutes if every station was done sequentially "
        "with your current Crew settings (upper bound — real flow has parallel sub-assembly)."
    )

    shift = inputs["shift"]
    efficiency = max(inputs["efficiency"], 0.01)  # avoid div-by-zero

    # ---- Build the per-pairing summary ----
    summary_rows = []
    detail_rows = []
    for (fg, acc, cls, bat), grp in units.groupby(["fg_base", "acc", "Class", "Bat"]):
        sample = grp.iloc[0]
        total_labor = 0
        sum_cycles = 0.0
        longest_st = None
        longest_cycle = 0.0
        per_station = {}

        for st_key in STATION_KEYS:
            lbr = sample[st_key]
            if lbr == 0:
                continue
            crew = inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"]
            if st_key == "Final" and cls == "HS":
                cycle = lbr / HS_FINAL_CREW
            else:
                cycle = lbr / crew if crew else 0
            total_labor += lbr
            sum_cycles += cycle
            if cycle > longest_cycle:
                longest_cycle = cycle
                longest_st = STATION_KEY_TO_DISPLAY[st_key]
            per_station[st_key] = (int(lbr), round(cycle, 1))

        # Lead time (days) — assumes 1 person, shift × efficiency productive minutes/day
        lead_days = total_labor / (shift * efficiency) if shift > 0 else 0

        summary_rows.append({
            "FG SKU": fg,
            "ACC SKU": acc or "—",
            "Class": cls,
            "Bat": int(bat),
            "Qty in schedule": len(grp),
            "Total Labor (p-min)": int(total_labor),
            "Lead Time (days)": round(lead_days, 2),
            "Sum of Cycles (min)": round(sum_cycles, 1),
            "Longest Station": longest_st or "—",
            "Longest Cycle (min)": round(longest_cycle, 1),
        })
        detail_rows.append({"key": f"{fg} + {acc or '—'}", "per_station": per_station, "cls": cls, "bat": int(bat)})

    summary_df = pd.DataFrame(summary_rows).sort_values(
        by="Total Labor (p-min)", ascending=False,
    ).reset_index(drop=True)

    # ---- Top metrics (across all unit pairings) ----
    if not summary_df.empty:
        avg_labor = summary_df["Total Labor (p-min)"].mean()
        avg_lead = summary_df["Lead Time (days)"].mean()
        slowest = summary_df.iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Unique unit pairings", f"{len(summary_df)}")
        c2.metric("Avg labor / unit", f"{int(avg_labor):,} p-min")
        c3.metric("Avg lead time / unit", f"{avg_lead:.1f} days")
        c4.metric(
            "Slowest unit", f"{slowest['FG SKU']}",
            help=f"{int(slowest['Total Labor (p-min)']):,} p-min · {slowest['Lead Time (days)']} days",
        )

    st.markdown("---")
    st.subheader("📋 Summary — one row per unit pairing")
    st.caption("Sorted by Total Labor descending. Each row = one unique FG + Accessory pairing in the current schedule.")
    st.dataframe(
        summary_df,
        use_container_width=True,
        height=380,
        hide_index=True,
        column_config={
            "Total Labor (p-min)": st.column_config.NumberColumn(
                "Total Labor (p-min)",
                help="Person-minutes summed across all stations.",
                format="%d",
            ),
            "Lead Time (days)": st.column_config.NumberColumn(
                "Lead Time (days)",
                help=f"Total Labor ÷ ({shift} min × {efficiency:.3f} efficiency). "
                     f"How long for one person to build the unit alone.",
                format="%.2f",
            ),
            "Sum of Cycles (min)": st.column_config.NumberColumn(
                "Sum of Cycles (min)",
                help="Sum of cycle times across stations (assumes purely sequential — upper bound).",
                format="%.1f",
            ),
            "Longest Cycle (min)": st.column_config.NumberColumn(
                "Longest Cycle (min)",
                help="Cycle time at the bottleneck station for this unit.",
                format="%.1f",
            ),
        },
    )

    # ---- Per-station drill-down ----
    st.markdown("---")
    st.subheader("🔎 Drill down: per-station breakdown")
    st.caption("Pick a unit pairing to see its labor and cycle time at each station.")

    pairing_choices = [r["key"] for r in detail_rows]
    if pairing_choices:
        chosen = st.selectbox(
            "Unit pairing",
            options=pairing_choices,
            index=0,
        )
        chosen_detail = next(r for r in detail_rows if r["key"] == chosen)

        breakdown_rows = []
        for st_key, (lbr, cycle) in chosen_detail["per_station"].items():
            crew = int(inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"])
            hc_used = HS_FINAL_CREW if (st_key == "Final" and chosen_detail["cls"] == "HS") else crew
            breakdown_rows.append({
                "Station": STATION_KEY_TO_DISPLAY[st_key],
                "Total Labor (p-min)": lbr,
                "Crew working in parallel": hc_used,
                "Cycle Time (min)": cycle,
            })
        breakdown_df = pd.DataFrame(breakdown_rows)
        # Add a TOTAL row
        total_lbr = int(breakdown_df["Total Labor (p-min)"].sum())
        total_cycle = round(breakdown_df["Cycle Time (min)"].sum(), 1)
        total_row = pd.DataFrame([{
            "Station": "🟦 TOTAL",
            "Total Labor (p-min)": total_lbr,
            "Crew working in parallel": "—",
            "Cycle Time (min)": total_cycle,
        }])
        breakdown_df = pd.concat([breakdown_df, total_row], ignore_index=True)

        st.dataframe(
            breakdown_df, use_container_width=True, hide_index=True, height=460,
            column_config={
                "Total Labor (p-min)": st.column_config.NumberColumn(format="%d"),
                "Cycle Time (min)": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        # Quick read-out for this unit
        lead_days = total_lbr / (shift * efficiency) if shift > 0 else 0
        st.info(
            f"**Build summary for {chosen}:**  \n"
            f"• Total work: **{total_lbr:,} person-minutes** "
            f"(= {total_lbr/60:.1f} person-hours)  \n"
            f"• Lead time (1 person, fully utilized): **{lead_days:.2f} days**  \n"
            f"• Sequential build cycle (with current Crew): **{total_cycle:.0f} cal-min** "
            f"(= {total_cycle/60:.1f} hours, ~{total_cycle/shift:.2f} shifts)"
        )


def tab_data_validation(machine_df, acc_df):
    st.header("🔍 Data Validation")
    st.caption(
        "Automatic checks over the Machine and Accessory catalogs. Re-runs every "
        "time the app reloads — push updated CSVs to GitHub to clear any issues."
    )

    issues_df = validate_all(machine_df, acc_df)

    if issues_df.empty:
        st.success("✅ No issues found in either catalog.")
        return

    # Summary metrics
    err_count = int((issues_df["Severity"] == "🔴 Error").sum())
    warn_count = int((issues_df["Severity"] == "🟡 Warning").sum())
    info_count = int((issues_df["Severity"] == "ℹ️ Info").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("🔴 Errors", err_count, help="Likely bugs — fix these first.")
    c2.metric("🟡 Warnings", warn_count, help="Suspicious — review and confirm.")
    c3.metric("ℹ️ Info", info_count, help="Flags or notes already known about.")

    st.markdown("---")

    # Filter
    sev_filter = st.multiselect(
        "Filter by severity",
        options=["🔴 Error", "🟡 Warning", "ℹ️ Info"],
        default=["🔴 Error", "🟡 Warning", "ℹ️ Info"],
    )
    cat_filter = st.multiselect(
        "Filter by category",
        options=sorted(issues_df["Category"].unique().tolist()),
        default=sorted(issues_df["Category"].unique().tolist()),
    )

    filtered = issues_df[
        issues_df["Severity"].isin(sev_filter) & issues_df["Category"].isin(cat_filter)
    ]
    st.dataframe(filtered, use_container_width=True, height=600, hide_index=True)

    # Download
    csv = filtered.to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download issues CSV",
        csv,
        "catalog_issues.csv",
        "text/csv",
    )


def tab_source_data(machine_df, acc_df, schedule_df):
    st.header("📁 Source Data")
    st.caption(
        "Raw inputs to the model. SKUs used in the current schedule/manual entry "
        "are highlighted and listed first."
    )

    # Aggregate qty per FG and Accessory SKU from the schedule
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    sub = st.radio(
        "Choose dataset",
        ["Schedule", "Machine_Clean", "Acc_Clean"],
        horizontal=True,
    )

    if sub == "Schedule":
        st.dataframe(schedule_df, use_container_width=True, height=600)
    elif sub == "Machine_Clean":
        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="m_only_used"
        )
        m_disp = machine_df.copy()
        m_disp.insert(0, "Used (qty)", m_disp.index.map(lambda s: used_fg.get(s, 0)))
        m_disp.insert(1, "In schedule", m_disp["Used (qty)"] > 0)
        if only_used:
            m_disp = m_disp[m_disp["In schedule"]]
        # Sort: used first (by qty desc), then alphabetical
        m_disp = m_disp.sort_values(
            by=["In schedule", "Used (qty)"],
            ascending=[False, False],
        )
        # Highlight rows that are in the schedule
        def _highlight_machine(row):
            return ["background-color: #FFF3CD"] * len(row) if row["In schedule"] else [""] * len(row)
        st.dataframe(
            m_disp.style.apply(_highlight_machine, axis=1),
            use_container_width=True, height=600,
        )
        st.caption(f"🟡 highlighted = used in current schedule ({sum(m_disp['In schedule'])} of {len(m_disp)} shown)")
    else:  # Acc_Clean
        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="a_only_used"
        )
        a_disp = acc_df.copy()
        a_disp.insert(0, "Used (qty)", a_disp.index.map(lambda s: used_acc.get(s, 0)))
        a_disp.insert(1, "In schedule", a_disp["Used (qty)"] > 0)
        # Per-accessory total time (sum of all labor columns, 1-battery basis)
        acc_labor_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                          "PMAcc", "GenAcc", "Compressor"]
        a_disp["Total (1 batt)"] = a_disp[acc_labor_cols].fillna(0).sum(axis=1).astype(int)
        if only_used:
            a_disp = a_disp[a_disp["In schedule"]]
        a_disp = a_disp.sort_values(
            by=["In schedule", "Used (qty)"],
            ascending=[False, False],
        )
        def _highlight_acc(row):
            return ["background-color: #FFF3CD"] * len(row) if row["In schedule"] else [""] * len(row)
        st.dataframe(
            a_disp.style.apply(_highlight_acc, axis=1),
            use_container_width=True, height=600,
        )
        st.caption(f"🟡 highlighted = used in current schedule ({sum(a_disp['In schedule'])} of {len(a_disp)} shown)")


# =============================================================
# Main
# =============================================================
def main():
    st.title("🏭 Labor Capacity Planner")
    st.caption(
        "Plan production capacity across facilities. "
        "Set up your scenario in the sidebar, then review the tabs below."
    )

    inputs = render_sidebar()

    # Load data with cache
    machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
    acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))

    if inputs.get("schedule_mode") == "✏️ Type a few SKUs":
        manual_entries = inputs.get("manual_entries")
        if manual_entries is None or manual_entries.empty:
            st.info("✏️ **Manual entry mode** — fill in FG SKU and Quantity in the sidebar to begin.")
            return
        machine_skus = set(machine_df["SKU"])
        schedule_df = build_manual_schedule(
            manual_entries, location=inputs["location"], machine_skus=machine_skus,
        )
        if schedule_df.empty:
            st.info("✏️ **Manual entry mode** — please fill in at least one row (FG SKU + Quantity > 0) in the sidebar.")
            return
    else:
        schedule_df = _load_schedule_df(inputs["uploaded"], location=inputs["location"])
        if schedule_df.empty:
            st.error(
                f"No schedule rows found for **{inputs['location']}**. "
                "Upload a schedule CSV that contains this location, or select a different location."
            )
            return

    # Compute everything
    units = expand_schedule(schedule_df, machine_df, acc_df)
    if units.empty:
        st.error("Could not expand schedule into units. Check FG/Acc SKUs match the catalog.")
        return

    capacity = build_capacity_table(
        units, inputs["crew_config"], inputs["shift"], inputs["days"],
        inputs["safety"], inputs["efficiency"],
    )
    batt_sku = battery_demand_by_sku(units)
    batt_type = battery_demand_by_type(units)

    # Detect schedule month for display (Mon-YY format rows, not carryover)
    current_months = schedule_df.loc[~schedule_df["CARRYOVER"], "PRODUCTION MONTH"].unique().tolist()
    schedule_month = current_months[0] if current_months else ""

    # Tabs — ordered from executive summary → planner detail → admin
    tabs = st.tabs([
        "🏠 Overview",
        "📊 Capacity",
        "🔋 Batteries",
        "🔧 Recommendations",
        "⏱ Build Time",
        "✅ Update Labor",
        "🔍 Data Quality",
        "📁 Source Data",
    ])

    with tabs[0]:
        tab_overview(units, capacity, batt_type, inputs, schedule_month)
    with tabs[1]:
        tab_capacity_vs_demand(capacity, inputs)
    with tabs[2]:
        tab_battery_throughput(batt_sku, batt_type, capacity, inputs)
    with tabs[3]:
        tab_mitigation(capacity, batt_sku, units, inputs)
    with tabs[4]:
        tab_cycle_time(units, inputs)
    with tabs[5]:
        tab_floor_verification(machine_df, acc_df, schedule_df)
    with tabs[6]:
        tab_data_validation(machine_df, acc_df)
    with tabs[7]:
        tab_source_data(machine_df, acc_df, schedule_df)


if __name__ == "__main__":
    main()
