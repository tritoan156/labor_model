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

# Suppress the default page padding for a more dashboard-y look
st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; padding-bottom: 2rem; }
      [data-testid="stMetricValue"] { font-size: 1.5rem; }
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
    st.sidebar.header("⚙️ Inputs")

    # Location selector
    st.sidebar.subheader("Location")
    location = st.sidebar.selectbox(
        "Facility",
        LOCATIONS,
        index=0,
        help="Filter the schedule to the selected facility.",
    )

    # Schedule data — pick CSV upload OR manual SKU entry
    st.sidebar.subheader("Schedule data")
    schedule_mode = st.sidebar.radio(
        "Mode",
        ["📤 Upload CSV", "✏️ Manual entry"],
        index=0,
        help="Upload a production schedule, or quickly type a few SKUs for an ad-hoc capacity check.",
    )

    uploaded = None
    manual_entries = None

    if schedule_mode == "📤 Upload CSV":
        uploaded = st.sidebar.file_uploader(
            "Upload production schedule CSV",
            type=["csv"],
            help="Defaults to bundled May 2026 Henderson schedule.",
        )
        if uploaded is None:
            st.sidebar.caption("📁 Using bundled May 2026 Henderson schedule")
    else:
        st.sidebar.caption(
            "Type FG SKU + Accessory SKU + Qty. Leave Accessory blank if none."
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

    # Working time
    st.sidebar.subheader("Working time")
    safety = st.sidebar.slider(
        "Safety factor",
        min_value=0.50, max_value=1.00,
        value=DEFAULT_SAFETY_FACTOR, step=0.05,
        help="0.85 = 15% buffer (recommended). 1.00 = raw capacity, no buffer.",
    )
    efficiency = st.sidebar.slider(
        "Efficiency factor",
        min_value=0.40, max_value=1.00,
        value=DEFAULT_EFFICIENCY_FACTOR, step=0.025,
        help=(
            "Fraction of shift time that is actually productive (breaks, setup, "
            "rework reduce it). 1.00 = no loss. 0.625 ≈ VSM productive-time rate."
        ),
    )
    days = st.sidebar.number_input(
        "Working days/month",
        min_value=1, max_value=31,
        value=DEFAULT_WORKING_DAYS,
    )
    shift = st.sidebar.number_input(
        "Shift length (mins)",
        min_value=60, max_value=1440,
        value=DEFAULT_SHIFT_MINUTES, step=30,
    )

    # Crew config — loaded per-facility from data/facility_crew.json
    # Using a facility-scoped widget key means edits to one facility don't
    # bleed into another; Streamlit preserves each facility's edits separately.
    st.sidebar.subheader(f"Station crew — {location}")
    st.sidebar.caption("HC=0 means the station does not exist at this facility.")

    crew_default_df = load_facility_crew_df(location)

    edited = st.sidebar.data_editor(
        crew_default_df,
        use_container_width=True,
        num_rows="fixed",
        key=f"crew_editor_{location}",
        column_config={
            "HC": st.column_config.NumberColumn(
                "HC", min_value=0, step=1,
                help="Set to 0 if this station does not exist at the selected facility.",
            ),
            "Conc": st.column_config.NumberColumn(
                "Conc", min_value=0, step=1,
                help="Set to 0 if this station does not exist at the selected facility.",
            ),
            "Crew": st.column_config.NumberColumn("Crew", min_value=1, step=1),
        },
    )

    # Save button — persists to GitHub for everyone
    if st.sidebar.button(f"💾 Save crew for {location}", use_container_width=True):
        token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
        if not token:
            st.sidebar.error(
                "GitHub token not configured. Add `github_token` to Streamlit Secrets "
                "to enable saving."
            )
        else:
            try:
                with st.spinner(f"Saving {location} crew to GitHub..."):
                    save_facility_crew_to_github(location, edited, token)
                st.sidebar.success(
                    f"✅ Saved! All users will see the new {location} values "
                    "after Streamlit Cloud redeploys (~1 min)."
                )
            except Exception as e:
                st.sidebar.error(f"Save failed: {e}")

    # Total headcount summary (live)
    total_hc = int(edited["HC"].sum())
    active_stations = int((edited["HC"] > 0).sum())
    st.sidebar.markdown(
        f"**👥 Total headcount:** {total_hc}  \n"
        f"**🏭 Active stations:** {active_stations} / {len(edited)}"
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
    st.header("🏠 Overview")
    month_label = f" — {schedule_month}" if schedule_month else ""
    st.caption(f"{inputs['location']} Manufacturing — Energy Storage Systems{month_label}")

    total_units = len(units)
    total_labor = units["total_labor"].sum()
    total_batt = int(units["Bat"].where(units["Battery"] > 0, 0).sum())
    total_hc = int(inputs["crew_config"]["HC"].sum())
    active_stations = int((inputs["crew_config"]["HC"] > 0).sum())
    total_required_hc = int(capacity["required_hc"].sum())
    total_hc_gap = total_required_hc - total_hc

    over_stations = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
    bottleneck = over_stations[0] if over_stations else "None"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total units", f"{total_units:,}")
    c2.metric("Total labor (person-mins)", f"{int(total_labor):,}")
    c3.metric("Total batteries", f"{total_batt:,}")
    c4.metric("Primary bottleneck", bottleneck)

    # Second row — headcount picture
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Current HC", f"{total_hc}", help=f"{active_stations} active stations")
    c6.metric(
        "Required HC",
        f"{total_required_hc}",
        help=(
            f"Sum of HC needed at each station, given safety={inputs['safety']:.2f} "
            f"× efficiency={inputs['efficiency']:.3f}."
        ),
    )
    c7.metric(
        "HC gap",
        f"{total_hc_gap:+d}",
        delta=f"{total_hc_gap:+d}",
        delta_color="inverse",
        help="Positive = short staffed; negative = surplus.",
    )
    over_stations_count = int((capacity["hc_gap"] > 0).sum())
    c8.metric(
        "Stations short",
        f"{over_stations_count}",
        help="Stations where Required HC > Current HC.",
    )

    st.markdown("---")

    # Bottom line
    st.subheader("🎯 Bottom Line")
    no_station_list = capacity[capacity["overall_status"] == "🔴 NO STATION"].index.tolist()
    if no_station_list:
        st.error(
            f"**{len(no_station_list)} station(s) NOT AVAILABLE at {inputs['location']}** "
            f"but have demand: {', '.join(no_station_list)}.  \n"
            f"Set HC > 0 in the sidebar or remove those units from the schedule."
        )
    if "🔴 OVER" in capacity["overall_status"].values:
        over_list = capacity[capacity["overall_status"] == "🔴 OVER"].index.tolist()
        st.error(
            f"**{len(over_list)} station(s) OVER capacity:** {', '.join(over_list)}.  \n"
            f"See the **Capacity vs Demand** tab for details and the **Mitigation** tab for fix options."
        )
    elif "🟠 NEAR-CAP" in capacity["overall_status"].values:
        near = capacity[capacity["overall_status"] == "🟠 NEAR-CAP"].index.tolist()
        st.warning(
            f"**{len(near)} station(s) near capacity:** {', '.join(near)}.  "
            f"Monitor closely; small changes could push them over."
        )
    else:
        st.success("All stations are within capacity.")

    # Quick station status grid
    st.subheader("📊 Station status at a glance")
    status_df = capacity[["overall_status"]].copy()
    status_df.columns = ["Status"]
    st.dataframe(status_df, use_container_width=True, height=480)

    # Battery type mix mini-chart
    st.subheader("🔋 Battery demand by type")
    fig = px.bar(
        batt_type, x="batt_type", y="total_batteries", color="batt_type",
        text="total_batteries",
        labels={"batt_type": "Battery type", "total_batteries": "Batteries"},
    )
    fig.update_layout(showlegend=False, height=350)
    st.plotly_chart(fig, use_container_width=True)


def tab_capacity_vs_demand(capacity, inputs):
    st.header("📊 Capacity vs Demand")
    st.caption(
        f"Shift: {inputs['shift']} min · Days: {inputs['days']} · "
        f"Safety: {inputs['safety']:.2f} · Efficiency: {inputs['efficiency']:.3f}  ·  "
        f"Battery row reports BATTERIES/day (not units/day)."
    )

    # Compose a display-friendly DataFrame
    disp = pd.DataFrame(index=capacity.index)
    disp["HC"] = capacity["HC"]
    disp["Required HC"] = capacity["required_hc"]
    disp["HC gap"] = capacity["hc_gap"]
    disp["Conc"] = capacity["Conc"]
    disp["Crew"] = capacity["Crew"]
    disp["Labor demand"] = capacity["labor_demand"].astype(int)
    disp["Labor cap (safe)"] = capacity["labor_cap_safe"].astype(int)
    disp["Labor util (safe)"] = (capacity["labor_util_safe"] * 100).round(1).astype(str) + "%"
    disp["Labor status (safe)"] = capacity["labor_status_safe"]
    disp["Labor cap (raw)"] = capacity["labor_cap_raw"].astype(int)
    disp["Labor util (raw)"] = (capacity["labor_util_raw"] * 100).round(1).astype(str) + "%"
    disp["Labor status (raw)"] = capacity["labor_status_raw"]
    disp["Avg cycle (min)"] = capacity["avg_cycle"].round(1)
    disp["Units / batt"] = capacity["units_or_batt"]
    disp["Need / day"] = capacity["need_per_day"].round(2)
    disp["Thru cap (safe)"] = capacity["thru_cap_safe"].round(2)
    disp["Thru util (safe)"] = (capacity["thru_util_safe"] * 100).round(1).astype(str) + "%"
    disp["Thru status (safe)"] = capacity["thru_status_safe"]
    disp["Thru cap (raw)"] = capacity["thru_cap_raw"].round(2)
    disp["Thru util (raw)"] = (capacity["thru_util_raw"] * 100).round(1).astype(str) + "%"
    disp["Thru status (raw)"] = capacity["thru_status_raw"]
    disp["Overall"] = capacity["overall_status"]

    st.dataframe(disp, use_container_width=True, height=480)

    # Utilisation chart
    st.subheader("Utilization by station")
    chart_data = capacity.reset_index()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["labor_util_safe"] * 100,
        name=f"Labor util @ safety {inputs['safety']:.2f}",
        marker_color="#5B9BD5",
    ))
    fig.add_trace(go.Bar(
        x=chart_data["station_display"],
        y=chart_data["thru_util_safe"] * 100,
        name=f"Throughput util @ safety {inputs['safety']:.2f}",
        marker_color="#ED7D31",
    ))
    fig.add_hline(y=100, line_dash="dash", line_color="red", annotation_text="100% (over capacity)")
    fig.add_hline(y=90, line_dash="dot", line_color="orange", annotation_text="90% (near cap)")
    fig.update_layout(barmode="group", height=420, yaxis_title="Utilization (%)")
    st.plotly_chart(fig, use_container_width=True)


def tab_battery_throughput(batt_sku, batt_type, capacity, inputs):
    st.header("🔋 Battery Throughput")

    total_batt = int(batt_sku["total_batteries"].sum())
    total_units = int(batt_sku["units"].sum())
    avg_per_unit = total_batt / total_units if total_units else 0
    daily_need = total_batt / inputs["days"] if inputs["days"] else 0

    cap_safe = capacity.loc["Battery Assembly", "thru_cap_safe"]
    cap_raw = capacity.loc["Battery Assembly", "thru_cap_raw"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total batteries needed", f"{total_batt:,}")
    c2.metric("Avg per unit", f"{avg_per_unit:.2f}")
    c3.metric("Daily rate needed", f"{daily_need:.2f} / day")
    c4.metric("Cap (safe)", f"{cap_safe:.1f} / day",
              delta=f"{(daily_need - cap_safe):+.1f}",
              delta_color="inverse")

    st.markdown("---")
    st.subheader("Battery demand by SKU")
    st.dataframe(batt_sku, use_container_width=True, height=360)

    st.subheader("Battery demand by type")
    cc1, cc2 = st.columns([2, 1])
    with cc1:
        fig = px.pie(batt_type, values="total_batteries", names="batt_type",
                     hole=0.45, title="Type mix")
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)
    with cc2:
        st.dataframe(batt_type, use_container_width=True)

    # Comparison view
    st.subheader("Two ways to read battery throughput")
    cmp = pd.DataFrame({
        "Metric": ["Need", f"Cap @ {inputs['safety']:.2f}", "Cap @ 1.00",
                   "Days needed @ safety", "Days needed @ 1.00"],
        "Units / day view": [
            f"{capacity.loc['Battery Assembly','units_or_batt']/inputs['days']:.2f} u/d",  # not quite — left as informational
            f"{cap_safe:.2f}",
            f"{cap_raw:.2f}",
            f"{daily_need/cap_safe*inputs['days']:.1f}" if cap_safe else "∞",
            f"{daily_need/cap_raw*inputs['days']:.1f}" if cap_raw else "∞",
        ],
        "Batteries / day view": [
            f"{daily_need:.2f} batt/d",
            f"{cap_safe:.2f} batt/d",
            f"{cap_raw:.2f} batt/d",
            f"{total_batt/cap_safe:.1f}" if cap_safe else "∞",
            f"{total_batt/cap_raw:.1f}" if cap_raw else "∞",
        ],
    }).set_index("Metric")
    st.dataframe(cmp, use_container_width=True)


def tab_mitigation(capacity, batt_sku, units, inputs):
    st.header("🔧 Mitigation")
    st.caption("Fix options for over-capacity stations + idle HC available for rotation.")

    # Idle HC table
    st.subheader("Idle HC at other stations (rotation candidates)")

    idle_rows = []
    for disp, row in capacity.iterrows():
        if row.get("station_missing", False):
            # Station doesn't exist at this facility — skip from rotation candidates
            continue
        util = row["labor_util_safe"]
        idle_pct = max(0, 1 - util)
        idle_hc_eq = idle_pct * row["HC"]
        if util > 1:
            tag = "🔴 OVER"
            note = "Bottleneck — needs help"
        elif idle_pct > 0.5:
            tag = "✅ HIGH"
            note = f"~{idle_hc_eq:.1f} HC equivalent free"
        elif idle_pct > 0.3:
            tag = "🟡 MEDIUM"
            note = f"{idle_pct*100:.0f}% idle"
        else:
            tag = "🔴 LOW"
            note = "Already busy"
        idle_rows.append({
            "Station": disp,
            "HC": row["HC"],
            "Util (safe)": f"{util*100:.1f}%",
            "Idle %": f"{idle_pct*100:.1f}%",
            "Rotation potential": tag,
            "Notes": note,
        })
    st.dataframe(pd.DataFrame(idle_rows), use_container_width=True, height=480)

    # Option list
    st.subheader("Mitigation options for Battery Assembly")
    options = [
        {
            "#": 1,
            "Option": "Add 5th Battery cell + 2 ppl",
            "Mechanism": "5 cells × 2.82 batt/day = 14.1 batt/day raw → 12.0 with safety",
            "Impact": "+25% capacity (covers demand)",
            "Cost": "Space, training, $",
            "Recommendation": "🟢 BEST long-term — permanent fix",
        },
        {
            "#": 2,
            "Option": "Defer BOSS400HS-002 (×10) to June",
            "Mechanism": f"Removes 50 batteries (≈21% of demand)",
            "Impact": "Brings util down to ~85% @ 0.85",
            "Cost": "Customer lead time impact",
            "Recommendation": "🟢 STRONG — immediate, no capex",
        },
        {
            "#": 3,
            "Option": "4 hrs OT/day on Battery (5 days/week)",
            "Mechanism": "8 HC × 4 hr × 20d = 640 OT hrs",
            "Impact": "+49% labor cap",
            "Cost": "OT premium",
            "Recommendation": "🟡 GOOD — temporary",
        },
        {
            "#": 4,
            "Option": "Run 1 Saturday/week (4 extra days)",
            "Mechanism": "20→24 working days = +20% capacity",
            "Impact": "Closes most of the gap",
            "Cost": "Saturday premium",
            "Recommendation": "🟡 OK — temporary",
        },
        {
            "#": 5,
            "Option": "Rotate 1-2 ppl from Gen Acc / QC to Battery",
            "Mechanism": "Cross-train idle HC to Battery duty",
            "Impact": "+12-25% Battery cap",
            "Cost": "Cross-training time",
            "Recommendation": "🟡 GOOD — see idle HC table above",
        },
    ]
    st.dataframe(pd.DataFrame(options).set_index("#"), use_container_width=True, height=320)

    st.info(
        "**Recommended path:** Combine Option 2 (defer BOSS400HS-002) + Option 5 (rotate 1-2 from Gen Acc). "
        "This solves May without capex. For June+, plan Option 1 (5th cell)."
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
    st.header("⏱ Cycle Time per Unit")
    st.caption("Per finished unit (FG + ACC pairing): Total Labor and Cycle Time at each station.")

    # Build per-pairing table
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW
    rows = []
    for (fg, acc, cls, bat), grp in units.groupby(["fg_base", "acc", "Class", "Bat"]):
        sample = grp.iloc[0]
        row = {
            "FG Base": fg,
            "ACC": acc or "—",
            "Class": cls,
            "Bat": int(bat),
            "Qty in May": len(grp),
        }
        for st_key in STATION_KEYS:
            total_lbr = sample[st_key]
            if total_lbr == 0:
                continue
            crew = inputs["crew_config"].loc[STATION_KEY_TO_DISPLAY[st_key], "Crew"]
            if st_key == "Final" and cls == "HS":
                cycle = total_lbr / HS_FINAL_CREW
            else:
                cycle = total_lbr / crew if crew else 0
            row[f"{st_key} Total"] = int(total_lbr)
            row[f"{st_key} Cycle"] = round(cycle, 1)
        rows.append(row)

    df = pd.DataFrame(rows)
    # Sort May SKUs by qty desc
    df = df.sort_values(by="Qty in May", ascending=False).reset_index(drop=True)
    st.dataframe(df, use_container_width=True, height=600)


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
    st.title("🏭 Labor Capacity Tool")

    inputs = render_sidebar()

    # Load data with cache
    machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
    acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))

    if inputs.get("schedule_mode") == "✏️ Manual entry":
        manual_entries = inputs.get("manual_entries")
        if manual_entries is None or manual_entries.empty:
            st.info("✏️ Manual entry mode — fill in FG SKU and Qty in the sidebar to begin.")
            return
        machine_skus = set(machine_df["SKU"])
        schedule_df = build_manual_schedule(
            manual_entries, location=inputs["location"], machine_skus=machine_skus,
        )
        if schedule_df.empty:
            st.info("✏️ Manual entry mode — fill in at least one row (FG SKU + Qty > 0) in the sidebar.")
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

    # Tabs (Battery Allocation removed)
    tabs = st.tabs([
        "🏠 Overview",
        "📊 Capacity vs Demand",
        "🔋 Battery Throughput",
        "🔧 Mitigation",
        "✅ Floor Verification",
        "⏱ Cycle Time",
        "🔍 Data Validation",
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
        tab_floor_verification(machine_df, acc_df, schedule_df)
    with tabs[5]:
        tab_cycle_time(units, inputs)
    with tabs[6]:
        tab_data_validation(machine_df, acc_df)
    with tabs[7]:
        tab_source_data(machine_df, acc_df, schedule_df)


if __name__ == "__main__":
    main()
