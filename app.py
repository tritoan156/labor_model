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
    load_item_master, load_item_packages,
    resolve_item_time, unique_abbrs,
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
from core.catalog_storage import (
    save_catalog_to_github,
    fetch_catalog_csv_from_github,
    latest_catalog_sha,
    GitHubConflict,
)
from core.scenario_storage import (
    load_all_scenarios,
    list_scenarios,
    get_scenario,
    save_scenario_to_github,
    delete_scenario_on_github,
)
from core.process_flow_storage import (
    load_process_flow,
    save_process_flow_to_github,
    reset_process_flow_to_default,
    DEFAULT_EDGES as PROCESS_FLOW_DEFAULTS,
    VALID_CLASSES as PROCESS_FLOW_VALID_CLASSES,
)
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


# Cache version — bump this when the loader's OUTPUT SCHEMA changes (column
# renames, new columns, etc.) so the cache invalidates even if the underlying
# CSV file's mtime hasn't changed.
_LOADER_SCHEMA_VERSION = 4


@st.cache_data(show_spinner=False)
def _load_machine_df(_mtime: float, _schema_ver: int = _LOADER_SCHEMA_VERSION):
    return load_machine_labor()


@st.cache_data(show_spinner=False)
def _load_acc_df(_mtime: float, _schema_ver: int = _LOADER_SCHEMA_VERSION):
    """Load acc_clean.csv. `_schema_ver` forces a cache refresh after schema
    changes even when the file mtime is unchanged."""
    return load_acc_labor()


@st.cache_data(show_spinner=False)
def _load_item_master_df(_mtime: float):
    return load_item_master()


@st.cache_data(show_spinner=False)
def _load_item_packages_df(_mtime: float):
    return load_item_packages()


@st.cache_data(show_spinner=False)
def _load_scenarios_dict(_mtime: float):
    """Load data/scenarios.json. Cached per file mtime."""
    return load_all_scenarios()


@st.cache_data(show_spinner=False)
def _load_process_flow_edges(_mtime: float):
    """Load data/process_flow.json. Cached per file mtime."""
    return load_process_flow()




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
# SKU browser — supports the manual-entry "Browse catalog" panel
# =============================================================
def _render_scenarios_panel(location: str, seed_key: str, rev_key: str) -> None:
    """Sidebar expander for saving / loading named build-plan scenarios for the
    current facility. Persists to data/scenarios.json on GitHub."""
    # Always reload the latest scenarios from the cache (keyed on file mtime).
    scenarios = _load_scenarios_dict(_csv_mtime("scenarios.json")).get(location, {})
    scenario_names = sorted(scenarios.keys()) if isinstance(scenarios, dict) else []

    with st.sidebar.expander("📂 Save / load scenarios", expanded=False):
        # Replay any one-shot success / info / error message stashed before rerun
        toast_key = f"_scenario_toast_{location}"
        toast = st.session_state.pop(toast_key, None)
        if toast:
            level, msg = toast
            if level == "success":
                st.success(msg)
            elif level == "info":
                st.info(msg)
            else:
                st.error(msg)

        st.caption(
            f"Scenarios for **{location}** only. "
            "Saved to `data/scenarios.json` on GitHub — shared across all users."
        )

        # ------------------------------------------------------------------
        # SAVE
        # ------------------------------------------------------------------
        st.markdown("**Save current as**")
        save_name = st.text_input(
            "Scenario name",
            key=f"scenario_save_name_{location}",
            placeholder="e.g. May 2026 push",
            label_visibility="collapsed",
        )
        if st.button(
            "💾 Save scenario",
            key=f"scenario_save_btn_{location}",
            use_container_width=True,
        ):
            _scenario_save(location, save_name, seed_key, scenario_names)

        # ------------------------------------------------------------------
        # LOAD / DELETE
        # ------------------------------------------------------------------
        st.markdown("---")
        st.markdown("**Load a saved scenario**")
        if not scenario_names:
            st.caption(f"_No saved scenarios for {location} yet._")
            return

        chosen = st.selectbox(
            "Scenario",
            options=scenario_names,
            key=f"scenario_load_pick_{location}",
            label_visibility="collapsed",
        )
        # Show metadata for the chosen scenario
        meta = scenarios.get(chosen, {}) if isinstance(scenarios, dict) else {}
        n_entries = len(meta.get("entries", []) or [])
        saved_at = meta.get("saved_at", "")
        st.caption(f"📋 {n_entries} row(s)  ·  saved {saved_at}")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("📥 Load", key=f"scenario_load_btn_{location}",
                         use_container_width=True):
                _scenario_load(location, chosen, seed_key, rev_key)
        with c2:
            if st.button("🗑 Delete", key=f"scenario_del_btn_{location}",
                         use_container_width=True):
                _scenario_delete(location, chosen)


def _scenario_save(location: str, name: str, seed_key: str, existing_names: list) -> None:
    """Handler for the Save button — validates and pushes the current
    manual-entry rows as a named scenario."""
    name = (name or "").strip()
    if not name:
        st.warning("Please enter a scenario name before saving.")
        return

    # Build the entries DataFrame from the latest manual-editor state. Prefer
    # the current data_editor key (so any in-flight edits are captured);
    # fall back to the seed if the editor hasn't rendered yet.
    rev = st.session_state.get(f"manual_rev_{location}", 0)
    editor_key = f"manual_entries_{location}_v{rev}"
    df = st.session_state.get(editor_key)
    if not isinstance(df, pd.DataFrame):
        df = st.session_state.get(seed_key, pd.DataFrame(
            columns=["FG SKU", "Accessory SKU", "Qty"]
        ))

    # Check empty after stripping blank rows — let save handler do the cleaning
    non_empty = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if not non_empty.empty:
        non_empty["__fg"] = non_empty.get("FG SKU", "").astype(str).str.strip()
        non_empty["__qty"] = pd.to_numeric(
            non_empty.get("Qty", 0), errors="coerce",
        ).fillna(0).astype(int)
        non_empty = non_empty[(non_empty["__fg"] != "") & (non_empty["__qty"] > 0)]
    if non_empty.empty:
        st.warning(
            "The manual-entry table is empty. Fill in at least one row (FG SKU + Qty > 0) "
            "before saving the scenario."
        )
        return

    if name in existing_names:
        # Streamlit doesn't have a modal yet — use a session flag to require
        # a second click for confirmation.
        confirm_key = f"scenario_overwrite_confirmed_{location}_{name}"
        if not st.session_state.get(confirm_key, False):
            st.session_state[confirm_key] = True
            st.warning(
                f"A scenario named **{name}** already exists for {location}. "
                "Click **💾 Save scenario** again to overwrite it."
            )
            return
        # second click — clear the flag and proceed
        st.session_state[confirm_key] = False

    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    try:
        with st.spinner(f"Saving scenario '{name}'..."):
            response = save_scenario_to_github(location, name, df, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        # Stash a one-shot success message that survives the upcoming rerun
        st.session_state[f"_scenario_toast_{location}"] = (
            "success",
            f"✅ Saved scenario **{name}** for {location}"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Save failed: {e}")


def _scenario_load(location: str, name: str, seed_key: str, rev_key: str) -> None:
    """Handler for the Load button — replaces the manual-editor's seed with
    the saved scenario's rows and forces the editor to re-render."""
    block = get_scenario(location, name) or {}
    rows = block.get("entries", []) or []
    if not rows:
        st.warning(f"Scenario '{name}' has no entries.")
        return
    seed = pd.DataFrame(rows, columns=["FG SKU", "Accessory SKU", "Qty"])
    # Coerce Qty to int (json round-trip can produce floats)
    seed["Qty"] = pd.to_numeric(seed["Qty"], errors="coerce").fillna(0).astype(int)
    st.session_state[seed_key] = seed
    st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
    st.success(f"Loaded scenario **{name}** — {len(seed)} row(s).")
    st.rerun()


def _scenario_delete(location: str, name: str) -> None:
    """Handler for the Delete button."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner(f"Deleting scenario '{name}'..."):
            delete_scenario_on_github(location, name, token)
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.session_state[f"_scenario_toast_{location}"] = (
            "success", f"🗑 Deleted scenario **{name}** for {location}.",
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Delete failed: {e}")


def _render_sku_browser(machine_df, acc_df, location: str, seed_key: str, rev_key: str) -> None:
    """Sidebar expander that lets the user filter the machine catalog by
    family + class and add a chosen FG SKU + accessory + qty into the
    manual entries table (via session_state[seed_key])."""
    # Build a working frame with classification columns we can filter on.
    # reset_index(drop=True) avoids the SKU index/column ambiguity that
    # bites sort_values later.
    work = machine_df.reset_index(drop=True).copy()
    work["__family"] = work["SKU"].astype(str).apply(_machine_family)
    work["__class"] = work.apply(
        lambda r: _machine_class(r["SKU"], r.get("Description", "")), axis=1,
    )

    # Helpers used by both directions
    def _fg_label_for_row(row):
        bat = int(row.get("Bat", 0) or 0)
        cls = row.get("__class", "")
        bat_part = f" · {bat} batt" if bat > 0 else ""
        placeholder = " ⚠" if cls == "⚠ Placeholder" else ""
        return f"{row['SKU']} — {cls}{bat_part}{placeholder}"

    def _acc_label(sku: str) -> str:
        if sku == "(none)":
            return "(no accessory)"
        placeholder = " ⚠ family placeholder" if "999" in sku or "XXX" in sku.upper() else ""
        return f"{sku}{placeholder}"

    def _commit_add(fg_choice: str, acc_choice: str, qty: int) -> None:
        """Append the chosen row to the manual entries DataFrame and bump the rev."""
        editor_key = f"manual_entries_{location}_v{st.session_state.get(rev_key, 0)}"
        latest = st.session_state.get(editor_key)
        if isinstance(latest, pd.DataFrame):
            current = latest.copy()
        else:
            current = st.session_state.get(
                seed_key, pd.DataFrame(columns=["FG SKU", "Accessory SKU", "Qty"]),
            ).copy()
        current = pd.concat(
            [current, pd.DataFrame([{
                "FG SKU": fg_choice,
                "Accessory SKU": "" if acc_choice == "(none)" else acc_choice,
                "Qty": int(qty),
            }])],
            ignore_index=True,
        )
        st.session_state[seed_key] = current
        st.session_state[rev_key] = st.session_state.get(rev_key, 0) + 1
        st.success(f"Added {fg_choice} × {int(qty)} to manual entries.")
        st.rerun()

    with st.sidebar.expander("📚 Browse catalog & add SKU", expanded=False):
        direction = st.radio(
            "Start from",
            options=["FG SKU first", "Accessory SKU first"],
            horizontal=True,
            help=(
                "**FG SKU first** — pick a finished good, then match accessory.  \n"
                "**Accessory SKU first** — pick an accessory, then match a finished good."
            ),
            key=f"browse_direction_{location}",
        )

        if direction == "FG SKU first":
            _render_fg_first(work, acc_df, location, _fg_label_for_row, _acc_label, _commit_add)
        else:
            _render_acc_first(work, acc_df, location, _fg_label_for_row, _acc_label, _commit_add)


def _render_fg_first(work, acc_df, location, fg_label_for_row, acc_label, commit_add):
    """FG-first browse flow: Family → Class → FG SKU → Accessory → Qty."""
    family_counts = work["__family"].value_counts()
    ordered_families = [f for f in KNOWN_FAMILIES if f in family_counts.index]
    if "OTHER" in family_counts.index:
        ordered_families.append("OTHER")
    family_choice = st.selectbox(
        "Family",
        options=ordered_families,
        format_func=lambda f: f"{f} ({family_counts.get(f, 0)})",
        key=f"browse_family_{location}",
    )

    in_family = work[work["__family"] == family_choice]
    class_counts = in_family["__class"].value_counts()
    class_order_pref = ["Standard Trailer", "Hybrid", "Power Module",
                        "Head Trailer", "Standard", "⚠ Placeholder"]
    ordered_classes = [c for c in class_order_pref if c in class_counts.index]
    for c in class_counts.index:
        if c not in ordered_classes:
            ordered_classes.append(c)
    class_choice = st.selectbox(
        "Class",
        options=["All classes"] + ordered_classes,
        key=f"browse_class_{location}",
    )

    if class_choice == "All classes":
        sku_rows = in_family
    else:
        sku_rows = in_family[in_family["__class"] == class_choice]
    sku_rows = sku_rows.sort_values(by="SKU")
    sku_options = sku_rows["SKU"].astype(str).tolist()
    if not sku_options:
        st.info("No SKUs match this family + class combination.")
        return

    fg_choice = st.selectbox(
        "FG SKU",
        options=sku_options,
        format_func=lambda s: fg_label_for_row(sku_rows[sku_rows["SKU"] == s].iloc[0]),
        key=f"browse_fg_{location}",
    )

    acc_options = ["(none)"]
    if acc_df is not None and not acc_df.empty:
        acc_work = acc_df.reset_index(drop=True).copy()
        acc_skus = acc_work["SKU"].astype(str)
        acc_mask = acc_skus.apply(_accessory_family_hint).str.upper() == family_choice.upper()
        acc_subset = acc_work[acc_mask].sort_values(by="SKU")
        acc_options.extend(acc_subset["SKU"].astype(str).tolist())

    acc_choice = st.selectbox(
        "Accessory",
        options=acc_options,
        format_func=acc_label,
        key=f"browse_acc_{location}",
    )
    qty = st.number_input(
        "Qty", min_value=1, max_value=999, value=1, step=1,
        key=f"browse_qty_{location}",
    )
    if st.button("➕ Add to manual entries", use_container_width=True,
                 key=f"browse_add_{location}"):
        commit_add(fg_choice, acc_choice, qty)


def _render_acc_first(work, acc_df, location, fg_label_for_row, acc_label, commit_add):
    """Accessory-first browse flow: Family → Accessory SKU → matching FG SKUs → Qty.

    Useful when the planner knows which accessory kit a customer ordered and
    wants the system to suggest a compatible finished-good unit.
    """
    if acc_df is None or acc_df.empty:
        st.info("No accessory catalog loaded.")
        return

    acc_work = acc_df.reset_index(drop=True).copy()
    acc_work["__family"] = acc_work["SKU"].astype(str).apply(_accessory_family_hint).str.upper()

    # Family selector — uses accessory-side family counts
    fam_counts = acc_work["__family"].value_counts()
    ordered_families = [f for f in KNOWN_FAMILIES if f.upper() in fam_counts.index]
    if not ordered_families:
        st.info("No accessories grouped by known families.")
        return
    family_choice = st.selectbox(
        "Family (accessories)",
        options=ordered_families,
        format_func=lambda f: f"{f} ({int(fam_counts.get(f.upper(), 0))})",
        key=f"browse_acc_family_{location}",
    )

    # Accessory SKU dropdown (filtered to chosen family)
    family_up = family_choice.upper()
    in_family_acc = acc_work[acc_work["__family"] == family_up].sort_values(by="SKU")
    acc_options = in_family_acc["SKU"].astype(str).tolist()
    if not acc_options:
        st.info("No accessories in this family.")
        return
    acc_choice = st.selectbox(
        "Accessory SKU",
        options=acc_options,
        format_func=acc_label,
        key=f"browse_acc_pick_{location}",
    )

    # Show description hint for the chosen accessory
    chosen_row = in_family_acc[in_family_acc["SKU"] == acc_choice]
    if not chosen_row.empty:
        desc = str(chosen_row.iloc[0].get("Description", "") or "").strip()
        if desc:
            st.caption(f"📋 {desc}")

    # Suggested FG SKUs — machines in the SAME family as the accessory
    matching_fg = work[work["__family"] == family_choice]
    # Allow optional class filter for the FG suggestion too
    fg_class_counts = matching_fg["__class"].value_counts()
    class_order_pref = ["Standard Trailer", "Hybrid", "Power Module",
                        "Head Trailer", "Standard", "⚠ Placeholder"]
    ordered_classes = [c for c in class_order_pref if c in fg_class_counts.index]
    for c in fg_class_counts.index:
        if c not in ordered_classes:
            ordered_classes.append(c)
    class_choice = st.selectbox(
        "FG class filter",
        options=["All classes"] + ordered_classes,
        key=f"browse_acc_fgclass_{location}",
    )
    if class_choice != "All classes":
        matching_fg = matching_fg[matching_fg["__class"] == class_choice]
    matching_fg = matching_fg.sort_values(by="SKU")
    fg_options = matching_fg["SKU"].astype(str).tolist()

    if not fg_options:
        st.warning(
            f"No machine SKUs match the **{family_choice}** family for accessory "
            f"`{acc_choice}`. Add the accessory by itself or pick a different "
            "accessory family."
        )
        return

    fg_choice = st.selectbox(
        "Suggested FG SKU",
        options=fg_options,
        format_func=lambda s: fg_label_for_row(matching_fg[matching_fg["SKU"] == s].iloc[0]),
        key=f"browse_acc_fg_{location}",
    )

    qty = st.number_input(
        "Qty", min_value=1, max_value=999, value=1, step=1,
        key=f"browse_acc_qty_{location}",
    )
    if st.button("➕ Add to manual entries", use_container_width=True,
                 key=f"browse_acc_add_{location}"):
        commit_add(fg_choice, acc_choice, qty)


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
            "Enter FG SKU, Accessory SKU (optional), and Quantity for each row. "
            "Use **Browse catalog** below to pick from the known SKUs."
        )

        # Cached loaders — same path as main(), no extra I/O
        machine_df = _load_machine_df(_csv_mtime("machine_clean.csv"))
        acc_df = _load_acc_df(_csv_mtime("acc_clean.csv"))

        # Per-location state keys: a seed DataFrame and a revision counter
        # that we bump every time we programmatically inject a row.
        seed_key = f"manual_seed_{location}"
        rev_key = f"manual_rev_{location}"

        default_entries = pd.DataFrame({
            "FG SKU": [""] * 8,
            "Accessory SKU": [""] * 8,
            "Qty": [0] * 8,
        })
        st.session_state.setdefault(seed_key, default_entries)
        st.session_state.setdefault(rev_key, 0)

        # Render the Browse panel (it may bump rev_key + rerun on Add)
        _render_sku_browser(machine_df, acc_df, location, seed_key, rev_key)
        _render_scenarios_panel(location, seed_key, rev_key)

        # Render the data_editor with a rev-suffixed key so a fresh seed
        # is picked up after each programmatic add.
        manual_entries = st.sidebar.data_editor(
            st.session_state[seed_key],
            use_container_width=True,
            num_rows="dynamic",
            key=f"manual_entries_{location}_v{st.session_state[rev_key]}",
            column_config={
                "FG SKU": st.column_config.TextColumn("FG SKU", help="e.g. BOSS25-006"),
                "Accessory SKU": st.column_config.TextColumn(
                    "Accessory SKU", help="e.g. BOSS25-A016 (optional)"
                ),
                "Qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            },
        )

        # Persist the latest in-flight edits so the next Add appends on top
        st.session_state[seed_key] = manual_entries

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
### Time units
- **Person-minutes (p-min)** — labor *effort*. 1 person working 1 min = 1 p-min. 2 people working 30 min = 60 p-min. Independent of how many people you assign.
- **Calendar minutes (cal-min)** — wall-clock *elapsed* time. With 1 person doing 60 p-min, cycle time is 60 cal-min. With 2 people working in parallel, the cycle is 30 cal-min.
- **Cycle time** — calendar minutes a unit physically spends at a station = `p-min ÷ Crew per unit`.
- **Lead time (days)** — total calendar days for **one** person to build the whole unit alone = `Total p-min ÷ (shift × efficiency)`.

### Crew terms
- **Headcount (HC) / People** — number of people assigned to a station for the whole shift.
- **Stations/Cells (Conc)** — how many units can be worked on at the same time at one station (parallel bays/cells).
- **Crew per unit** — number of people working together on one unit at a station. Drives cycle time.
- **Required HC** — number of people needed to meet the schedule given safety + efficiency.

### Capacity factors
- **Safety factor** — planning buffer applied to capacity. `0.85` = leave 15% slack.
- **Efficiency factor** — fraction of the shift that is actually productive. `1.00` = no loss; `0.625` ≈ VSM standard.
- **Effective capacity** = `HC × shift × days × efficiency × safety` person-minutes/period.
- **Utilization %** — `demand ÷ effective capacity`. >100% means over capacity.

### Unit classes
- **STD** — Standard trailer (full assembly with marry)
- **HS** — Head Skid only (no trailer)
- **HT** — Head + Trailer (mount only, no marry)

### Catalog terms
- **FG SKU** — finished-good SKU from `machine_clean.csv` (e.g. `BOSS25-006`).
- **Accessory SKU** — accessory kit from `acc_clean.csv` (e.g. `BOSS25-A016`).
- **Bat** — battery count for that FG SKU. PDS/SDG = 0.
- **Last Modified** — date the row was last edited through the app (blank = never).

### Stations (key → display name)
- `Warehouse` = Warehouse (Pick) — pull parts
- `Wire` = Wire Assembly — pre-wiring sub-assembly
- `Battery` = Battery Assembly — battery cells (BOSS only)
- `PMAcc` = PM Acc (Headunit) — PM/Head-unit accessories
- `GenAcc` = Gen Accessories — generator-side accessories
- `ComAcc` = Com Accessories — compressor-side accessories
- `Trailer` = Trailer Assembly — trailer sub-assembly (STD/HT only)
- `AccKIT` = Accessories KIT — kit prep
- `ETO` = Engineering To Order — special engineering (BOSS220/BOSS400)
- `Final` = Final Assembly — mount + marry (where used)
- `PDI / QC / Ship` — Pre-Delivery Inspection · Quality Check · Shipping

### Placeholders & estimates
- **`XXX` in SKU** — estimation placeholder. Labor is an estimate, not measured. E.g. `BOSS220PM XXX`, `BOSS25 AXXX`.
- **`-A999`** — legacy family-level accessory placeholder.

### Status colors
🟢 OK · 🟡 Tight · 🟠 Near capacity · 🔴 Over capacity · ⚪ Not at this facility · 🆕 Recently added

### GitHub Save (for admins)
Most of the **💾 Save** buttons push CSVs/JSON back to the project's GitHub repo so changes apply to everyone after the next ~1 min Streamlit Cloud redeploy. This requires a **GitHub Personal Access Token** with the `repo` scope, stored in **Streamlit Cloud → Settings → Secrets** as:
```toml
github_token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxx"
```
If you see "GitHub token not configured" when saving, ask the app's admin to add the token. Read-only browsing works without it.

### Tip
Hover any column header in a table to see its specific tooltip.
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


def tab_capacity_vs_demand(capacity, inputs, batt_sku=None, batt_type=None):
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
        text=[f"{v*100:.0f}%" for v in chart_data["thru_util_safe"]],
        textposition="outside",
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

    # ----------------------------------------------------------------
    # 🔋 Battery throughput section (was a top-level tab before)
    # ----------------------------------------------------------------
    if batt_sku is not None and batt_type is not None:
        total_batt = int(batt_sku["total_batteries"].sum()) if not batt_sku.empty else 0
        if total_batt == 0:
            st.markdown("---")
            st.markdown("#### 🔋 Battery throughput")
            st.info(
                f"**No batteries needed for this plan at {inputs.get('location', '')}.** "
                "(Either the schedule has no BOSS units, or this facility doesn't build batteries.)"
            )
        else:
            st.markdown("---")
            with st.expander("🔋 Battery throughput detail", expanded=False):
                tab_battery_throughput(batt_sku, batt_type, capacity, inputs)


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


def tab_data_setup(machine_df, acc_df, schedule_df, item_master_df, item_packages_df):
    """Consolidated data + admin tab.

    Replaces three previous top-level tabs (Update Labor, Data Quality, Source
    Data) with a single tab containing radio sub-views. Sub-views in order:

      📋 Schedule              — read-only schedule view
      🗂 Machine catalog       — editable catalog + add-new-SKU form
      🗂 Accessory catalog     — editable catalog + add-new + XXX placeholder forms
      🔩 Items                 — unified items table editor
      📦 Item packages         — package definitions editor
      🔬 Reconciliation & Apply — items-vs-aggregate diff + write-back
      🔍 Data Quality          — validation report
    """
    st.header("📁 Data & Setup")
    st.caption(
        "All data inputs in one place. Switch views below to browse the schedule, "
        "edit the catalogs, manage items / packages, reconcile, or run data quality checks."
    )

    sub = st.radio(
        "View",
        [
            "📋 Schedule",
            "🗂 Machine catalog",
            "🗂 Accessory catalog",
            "🔩 Items",
            "📦 Item packages",
            "🔬 Reconciliation & Apply",
            "🔍 Data Quality",
        ],
        horizontal=True,
        label_visibility="collapsed",
    )

    # Used-in-schedule maps (shared by some sub-views)
    used_fg = schedule_df.groupby("FG_BASE")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}
    used_acc = schedule_df[schedule_df["ACC"] != ""].groupby("ACC")["BUILD QTY"].sum().to_dict() \
        if not schedule_df.empty else {}

    if sub == "📋 Schedule":
        st.subheader("📋 Schedule")
        st.caption("Read-only view of the schedule currently driving the analysis tabs.")
        if schedule_df.empty:
            st.info("No schedule rows. Upload a CSV in the sidebar or switch to manual entry.")
        else:
            st.dataframe(schedule_df, use_container_width=True, height=600)

    elif sub == "🗂 Machine catalog":
        tab_floor_verification_machine(machine_df, schedule_df, used_fg)

    elif sub == "🗂 Accessory catalog":
        tab_floor_verification_accessory(acc_df, schedule_df, used_acc, machine_df)

    elif sub == "🔩 Items":
        _render_item_master_view(item_master_df)

    elif sub == "📦 Item packages":
        _render_item_packages_view(item_packages_df, item_master_df)

    elif sub == "🔬 Reconciliation & Apply":
        _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc)

    elif sub == "🔍 Data Quality":
        tab_data_validation(machine_df, acc_df)


def tab_floor_verification_machine(machine_df, schedule_df, used_fg):
    """The Machine-catalog editor — extracted from the old tab_floor_verification
    so it can be embedded as a sub-view of Data & Setup."""
    st.subheader("🗂 Machine catalog")
    _render_stale_data_banner("data/machine_clean.csv")
    st.caption(
        "Edit labor times in the table below. SKUs used in the current schedule "
        "are highlighted. Click **💾 Save** to push changes to GitHub — all users "
        "see new values after redeploy (~1 min)."
    )

    editable_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy", "PDI", "QC", "Ship", "Bat"]
    display_df = machine_df.reset_index(drop=True).copy()
    # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
    if "FN_Assy_old" in display_df.columns and "FN_Assy" not in display_df.columns:
        display_df = display_df.rename(columns={"FN_Assy_old": "FN_Assy"})
    if "Last Modified" not in display_df.columns:
        display_df["Last Modified"] = ""
    display_df.insert(0, "Used (qty)", display_df["SKU"].map(lambda s: used_fg.get(s, 0)))
    display_df.insert(1, "In schedule", display_df["Used (qty)"] > 0)

    only_used = st.checkbox(
        "Show only SKUs used in current schedule", value=False, key="fv_m_only_used"
    )
    if only_used:
        display_df = display_df[display_df["In schedule"]].copy()
    display_df = display_df.sort_values(
        by=["In schedule", "Used (qty)"], ascending=[False, False]
    ).reset_index(drop=True)

    col_cfg = {
        "SKU": st.column_config.TextColumn("SKU", disabled=True),
        "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
        "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
        "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
        "Last Modified": st.column_config.TextColumn(
            "Last Modified", disabled=True,
            help="Date this row was last updated through the app. Blank = never edited.",
        ),
    }
    for c in editable_cols:
        col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

    edited = st.data_editor(
        display_df,
        use_container_width=True, num_rows="fixed",
        key="fv_machine_editor", height=600,
        column_config=col_cfg, hide_index=True,
    )

    _render_pending_changes(edited, machine_df, editable_cols)

    if st.button("💾 Save updated Machine catalog to GitHub", use_container_width=True):
        _save_catalog_csv(
            edited=edited, source_df=machine_df,
            editable_cols=editable_cols,
            file_path="data/machine_clean.csv", label="machine",
        )

    _render_add_new_sku(
        source_df=machine_df, editable_cols=editable_cols,
        file_path="data/machine_clean.csv", label="machine",
    )


def tab_floor_verification_accessory(acc_df, schedule_df, used_acc, machine_df):
    """The Accessory-catalog editor — extracted from tab_floor_verification."""
    st.subheader("🗂 Accessory catalog")
    _render_stale_data_banner("data/acc_clean.csv")
    st.caption(
        "Edit labor times in the table below. SKUs used in the current schedule "
        "are highlighted. Click **💾 Save** to push changes to GitHub."
    )

    editable_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                     "PMAcc", "GenAcc", "ComAcc"]
    display_df = acc_df.reset_index(drop=True).copy()
    # Backward-compat: alias "Compressor" → "ComAcc" if cache is stale
    if "Compressor" in display_df.columns and "ComAcc" not in display_df.columns:
        display_df = display_df.rename(columns={"Compressor": "ComAcc"})
    if "Last Modified" not in display_df.columns:
        display_df["Last Modified"] = ""
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

    # Live computed columns: Bat from machine catalog + Total per unit
    bat_counts = display_df["SKU"].astype(str).apply(
        lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
    )
    non_batt_cols = [c for c in editable_cols if c != "BattSubRaw"]
    base = display_df[non_batt_cols].fillna(0).sum(axis=1)
    per_batt = display_df["BattSubRaw"].fillna(0) if "BattSubRaw" in display_df.columns else 0
    display_df["Bat"] = bat_counts.astype(int)
    display_df["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)

    col_cfg = {
        "SKU": st.column_config.TextColumn("SKU", disabled=True),
        "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
        "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
        "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
        "Bat": st.column_config.NumberColumn(
            "Bat", disabled=True,
            help="Battery count for this accessory's FG family — from machine_clean.csv.",
        ),
        "Total per unit (p-min)": st.column_config.NumberColumn(
            "Total per unit (p-min)", disabled=True,
            help="Non-battery labor + BattSubRaw × Bat.",
        ),
        "Last Modified": st.column_config.TextColumn(
            "Last Modified", disabled=True,
            help="Date this row was last updated through the app. Blank = never edited.",
        ),
    }
    for c in editable_cols:
        col_cfg[c] = st.column_config.NumberColumn(c, min_value=0, step=1)

    edited = st.data_editor(
        display_df,
        use_container_width=True, num_rows="fixed",
        key="fv_acc_editor", height=600,
        column_config=col_cfg, hide_index=True,
    )
    st.caption(
        "💡 **Total per unit** = non-battery labor + `BattSubRaw × Bat`. "
        "`Bat` is the exact count from the machine catalog (max within the family)."
    )

    _render_pending_changes(edited, acc_df, editable_cols)

    if st.button("💾 Save updated Accessory catalog to GitHub", use_container_width=True):
        _save_catalog_csv(
            edited=edited, source_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv", label="accessory",
        )

    _render_add_new_sku(
        source_df=acc_df, editable_cols=editable_cols,
        file_path="data/acc_clean.csv", label="accessory",
    )

    # XXX-style placeholder helper (replaces the old A999 family placeholder)
    _render_add_xxx_placeholder(
        acc_df=acc_df, editable_cols=editable_cols,
        file_path="data/acc_clean.csv",
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

    # Stale-data banner — polls GitHub for the latest commit SHA of the chosen
    # catalog and warns the user if someone else committed after they loaded.
    active_file = "data/machine_clean.csv" if catalog == "Machine (FG SKU)" else "data/acc_clean.csv"
    _render_stale_data_banner(active_file)

    if catalog == "Machine (FG SKU)":
        # Machine labor columns the user can edit
        editable_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy", "PDI", "QC", "Ship", "Bat"]
        display_df = machine_df.reset_index(drop=True).copy()
        # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
        if "FN_Assy_old" in display_df.columns and "FN_Assy" not in display_df.columns:
            display_df = display_df.rename(columns={"FN_Assy_old": "FN_Assy"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in display_df.columns:
            display_df["Last Modified"] = ""
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

        # Column config: read-only for SKU/Description/Last Modified, editable for labor
        col_cfg = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
            "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
            "Last Modified": st.column_config.TextColumn(
                "Last Modified", disabled=True,
                help="Date this row was last updated through the app. Blank = never edited.",
            ),
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

        # Pending-changes preview so the user can verify their edits before saving
        _render_pending_changes(edited, machine_df, editable_cols)

        # Save button
        if st.button("💾 Save updated Machine catalog to GitHub", use_container_width=True):
            _save_catalog_csv(
                edited=edited,
                source_df=machine_df,
                editable_cols=editable_cols,
                file_path="data/machine_clean.csv",
                label="machine",
            )

        # New-SKU form below the editor
        _render_add_new_sku(
            source_df=machine_df,
            editable_cols=editable_cols,
            file_path="data/machine_clean.csv",
            label="machine",
        )

    else:  # Accessory
        editable_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                         "PMAcc", "GenAcc", "ComAcc"]
        display_df = acc_df.reset_index(drop=True).copy()
        # Backward-compat: alias "Compressor" → "ComAcc" if cache is stale
        if "Compressor" in display_df.columns and "ComAcc" not in display_df.columns:
            display_df = display_df.rename(columns={"Compressor": "ComAcc"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in display_df.columns:
            display_df["Last Modified"] = ""
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

        # Live totals — non-battery labor + BattSubRaw × N for N = 1, 3, 5
        # Per-accessory battery count comes from the machine catalog (max Bat for
        # the family). PDS / SDG accessories yield 0 (no battery multiplier).
        bat_counts = display_df["SKU"].astype(str).apply(
            lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
        )
        non_batt_cols = [c for c in editable_cols if c != "BattSubRaw"]
        base = display_df[non_batt_cols].fillna(0).sum(axis=1)
        per_batt = display_df["BattSubRaw"].fillna(0)
        display_df["Bat"] = bat_counts.astype(int)
        display_df["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)

        col_cfg = {
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True, width="large"),
            "Used (qty)": st.column_config.NumberColumn("Used (qty)", disabled=True),
            "In schedule": st.column_config.CheckboxColumn("In schedule", disabled=True),
            "Bat": st.column_config.NumberColumn(
                "Bat", disabled=True,
                help="Battery count for this accessory's FG family — sourced from machine_clean.csv.",
            ),
            "Total per unit (p-min)": st.column_config.NumberColumn(
                "Total per unit (p-min)", disabled=True,
                help="Non-battery labor + BattSubRaw × Bat. Bat comes from the machine catalog for the accessory's family.",
            ),
            "Last Modified": st.column_config.TextColumn(
                "Last Modified", disabled=True,
                help="Date this row was last updated through the app. Blank = never edited.",
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
            "💡 **Total per unit** = non-battery labor + `BattSubRaw × Bat`, where "
            "`Bat` is the exact count from the machine catalog for each accessory's "
            "FG family. Family-mixed counts use the maximum (e.g. BOSS220 → 3)."
        )

        # Pending-changes preview so the user can verify their edits before saving
        _render_pending_changes(edited, acc_df, editable_cols)

        if st.button("💾 Save updated Accessory catalog to GitHub", use_container_width=True):
            _save_catalog_csv(
                edited=edited,
                source_df=acc_df,
                editable_cols=editable_cols,
                file_path="data/acc_clean.csv",
                label="accessory",
            )

        # New-SKU form below the editor
        _render_add_new_sku(
            source_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv",
            label="accessory",
        )

        # Family-level placeholder helper (e.g. BOSS25-A999)
        _render_add_xxx_placeholder(
            acc_df=acc_df,
            editable_cols=editable_cols,
            file_path="data/acc_clean.csv",
        )


def _render_pending_changes(edited, source_df, editable_cols):
    """Show a small expander listing cells where the edited value differs from
    the source. Helps the user confirm their edits are captured before clicking
    Save (and helps debug 'nothing happens' cases)."""
    if edited is None or len(edited) == 0:
        return
    if "SKU" not in edited.columns:
        return
    try:
        ei = edited.set_index("SKU")
    except Exception:
        return

    diffs = []
    for sku in ei.index:
        if sku not in source_df.index:
            continue
        for col in editable_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.at[sku, col]
            old_val = source_df.at[sku, col]
            try:
                if pd.notna(new_val) and float(new_val) != float(old_val):
                    diffs.append({
                        "SKU": sku, "Column": col,
                        "Old": float(old_val), "New": float(new_val),
                        "Δ": float(new_val) - float(old_val),
                    })
            except (TypeError, ValueError):
                continue

    if not diffs:
        st.caption("ℹ️ No pending edits — make a change in the table above before saving.")
        return

    n = len(diffs)
    with st.expander(f"📝 {n} pending change(s) — preview before saving", expanded=False):
        st.dataframe(pd.DataFrame(diffs), use_container_width=True, hide_index=True)


def _render_add_xxx_placeholder(acc_df, editable_cols, file_path):
    """Quick form to add an estimation placeholder accessory (e.g. `BOSS25 AXXX`).

    Mirrors the machine-catalog `XXX` convention: pick a family, generate
    `{family} AXXX` (with space), and save with default labor times you can
    adjust. Existing examples already in the catalog include `PDS100 AXXX`,
    `BOSS25PM AXXX`, `BOSS220PM AXXX`, etc.

    Note: the model does NOT auto-fall-back to this placeholder when an
    accessory is missing. Some real orders have no accessory component; a
    silent fallback would inflate labor. The placeholder is a regular row
    that the user can OPT to use by typing its SKU in the manual entry. The
    Data Quality view automatically flags any `XXX` SKU as `⚠ Estimation placeholder`.
    """
    with st.expander("➕ Add placeholder accessory (XXX)", expanded=False):
        st.caption(
            "Creates a `{family} AXXX` placeholder accessory row, matching the "
            "machine-catalog XXX naming convention. This is **not** used as an "
            "automatic fallback — it's a conventionally-named accessory you can "
            "reference explicitly."
        )

        family = st.selectbox(
            "FG family",
            options=KNOWN_FAMILIES,
            help="The placeholder SKU will be `{family} AXXX`.",
            key="placeholder_family",
        )
        new_sku = f"{family} AXXX"
        st.text(f"SKU → {new_sku}")

        # Defaults — sensible numbers for the typical family placeholder
        default_labor = {
            "Warehouse": 10,
            "AccKIT": 10,
            "Nameplate Prep": 10,
            "BattSubRaw": 320,
            "PMAcc": 60,
            "GenAcc": 60,
            "ComAcc": 0,
        }

        with st.form("add_xxx_placeholder", clear_on_submit=True):
            new_desc = st.text_input(
                "Description",
                value=f"{family} ESTIMATION ACCESSORY PACKAGE",
                key="placeholder_desc",
            )

            cols_per_row = 4
            new_values = {}
            for i in range(0, len(editable_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, c in enumerate(editable_cols[i:i + cols_per_row]):
                    new_values[c] = row_cols[j].number_input(
                        c, min_value=0, step=1,
                        value=int(default_labor.get(c, 0)),
                        key=f"placeholder_num_{c}",
                    )

            submitted = st.form_submit_button(
                f"💾 Add `{new_sku}` & save to GitHub",
                use_container_width=True,
            )

        if not submitted:
            return

        # Validate
        existing = {str(s).upper() for s in acc_df["SKU"].astype(str)}
        if new_sku.upper() in existing:
            st.error(
                f"`{new_sku}` already exists in the accessory catalog. "
                "Edit the existing row in the table above instead."
            )
            return

        token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
        if not token:
            st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            return

        today = pd.Timestamp.today().strftime("%Y-%m-%d")
        new_sku_upper = new_sku.upper()
        response = None
        for attempt in (1, 2):
            try:
                with st.spinner(
                    f"Adding {new_sku} to {file_path}"
                    + (" — retrying" if attempt == 2 else "") + "..."
                ):
                    fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                    if fresh_df.empty:
                        st.error("Could not fetch the current catalog from GitHub.")
                        return

                    fresh_skus_upper = set(
                        fresh_df["SKU"].astype(str).str.strip().str.upper()
                    ) if "SKU" in fresh_df.columns else set()
                    if new_sku_upper in fresh_skus_upper:
                        st.error(
                            f"`{new_sku}` was just added by another user. Refresh."
                        )
                        return

                    if "Last Modified" not in fresh_df.columns:
                        fresh_df["Last Modified"] = ""

                    new_row = {col: "" for col in fresh_df.columns}
                    new_row["SKU"] = new_sku
                    if "Description" in new_row:
                        new_row["Description"] = new_desc
                    for c, v in new_values.items():
                        if c in new_row:
                            new_row[c] = int(v)
                    new_row["Last Modified"] = today

                    merged = pd.concat(
                        [fresh_df, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    csv_text = merged.to_csv(index=False)

                    response = save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Add XXX placeholder accessory '{new_sku}' via app",
                        sha=fresh_sha,
                    )
                break
            except GitHubConflict:
                if attempt == 2:
                    st.error("❌ Save failed: concurrent commit. Please refresh and retry.")
                    return
                continue
            except Exception as e:
                st.error(f"❌ Save failed: {e}")
                return

        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.success(
            f"✅ Added XXX placeholder accessory `{new_sku}` "
            + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
            + "  \nIt will appear in the catalog after the app redeploys (~1 min)."
        )


def _render_add_new_sku(source_df, editable_cols, file_path, label, extra_text_cols=None):
    """Form to add a brand-new SKU to a catalog and immediately push to GitHub.

    Args:
      source_df:     The loaded catalog DataFrame (machine_df or acc_df).
      editable_cols: Labor columns the user can fill in (numbers).
      file_path:     Path in the repo (e.g. "data/machine_clean.csv").
      label:         Display label for messages ("machine" or "accessory").
      extra_text_cols: Optional extra text columns to capture (defaults to none).
    """
    extra_text_cols = extra_text_cols or []
    with st.expander(f"➕ Add a new {label} SKU", expanded=False):
        st.caption(
            f"Use this to add a brand-new {label} SKU that isn't in the catalog "
            "yet. The new row is committed to GitHub immediately along with today's "
            "date in **Last Modified**."
        )
        with st.form(f"add_new_{label}", clear_on_submit=True):
            new_sku = st.text_input(
                "SKU", help="Must be unique — case-insensitive check against the catalog.",
                key=f"new_sku_{label}",
            ).strip()
            new_desc = st.text_input(
                "Description", help="Short, descriptive text shown in catalog views.",
                key=f"new_desc_{label}",
            ).strip()

            extra_text_values = {}
            for c in extra_text_cols:
                extra_text_values[c] = st.text_input(
                    c, key=f"new_text_{label}_{c}",
                ).strip()

            cols_per_row = 4
            new_values = {}
            for i in range(0, len(editable_cols), cols_per_row):
                row_cols = st.columns(cols_per_row)
                for j, c in enumerate(editable_cols[i:i + cols_per_row]):
                    new_values[c] = row_cols[j].number_input(
                        c, min_value=0, step=1, value=0,
                        key=f"new_num_{label}_{c}",
                    )

            submitted = st.form_submit_button(
                f"💾 Add {label} SKU and save to GitHub", use_container_width=True,
            )

        if not submitted:
            return

        if not new_sku:
            st.error("SKU cannot be empty.")
            return
        existing = {str(s).upper() for s in source_df["SKU"].astype(str)}
        if new_sku.upper() in existing:
            st.error(f"`{new_sku}` is already in the {label} catalog. "
                     "Edit the existing row in the table above instead.")
            return

        token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
        if not token:
            st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
            return

        today = pd.Timestamp.today().strftime("%Y-%m-%d")
        new_sku_upper = new_sku.upper()

        # Fetch + append + push, with one retry on SHA conflict.
        response = None
        for attempt in (1, 2):
            try:
                with st.spinner(
                    f"Adding {new_sku} to {file_path}"
                    + (" — retrying" if attempt == 2 else "") + "..."
                ):
                    fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                    if fresh_df.empty:
                        st.error("Could not fetch the current catalog from GitHub.")
                        return

                    # Concurrent-safety check: did someone else just add this SKU?
                    fresh_skus_upper = set(
                        fresh_df["SKU"].astype(str).str.strip().str.upper()
                    ) if "SKU" in fresh_df.columns else set()
                    if new_sku_upper in fresh_skus_upper:
                        st.error(
                            f"`{new_sku}` was just added by another user. "
                            "Refresh the page to see it, then edit the existing row instead."
                        )
                        return

                    if "Last Modified" not in fresh_df.columns:
                        fresh_df["Last Modified"] = ""

                    # Build the new row matching fresh_df's schema
                    new_row = {col: "" for col in fresh_df.columns}
                    new_row["SKU"] = new_sku
                    if "Description" in new_row:
                        new_row["Description"] = new_desc
                    for c, v in extra_text_values.items():
                        if c in new_row:
                            new_row[c] = v
                    for c, v in new_values.items():
                        if c in new_row:
                            new_row[c] = int(v)
                    new_row["Last Modified"] = today

                    merged = pd.concat(
                        [fresh_df, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    csv_text = merged.to_csv(index=False)

                    response = save_catalog_to_github(
                        csv_text, file_path, token,
                        message=f"Add new {label} SKU {new_sku} via app",
                        sha=fresh_sha,
                    )
                break  # success
            except GitHubConflict:
                if attempt == 2:
                    st.error(
                        "❌ Save failed: another user committed concurrently. "
                        "Please refresh the page and try again."
                    )
                    return
                continue
            except Exception as e:
                st.error(f"❌ Save failed: {e}")
                return

        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.success(
            f"✅ Added `{new_sku}` to `{file_path}` "
            + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
            + "  \nIt will appear in the catalog after the app redeploys (~1 min)."
        )


@st.cache_data(ttl=30, show_spinner=False)
def _latest_sha_cached(file_path: str, token: str) -> "str | None":
    """Cache the GitHub SHA lookup for 30 seconds to avoid hammering the API."""
    return latest_catalog_sha(file_path, token)


def _render_stale_data_banner(file_path: str) -> None:
    """Show a yellow banner if another user committed to `file_path` after
    we recorded our 'loaded' SHA. The user can click 🔄 to refresh."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        return  # No token → can't poll; silently skip the banner

    state_key = f"loaded_sha::{file_path}"
    # On the first render for this file in this session, record the current SHA
    # as "what the user loaded" — anything newer is stale relative to them.
    if state_key not in st.session_state:
        st.session_state[state_key] = _latest_sha_cached(file_path, token)
        return

    loaded_sha = st.session_state[state_key]
    latest = _latest_sha_cached(file_path, token)
    if latest is None or loaded_sha is None:
        return  # File missing or transient API error — silently skip
    if latest != loaded_sha:
        c1, c2 = st.columns([4, 1])
        with c1:
            st.warning(
                f"⚠️ **`{file_path}` was just updated by another user.** "
                "Your view is stale — refresh before saving so you don't lose their changes."
            )
        with c2:
            if st.button("🔄 Refresh", key=f"stale_refresh_{file_path}"):
                try:
                    st.cache_data.clear()
                except Exception:
                    pass
                st.session_state[state_key] = latest
                st.rerun()


def _compute_cell_diffs(edited, source_df, editable_cols):
    """Return a list of (sku, col, new_value) tuples for cells the user changed.

    Compares `edited` (user's data_editor output) against `source_df` (the
    in-memory catalog loaded at session start).
    """
    # Drop UI-only / derived columns first. We exclude any name that is also in
    # `editable_cols` so we don't accidentally strip a real editable column
    # (e.g. "Bat" is a derived column in the accessory editor but a real
    # editable column in the machine editor).
    candidate_drops = [
        "Bat", "Total per unit (p-min)",
        "Total (1 batt)", "Total (3 batt)", "Total (5 batt)",
        "Total labor (p-min)",
        "Used (qty)", "In schedule",
    ]
    drop_cols = [c for c in candidate_drops
                 if c in edited.columns and c not in editable_cols]
    work = edited.drop(columns=drop_cols)
    if "SKU" not in work.columns:
        return []
    ei = work.set_index("SKU")

    diffs = []
    for sku in ei.index:
        if sku not in source_df.index:
            continue
        for col in editable_cols:
            if col not in ei.columns or col not in source_df.columns:
                continue
            new_val = ei.loc[sku, col]
            old_val = source_df.loc[sku, col]
            try:
                if pd.notna(new_val) and float(new_val) != float(old_val):
                    diffs.append((sku, col, new_val))
            except (TypeError, ValueError):
                continue
    return diffs


def _apply_diffs_and_serialize(fresh_df, diffs, editable_cols):
    """Apply the cell-level diffs on top of `fresh_df` (from GitHub) and return CSV text.

    Stamps Last Modified for any row that had a change.
    """
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    if "Last Modified" not in fresh_df.columns:
        fresh_df["Last Modified"] = ""

    # Build an index lookup. The CSV from GitHub doesn't have SKU as index;
    # we need to find each SKU by its column value.
    if "SKU" not in fresh_df.columns:
        raise RuntimeError("Catalog CSV is missing the SKU column.")
    fresh_df = fresh_df.copy()
    fresh_df["__sku_norm"] = fresh_df["SKU"].astype(str).str.strip()

    changed_skus = set()
    for sku, col, new_val in diffs:
        mask = fresh_df["__sku_norm"] == str(sku).strip()
        if not mask.any():
            continue
        # Ensure the column exists in the fresh file (e.g. column renames)
        if col not in fresh_df.columns:
            fresh_df[col] = 0
        idx = fresh_df.index[mask][0]
        fresh_df.at[idx, col] = new_val
        changed_skus.add(sku)

    for sku in changed_skus:
        mask = fresh_df["__sku_norm"] == str(sku).strip()
        if mask.any():
            idx = fresh_df.index[mask][0]
            fresh_df.at[idx, "Last Modified"] = today

    fresh_df = fresh_df.drop(columns=["__sku_norm"])
    return fresh_df.to_csv(index=False), len(changed_skus)


def _read_fresh_csv(file_path: str, token: str) -> "tuple[pd.DataFrame, str | None]":
    """Fetch the latest CSV from GitHub and parse it. Returns (df, sha)."""
    csv_bytes, sha = fetch_catalog_csv_from_github(file_path, token)
    if not csv_bytes:
        # First-time write — empty DataFrame is fine (caller will populate)
        return pd.DataFrame(), sha
    # Some CSVs are latin-1 encoded; try utf-8 first, fall back to latin-1
    try:
        df = pd.read_csv(io.BytesIO(csv_bytes))
    except UnicodeDecodeError:
        df = pd.read_csv(io.BytesIO(csv_bytes), encoding="latin-1")
    return df, sha


def _save_catalog_csv(edited, source_df, editable_cols, file_path, label):
    """Cell-level merge: compute the user's diff, fetch the latest file from
    GitHub, apply only those cells on top, and push back. Retries once on a
    409 SHA conflict (the narrow race window where someone else committed
    between our fetch and our PUT).
    """
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return

    diffs = _compute_cell_diffs(edited, source_df, editable_cols)
    if not diffs:
        st.info("No changes detected.")
        return

    n_diffs = len(diffs)
    commit_message = f"Update {label} catalog labor values via app ({n_diffs} cells)"

    response = None
    for attempt in (1, 2):
        try:
            with st.spinner(
                f"Saving {label} catalog to GitHub "
                f"({n_diffs} cell change(s){' — retrying' if attempt == 2 else ''})..."
            ):
                fresh_df, fresh_sha = _read_fresh_csv(file_path, token)
                if fresh_df.empty:
                    st.error("Could not fetch the current catalog from GitHub.")
                    return
                csv_text, changed_skus_count = _apply_diffs_and_serialize(
                    fresh_df, diffs, editable_cols,
                )
                response = save_catalog_to_github(
                    csv_text, file_path, token,
                    message=commit_message,
                    sha=fresh_sha,
                )
            break  # success — exit retry loop
        except GitHubConflict:
            if attempt == 2:
                st.error(
                    "❌ Save failed: another user committed concurrently. "
                    "Please refresh the page (your edits are not lost — re-enter them) and try again."
                )
                return
            # else: loop will retry with a fresh fetch
            continue
        except Exception as e:
            st.error(
                f"❌ Save failed: {e}\n\n"
                "Common causes: GitHub token missing the `repo` scope, token expired, "
                "or the repo path in `core/catalog_storage.py` is wrong."
            )
            return

    commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
    commit_url = (response or {}).get("commit", {}).get("html_url", "")
    try:
        st.cache_data.clear()
    except Exception:
        pass

    st.success(
        f"✅ Saved {n_diffs} cell change(s) to `{file_path}` "
        + (f"(commit [`{commit_sha}`]({commit_url}))." if commit_url else "")
    )
    st.info(
        "⏳ **Streamlit Cloud is now redeploying** with the new values "
        "(~1–2 minutes). The Refresh banner at the top of this tab will let "
        "you reload once it's ready."
    )
    if st.button("🔄 I waited — try reloading now", key=f"reload_after_save_{label}"):
        st.rerun()


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


def _compute_station_depths(nodes, edges):
    """Topological depth per node — used to lay out stages in Graphviz.

    `nodes` = set of station keys that the unit visits.
    `edges` = list of dicts with `From`/`To` keys (already filtered to visited nodes).
    Returns {node: depth}, with depth=0 for nodes that have no incoming edges.
    Nodes in a cycle or unreachable fall back to depth=0 to avoid hanging the layout.
    """
    incoming = {n: [] for n in nodes}
    outgoing = {n: [] for n in nodes}
    for e in edges:
        f, t = e["From"], e["To"]
        if f in nodes and t in nodes:
            incoming[t].append(f)
            outgoing[f].append(t)

    depth = {n: 0 for n in nodes}
    # Topological sort via Kahn's algorithm
    indeg = {n: len(incoming[n]) for n in nodes}
    queue = [n for n in nodes if indeg[n] == 0]
    visited = set()
    while queue:
        n = queue.pop(0)
        if n in visited:
            continue
        visited.add(n)
        for t in outgoing[n]:
            if depth[t] < depth[n] + 1:
                depth[t] = depth[n] + 1
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    return depth


def tab_process_flow(units_df, machine_df, acc_df, inputs):
    """Visual flow chart of a single unit's path through the workstations.

    The graph is built from edges in `data/process_flow.json`, which the user
    can edit through the panel below the diagram. Edges are filtered per unit
    class (HS / HT / STD) so different products can have different shapes.
    Nodes are auto-layered by topological depth — no hardcoded stages.
    """
    from core.labor_calculator import compute_unit_labor, classify_unit
    from core.constants import STATION_KEY_TO_DISPLAY, STATION_KEYS, HS_FINAL_CREW

    st.header("🔀 Process Flow")
    st.markdown(
        "**Where does this unit go on the floor?** Pick an FG SKU "
        "(+ optional Accessory) to see the route. The graph is built from "
        "**editable per-class edges** stored in `data/process_flow.json` — use "
        "the **🔧 Edit process flow** panel below to correct the manufacturing "
        "precedence for your facility."
    )

    # ---- Load edges from JSON -------------------------------------------
    all_edges = _load_process_flow_edges(_csv_mtime("process_flow.json"))

    # ---- SKU selectors ---------------------------------------------------
    fg_options = sorted(machine_df["SKU"].astype(str).unique().tolist())
    if not fg_options:
        st.info("No machine SKUs loaded.")
        return

    default_fg = None
    if units_df is not None and not units_df.empty:
        try:
            default_fg = str(units_df.iloc[0]["fg_base"])
        except Exception:
            default_fg = None
    fg_default_idx = fg_options.index(default_fg) if default_fg in fg_options else 0

    c1, c2 = st.columns(2)
    with c1:
        fg_choice = st.selectbox(
            "FG SKU", fg_options, index=fg_default_idx, key="flow_fg",
        )
    with c2:
        family = _machine_family(fg_choice)
        acc_subset = acc_df[
            acc_df["SKU"].astype(str).apply(_accessory_family_hint).str.upper()
            == family.upper()
        ] if family != "OTHER" else acc_df.iloc[0:0]
        acc_options = ["(none)"] + sorted(acc_subset["SKU"].astype(str).tolist())
        acc_choice = st.selectbox("Accessory SKU", acc_options, key="flow_acc")

    acc_sku = None if acc_choice == "(none)" else acc_choice

    # ---- Compute labor for this pairing ---------------------------------
    labor = compute_unit_labor(fg_choice, acc_sku, machine_df, acc_df)
    if labor is None:
        st.error(f"`{fg_choice}` not found in the machine catalog.")
        return
    cls = labor["Class"]   # "HS" / "HT" / "STD"
    bat = int(labor["Bat"])

    # ---- Filter edges to this unit's class + visited stations ---------
    visited = {k for k in STATION_KEYS if float(labor.get(k, 0) or 0) > 0}
    filtered_edges = [
        e for e in all_edges
        if e.get("Class") in ("All", cls)
        and e.get("From") in visited and e.get("To") in visited
    ]

    if not visited:
        st.warning("This unit has no labor at any station — check the catalog.")
        return

    # ---- Per-station summary ---------------------------------------------
    crew_config = inputs["crew_config"]

    def _crew_for(st_key):
        disp = STATION_KEY_TO_DISPLAY.get(st_key, st_key)
        if disp in crew_config.index:
            try:
                return int(crew_config.loc[disp, "Crew"])
            except Exception:
                return 1
        return 1

    def _cycle(st_key, lbr):
        if lbr <= 0:
            return 0.0
        if st_key == "Final" and cls == "HS":
            return lbr / HS_FINAL_CREW
        crew = _crew_for(st_key)
        return lbr / crew if crew else 0.0

    station_visits = []
    for st_key in STATION_KEYS:
        lbr = float(labor.get(st_key, 0) or 0)
        if lbr <= 0:
            continue
        station_visits.append({
            "key": st_key,
            "name": STATION_KEY_TO_DISPLAY.get(st_key, st_key),
            "labor": int(round(lbr)),
            "cycle": round(_cycle(st_key, lbr), 1),
        })

    # ---- Topological-depth layout ---------------------------------------
    depths = _compute_station_depths(visited, filtered_edges)
    # Group by depth
    by_depth = {}
    for sv in station_visits:
        d = depths.get(sv["key"], 0)
        by_depth.setdefault(d, []).append(sv)
    max_depth = max(by_depth.keys()) if by_depth else 0

    # Color: orange if alone at its depth (sequential); blue if has siblings (parallel)
    SEQ_COLOR = "#FFE4B5"   # light orange
    PAR_COLOR = "#BCD6F2"   # light blue

    def _node_id(st_key: str) -> str:
        return f"n_{st_key}"

    def _node_label(sv) -> str:
        return f"{sv['name']}\\n{sv['labor']} p-min · {sv['cycle']:.0f} cal-min"

    # ---- Build the Graphviz DOT -----------------------------------------
    dot_lines = [
        "digraph G {",
        '  rankdir=TB;',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", margin="0.18,0.10"];',
        '  edge [color="#666"];',
    ]

    for d in sorted(by_depth.keys()):
        group = by_depth[d]
        color = PAR_COLOR if len(group) > 1 else SEQ_COLOR
        if len(group) > 1:
            dot_lines.append(f"  {{ rank=same; // depth {d}")
            for sv in group:
                dot_lines.append(
                    f'    {_node_id(sv["key"])} [label="{_node_label(sv)}", fillcolor="{color}"];'
                )
            dot_lines.append("  }")
        else:
            sv = group[0]
            dot_lines.append(
                f'  {_node_id(sv["key"])} [label="{_node_label(sv)}", fillcolor="{color}"];'
            )

    for e in filtered_edges:
        dot_lines.append(f'  {_node_id(e["From"])} -> {_node_id(e["To"])};')

    dot_lines.append("}")
    dot = "\n".join(dot_lines)

    # ---- Headline metrics + render --------------------------------------
    total_labor = sum(sv["labor"] for sv in station_visits)
    sum_cycles = sum(sv["cycle"] for sv in station_visits)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Class", cls,
              help="HS = head-skid (PM only); HT = head + trailer; STD = standard trailer.")
    c2.metric("Batteries", bat)
    c3.metric("Total labor", f"{total_labor:,} p-min")
    c4.metric("Stations visited", len(station_visits))

    if filtered_edges:
        st.graphviz_chart(dot, use_container_width=True)
    else:
        st.info(
            "No edges connect the stations this unit visits — diagram shows nodes "
            "only. Open **🔧 Edit process flow** below to add precedence edges."
        )
        st.graphviz_chart(dot, use_container_width=True)

    # ---- Per-station breakdown table ------------------------------------
    st.markdown("#### 🔎 Per-station breakdown")
    table_rows = []
    for sv in station_visits:
        d = depths.get(sv["key"], 0)
        same_depth = len(by_depth.get(d, []))
        table_rows.append({
            "Stage / depth": d,
            "Stage type": "Parallel" if same_depth > 1 else "Sequential",
            "Station": sv["name"],
            "Labor (p-min)": sv["labor"],
            "Cycle (cal-min)": sv["cycle"],
            "Crew (parallel)": (
                HS_FINAL_CREW if (sv["key"] == "Final" and cls == "HS")
                else _crew_for(sv["key"])
            ),
        })
    breakdown_df = pd.DataFrame(table_rows).sort_values(by="Stage / depth")
    total_row = pd.DataFrame([{
        "Stage / depth": "",
        "Stage type": "🟦 TOTAL",
        "Station": "All stations",
        "Labor (p-min)": total_labor,
        "Cycle (cal-min)": round(sum_cycles, 1),
        "Crew (parallel)": "—",
    }])
    breakdown_df = pd.concat([breakdown_df, total_row], ignore_index=True)
    st.dataframe(
        breakdown_df, use_container_width=True, hide_index=True, height=420,
    )

    # ---- Editor panel ----------------------------------------------------
    _render_flow_editor(all_edges)


def _render_flow_editor(edges: list) -> None:
    """Editable table of process-flow edges. Save / Reset push to GitHub."""
    from core.constants import STATION_KEYS

    with st.expander("🔧 Edit process flow", expanded=False):
        # Replay any one-shot toast stashed before rerun
        toast = st.session_state.pop("_flow_toast", None)
        if toast:
            level, msg = toast
            (st.success if level == "success" else st.error)(msg)

        st.caption(
            "Each row is one edge **From → To**. Use **Class = All** for edges that "
            "apply to every unit type, or **STD / HT / HS** for class-specific edges. "
            f"**Valid stations**: {', '.join(STATION_KEYS)}. "
            "**Valid classes**: All, STD, HT, HS."
        )

        # Build a clean seed DataFrame; normalize dtypes to avoid the
        # data_editor type-compat error.
        if edges:
            seed = pd.DataFrame(edges, columns=["From", "To", "Class"])
        else:
            seed = pd.DataFrame(columns=["From", "To", "Class"])
        for c in ("From", "To", "Class"):
            if c in seed.columns:
                seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})

        edited = st.data_editor(
            seed,
            num_rows="dynamic",
            use_container_width=True,
            key="flow_editor",
            height=420,
            column_config={
                "From": st.column_config.TextColumn(
                    "From", help=f"Station key (one of: {', '.join(STATION_KEYS)})",
                ),
                "To": st.column_config.TextColumn(
                    "To", help=f"Station key (one of: {', '.join(STATION_KEYS)})",
                ),
                "Class": st.column_config.TextColumn(
                    "Class", help="All / STD / HT / HS", width="small",
                ),
            },
        )

        # Live validation feedback
        bad_count = 0
        valid_stations = set(STATION_KEYS)
        for _, row in edited.iterrows():
            f = str(row.get("From", "") or "").strip()
            t = str(row.get("To", "") or "").strip()
            c = str(row.get("Class", "") or "").strip()
            if not f and not t and not c:
                continue  # blank scratch row
            if (f and f not in valid_stations) \
                    or (t and t not in valid_stations) \
                    or (c and c not in PROCESS_FLOW_VALID_CLASSES):
                bad_count += 1
        if bad_count:
            st.warning(
                f"⚠️ {bad_count} row(s) reference unknown station(s) or class. "
                "These will be silently dropped on save."
            )

        c1, c2 = st.columns([3, 1])
        with c1:
            if st.button(
                "💾 Save process flow to GitHub",
                key="flow_save_btn", use_container_width=True,
            ):
                _flow_save(edited)
        with c2:
            if st.button(
                "🔄 Reset to default",
                key="flow_reset_btn", use_container_width=True,
            ):
                _flow_reset()


def _flow_save(edited_df) -> None:
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner("Saving process flow to GitHub..."):
            response = save_process_flow_to_github(edited_df, token)
        commit_sha = (response or {}).get("commit", {}).get("sha", "")[:7]
        commit_url = (response or {}).get("commit", {}).get("html_url", "")
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.session_state["_flow_toast"] = (
            "success",
            f"✅ Process flow saved"
            + (f" (commit [`{commit_sha}`]({commit_url}))." if commit_url else "."),
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Save failed: {e}")


def _flow_reset() -> None:
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured — add `github_token` to Streamlit Secrets.")
        return
    try:
        with st.spinner("Resetting process flow to default..."):
            reset_process_flow_to_default(token)
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.session_state["_flow_toast"] = (
            "success", "🔄 Process flow reset to default.",
        )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Reset failed: {e}")


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


def _tokenize_description(description: str) -> set:
    """Split an accessory description into uppercase tokens (word boundaries)."""
    import re
    if not description:
        return set()
    desc_up = str(description).upper()
    tokens = set(re.split(r"[^A-Z0-9]+", desc_up))
    tokens.discard("")
    return tokens


def _detect_items_in_description(description: str, item_master_df) -> list:
    """Unique items from the master whose Abbr appears as a whole token in the description.

    The master may have multiple rows per Abbr (one default + variants). We
    dedupe so each item appears only once in the detected list.
    """
    if item_master_df is None or item_master_df.empty:
        return []
    tokens = _tokenize_description(description)
    out = []
    seen = set()
    for a in item_master_df["Abbr"]:
        s = str(a).strip()
        if not s:
            continue
        u = s.upper()
        if u in tokens and u not in seen:
            seen.add(u)
            out.append(s)
    return out


def _detect_packages_in_description(description: str, packages_df) -> list:
    """Packages whose code appears as a whole token in the description (e.g. CWP, AWP)."""
    if packages_df is None or packages_df.empty:
        return []
    tokens = _tokenize_description(description)
    return [
        str(p).strip()
        for p in packages_df["Package"]
        if str(p).strip() and str(p).strip().upper() in tokens
    ]


def _expand_and_dedupe(items_found, packages_found, packages_df) -> list:
    """Expand each detected package into its components, union with standalone
    items, and dedupe. Preserves first-seen ordering for readable output.

    Example: items=["EBH"], packages=["AWP"] (AWP = CCV,EH,EBH,BHW,HFF,LRH)
    -> ["CCV", "EH", "EBH", "BHW", "HFF", "LRH"]  (EBH counted once)
    """
    seen = set()
    ordered = []

    def _add(abbr):
        a = str(abbr).strip()
        if not a:
            return
        key = a.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(a)

    # Expand packages first so component ordering reflects the package definition
    if packages_df is not None and not packages_df.empty:
        pkg_lookup = {
            str(r["Package"]).strip().upper(): str(r.get("Components", "") or "")
            for _, r in packages_df.iterrows()
        }
    else:
        pkg_lookup = {}

    for pkg in packages_found or []:
        comps = pkg_lookup.get(str(pkg).strip().upper(), "")
        for comp in [c.strip() for c in comps.split(",") if c.strip()]:
            _add(comp)

    # Then standalone items (deduped against package components)
    for it in items_found or []:
        _add(it)

    return ordered


def _accessory_sides(family_hint: str, sku: str) -> list:
    """Which station side(s) this accessory feeds into.

    - PDS family -> ["Compressor"]
    - SDG / BOSS non-HS -> ["Generator"]
    - HS variants (BOSSxxHS / -HS- in family) -> ["Generator", "PM"] so HS units
      with PM-only accessories still get the PM side.
    """
    fam = str(family_hint or "").upper()
    if fam.startswith("PDS"):
        return ["Compressor"]
    sides = ["Generator"]
    if "HS" in fam or "HS" in str(sku or "").upper():
        sides.append("PM")
    return sides


def _is_compressor_family(fg_family_hint: str) -> bool:
    """A FG family is a Compressor if it starts with PDS (portable diesel
    compressor). Generators (SDG) and BOSS are not."""
    s = str(fg_family_hint).upper().strip()
    return s.startswith("PDS")


def _accessory_family_hint(sku: str) -> str:
    """Strip the trailing -A### / -AXXX from an accessory SKU to get the FG family.

    e.g. BOSS25-A016 -> BOSS25;  PDS185EZ-A001 -> PDS185EZ.
    """
    import re
    s = str(sku).strip()
    return re.split(r"-A", s)[0]


# Order matters: longest prefixes first so BOSS220HS-002 resolves to BOSS220
# (not the non-existent BOSS2 or BOSS22).
KNOWN_FAMILIES = ["BOSS400", "BOSS220", "BOSS125", "BOSS70", "BOSS25", "PDS", "SDG"]


def _machine_family(sku: str) -> str:
    """Map an FG SKU to a high-level product family (BOSS25 / BOSS70 / … / PDS / SDG)."""
    s = str(sku or "").upper().strip()
    if not s:
        return "OTHER"
    for fam in KNOWN_FAMILIES:
        if s.startswith(fam):
            return fam
    return "OTHER"


def _machine_class(sku: str, description: str) -> str:
    """Classify a machine SKU into one of:
       ⚠ Placeholder · Hybrid · Power Module · Head Trailer · Standard Trailer · Standard.

    PDS/SDG always classify as 'Standard' (compressor/generator base units).
    BOSS rules apply in this priority: Placeholder > Hybrid > Power Module > Head Trailer > Standard Trailer.
    """
    s = str(sku or "").upper().strip()
    d = str(description or "").upper()
    fam = _machine_family(s)
    if "XXX" in s:
        return "⚠ Placeholder"
    if fam in ("PDS", "SDG"):
        return "Standard"
    if "HYBRID" in d:
        return "Hybrid"
    # "HS" or "PM SKID" / "SKID" indicates a Power Module (head-skid only)
    if "HS" in s.replace(fam, "", 1) or "PM SKID" in d or " SKID " in f" {d} ":
        return "Power Module"
    # "HT" in the SKU after the family prefix indicates a Head + Trailer unit
    if "HT" in s.replace(fam, "", 1):
        return "Head Trailer"
    return "Standard Trailer"


def _family_battery_count(machine_df, family_prefix: str) -> int:
    """Max `Bat` among machine rows whose SKU starts with `family_prefix`.

    Returns 0 when nothing matches (e.g. PDS / SDG / unknown family) — those
    accessories simply skip the battery multiplier in the Total column.

    The machine catalog is the authoritative source for battery counts;
    `BATTERY_COUNT_OVERRIDES` from `core/constants.py` is already baked into
    the loaded `machine_df["Bat"]` by `load_machine_labor()`.
    """
    if not family_prefix or machine_df is None or len(machine_df) == 0:
        return 0
    pf = str(family_prefix).strip().upper()
    if not pf:
        return 0
    skus = machine_df["SKU"].astype(str).str.upper()
    mask = skus.str.startswith(pf)
    if not mask.any():
        return 0
    try:
        return int(machine_df.loc[mask, "Bat"].fillna(0).max())
    except (TypeError, ValueError):
        return 0


def _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc):
    """Reconciliation & Apply — packages expanded, family variants honored, PM included.

    For each accessory:
      1. Detect item abbrevs + package codes in the description
      2. Expand packages -> components, dedupe with standalone items
      3. For each side this accessory feeds, resolve each item's time from the
         unified item master (variant rows override the default row), sum, and
         compare against the aggregate in acc_clean.csv
      4. Let the user select rows to push back into acc_clean.csv.
    """
    st.subheader("🔬 Reconciliation & Apply")
    st.markdown(
        "**Cross-check the catalog aggregate against the item master + packages.**  \n"
        "Packages auto-expand into their components (no double-counting). "
        "Family-specific time variants in the item master override the default row. "
        "When the math looks right, tick rows in the **Apply** column and click "
        "**Apply selected updates** to write the new value back to `acc_clean.csv`."
    )
    st.caption(
        "Side mapping: PDS family → **Compressor** · SDG/BOSS → **Generator** "
        "(plus **PM** for HS-derived accessories). One review row per (SKU, side)."
    )

    cc1, cc2 = st.columns([2, 1])
    with cc1:
        only_used = st.checkbox(
            "Show only accessories used in current schedule", value=True,
            key="recon_only_used",
        )
    with cc2:
        side_filter = st.multiselect(
            "Sides", ["Compressor", "Generator", "PM"],
            default=["Compressor", "Generator", "PM"],
            key="recon_side_filter",
        )

    side_to_agg_col = {"Compressor": "ComAcc", "Generator": "GenAcc", "PM": "PMAcc"}

    rows = []
    for sku in acc_df.index:
        if only_used and used_acc.get(sku, 0) == 0:
            continue
        ar = acc_df.loc[sku]
        desc = str(ar.get("Description", ""))
        family_hint = _accessory_family_hint(sku)

        items_found = _detect_items_in_description(desc, item_master_df)
        pkgs_found = _detect_packages_in_description(desc, item_packages_df)
        expanded = _expand_and_dedupe(items_found, pkgs_found, item_packages_df)

        sides = _accessory_sides(family_hint, sku)
        for side in sides:
            if side not in side_filter:
                continue
            agg_col = side_to_agg_col[side]
            try:
                agg_value = float(ar[agg_col]) if agg_col in acc_df.columns else 0.0
            except (TypeError, ValueError):
                agg_value = 0.0

            item_times = []
            for abbr in expanded:
                t = resolve_item_time(abbr, family_hint, side, item_master_df)
                if t > 0:
                    item_times.append((abbr, t))
            items_sum = sum(t for _, t in item_times)
            breakdown = ", ".join(f"{a}={t:.0f}" for a, t in item_times) or "—"
            pkg_label = ", ".join(pkgs_found) if pkgs_found else ""

            diff = items_sum - agg_value
            if abs(diff) < 0.5:
                status = "✅ Match"
            elif items_sum == 0 and agg_value > 0:
                status = "⚪ No items detected"
            elif items_sum > 0 and agg_value == 0:
                status = "⚠️ Items but no aggregate"
            elif diff > 0:
                status = f"🔺 Items higher (+{diff:.0f})"
            else:
                status = f"🔻 Aggregate higher ({diff:.0f})"

            rows.append({
                "Apply": False,
                "SKU": sku,
                "Family": family_hint,
                "Side": side,
                "Aggregate (catalog)": int(round(agg_value)),
                "Sum of items": int(round(items_sum)),
                "Difference": int(round(diff)),
                "Status": status,
                "Packages": pkg_label,
                "Items detected": breakdown,
                "Description": desc,
                "Used in schedule": used_acc.get(sku, 0),
            })

    if not rows:
        st.info("No accessories to show. Uncheck the filter or widen the side selection.")
        return

    rec_df = pd.DataFrame(rows).sort_values(
        by=["Used in schedule", "Difference"], ascending=[False, True],
    ).reset_index(drop=True)

    # Top metrics
    matched = int((rec_df["Status"] == "✅ Match").sum())
    no_items = int((rec_df["Status"] == "⚪ No items detected").sum())
    mismatches = len(rec_df) - matched - no_items

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Review rows", len(rec_df))
    c2.metric("Matches", matched)
    c3.metric("Mismatches", mismatches)
    c4.metric("No items detected", no_items)

    edited = st.data_editor(
        rec_df,
        use_container_width=True, height=520, hide_index=True,
        num_rows="fixed",
        key="recon_editor",
        column_config={
            "Apply": st.column_config.CheckboxColumn(
                "Apply",
                help="Tick rows whose 'Sum of items' you want to write back as the new aggregate.",
            ),
            "SKU": st.column_config.TextColumn("SKU", disabled=True),
            "Family": st.column_config.TextColumn("Family", disabled=True),
            "Side": st.column_config.TextColumn("Side", disabled=True),
            "Aggregate (catalog)": st.column_config.NumberColumn(
                "Aggregate (catalog)", disabled=True,
                help="Current value in acc_clean.csv.",
            ),
            "Sum of items": st.column_config.NumberColumn(
                "Sum of items", disabled=True,
                help="Sum of item times resolved from the master (family variant wins over default), with packages expanded.",
            ),
            "Difference": st.column_config.NumberColumn("Difference", disabled=True),
            "Status": st.column_config.TextColumn("Status", disabled=True),
            "Packages": st.column_config.TextColumn(
                "Packages", disabled=True,
                help="Package codes detected in the description, before expansion.",
            ),
            "Items detected": st.column_config.TextColumn(
                "Items detected (after expansion)", width="large", disabled=True,
            ),
            "Description": st.column_config.TextColumn("Description", width="large", disabled=True),
        },
    )

    # Apply button
    selected = edited[edited["Apply"] == True]  # noqa: E712
    n_sel = len(selected)
    btn_label = f"💾 Apply {n_sel} selected update(s) to SKU aggregate" if n_sel \
                else "💾 Apply selected updates to SKU aggregate"
    if st.button(btn_label, use_container_width=True, disabled=(n_sel == 0)):
        _apply_recon_to_acc_csv(selected, acc_df)

    # Download for offline review
    csv = edited.drop(columns=["Apply"]).to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download reconciliation CSV", csv, "accessory_reconciliation.csv", "text/csv",
    )


def _apply_recon_to_acc_csv(selected_df, acc_df):
    """Take the user's selected reconciliation rows and write updated values
    into acc_clean.csv via the existing GitHub-API save path."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured. Ask your admin to add `github_token` to Streamlit Secrets.")
        return

    side_to_agg_col = {"Compressor": "ComAcc", "Generator": "GenAcc", "PM": "PMAcc"}

    # Start from the current acc_df (excluding the index 'SKU' duplicate column noise)
    out = acc_df.copy()
    changed = 0
    for _, sel in selected_df.iterrows():
        sku = sel["SKU"]
        side = sel["Side"]
        new_val = float(sel["Sum of items"])
        col = side_to_agg_col.get(side)
        if not col or sku not in out.index or col not in out.columns:
            continue
        old_val = float(out.at[sku, col]) if pd.notna(out.at[sku, col]) else 0.0
        if abs(new_val - old_val) < 0.5:
            continue
        out.at[sku, col] = new_val
        changed += 1

    if changed == 0:
        st.info("No actual changes detected (selected rows already match the catalog).")
        return

    # The SKU index and 'SKU' column are duplicates from set_index(..., drop=False);
    # drop the column before writing so to_csv emits SKU once via reset_index.
    csv_text = out.drop(columns=["SKU"], errors="ignore").reset_index().to_csv(index=False)

    try:
        with st.spinner(f"Saving {changed} updated cell(s) to acc_clean.csv..."):
            save_catalog_to_github(
                csv_text, "data/acc_clean.csv", token,
                message=f"Apply reconciliation: update {changed} accessory cell(s)",
            )
        st.success(
            f"✅ Wrote {changed} updated cell(s) to acc_clean.csv. "
            "New values apply after the app redeploys (~1 min)."
        )
    except Exception as e:
        st.error(f"Save failed: {e}")


def _render_item_master_view(item_master_df):
    """Unified items table — master defaults, family variants, AND per-accessory
    entries in one editor. Replaces the previous split between Item Master and
    Accessory Item Details views."""
    st.subheader("📒 Items")
    st.markdown(
        "**Unified table** of every installable item — master defaults, "
        "family variants, and per-accessory entries all in one place. Edit "
        "inline and click Save to persist."
    )
    st.caption(
        "Each row defines an item's labor times. Use the **FG family** and "
        "**Accessory SKU** columns to scope the row:\n"
        "- Both blank → **default row** (used when nothing more specific matches)\n"
        "- `FG family` only (e.g. `SDG13`, `PDS185EZ`) → **family variant** (longest prefix wins)\n"
        "- `Accessory SKU` populated → **per-accessory entry** (item belongs to that specific accessory)\n\n"
        "Time columns are person-minutes on each side (Compressor / Generator / PM)."
    )

    # Normalize dtypes so st.data_editor's type checker doesn't complain
    seed = item_master_df.copy()
    for c in ("Abbr", "Description", "FG family", "Accessory SKU", "Notes"):
        if c in seed.columns:
            seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})
    for c in ("Time on Compressor (min)", "Time on Generator (min)", "Time on PM (min)"):
        if c in seed.columns:
            seed[c] = pd.to_numeric(seed[c], errors="coerce").fillna(0).astype(float)

    edited = st.data_editor(
        seed,
        use_container_width=True,
        num_rows="dynamic",
        key="item_master_editor",
        height=560,
        column_config={
            "Abbr": st.column_config.TextColumn(
                "Abbr", help="Short code used in accessory descriptions (e.g. EBH, BHW, TEL).",
                width="small",
            ),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "FG family": st.column_config.TextColumn(
                "FG family",
                help="Leave blank for a default / per-accessory row. Set to a prefix "
                     "like SDG13 or PDS185EZ to make this row a family variant.",
                width="small",
            ),
            "Accessory SKU": st.column_config.TextColumn(
                "Accessory SKU",
                help="Leave blank for default/family rows. Populate (e.g. `BOSS25-A016`) "
                     "to declare this item belongs to a specific accessory.",
                width="small",
            ),
            "Time on Compressor (min)": st.column_config.NumberColumn(
                "Compressor min", min_value=0, step=1,
                help="Install time on a Compressor product (PDS series).",
            ),
            "Time on Generator (min)": st.column_config.NumberColumn(
                "Generator min", min_value=0, step=1,
                help="Install time on a Generator product (SDG / BOSS series).",
            ),
            "Time on PM (min)": st.column_config.NumberColumn(
                "PM min", min_value=0, step=1,
                help="Install time on a PM (head-skid / HS) unit. Used when the "
                     "accessory feeds the PM Acc station.",
            ),
            "Notes": st.column_config.TextColumn("Notes", width="large"),
        },
    )

    if st.button("💾 Save items to GitHub", use_container_width=True):
        _save_simple_csv(edited, "data/item_master.csv", "items")


def _render_item_packages_view(packages_df, item_master_df):
    """Reference table of cold-weather / accessory packages (item bundles)."""
    st.subheader("📦 Item packages")
    st.markdown(
        "**Pre-defined bundles** of items often ordered together (e.g. "
        "Cold Weather Package). Each bundle's Components is a comma-separated "
        "list of item abbreviations from the Item master."
    )

    # Quick check: are all package components defined in the item master?
    if not packages_df.empty and not item_master_df.empty:
        all_abbrs = set(item_master_df["Abbr"].astype(str).str.strip())
        problems = []
        for _, row in packages_df.iterrows():
            comps = [c.strip() for c in str(row.get("Components", "")).split(",") if c.strip()]
            missing = [c for c in comps if c not in all_abbrs]
            if missing:
                problems.append(f"`{row['Package']}` references unknown item(s): {', '.join(missing)}")
        if problems:
            st.warning("⚠️ " + " · ".join(problems))

    # Normalize column dtypes so the editor's type checker is happy
    seed = packages_df.copy()
    for c in ("Package", "Description", "Components", "Notes"):
        if c in seed.columns:
            seed[c] = seed[c].astype(str).replace({"nan": "", "None": ""})
    if "Total Time (min)" in seed.columns:
        seed["Total Time (min)"] = pd.to_numeric(seed["Total Time (min)"], errors="coerce").fillna(0).astype(float)

    edited = st.data_editor(
        seed,
        use_container_width=True,
        num_rows="dynamic",
        key="item_packages_editor",
        height=400,
        column_config={
            "Package": st.column_config.TextColumn("Package code", width="small"),
            "Description": st.column_config.TextColumn("Description", width="large"),
            "Total Time (min)": st.column_config.NumberColumn(
                "Total time (min)", min_value=0, step=1,
                help="Total install time for the whole package.",
            ),
            "Components": st.column_config.TextColumn(
                "Components",
                help="Comma-separated abbreviations from the Item master.",
                width="large",
            ),
            "Notes": st.column_config.TextColumn("Notes"),
        },
    )

    if st.button("💾 Save item packages to GitHub", use_container_width=True):
        _save_simple_csv(edited, "data/item_packages.csv", "item packages")


def _save_simple_csv(df, file_path, label):
    """Generic save helper — write a DataFrame as CSV to GitHub."""
    token = st.secrets.get("github_token", None) if hasattr(st, "secrets") else None
    if not token:
        st.error("GitHub token not configured. Ask your admin to add `github_token` to Streamlit Secrets.")
        return
    try:
        # Drop rows where every column is empty/NaN
        clean = df.dropna(how="all").copy()
        for c in clean.select_dtypes(include="object").columns:
            clean[c] = clean[c].astype(str).str.strip()
        csv_text = clean.to_csv(index=False)
        with st.spinner(f"Saving {label} to GitHub..."):
            save_catalog_to_github(
                csv_text, file_path, token,
                message=f"Update {label} via app",
            )
        st.success(f"✅ Saved {label}! New values apply after the app redeploys (~1 min).")
    except Exception as e:
        st.error(f"Save failed: {e}")


def tab_source_data(machine_df, acc_df, schedule_df,
                     item_master_df, item_packages_df):
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
        [
            "Schedule",
            "Machine catalog",
            "Accessory catalog",
            "Items",
            "Item packages",
            "Reconciliation & Apply",
        ],
        horizontal=True,
    )

    if sub == "Items":
        _render_item_master_view(item_master_df)
        return
    if sub == "Item packages":
        _render_item_packages_view(item_packages_df, item_master_df)
        return
    if sub == "Reconciliation & Apply":
        _render_reconciliation_view(acc_df, item_master_df, item_packages_df, used_acc)
        return

    if sub == "Schedule":
        st.dataframe(schedule_df, use_container_width=True, height=600)
    elif sub == "Machine catalog":
        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="m_only_used"
        )
        m_disp = machine_df.copy()
        # Backward-compat: alias old "FN_Assy_old" → "FN_Assy" if cache is stale
        if "FN_Assy_old" in m_disp.columns and "FN_Assy" not in m_disp.columns:
            m_disp = m_disp.rename(columns={"FN_Assy_old": "FN_Assy"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in m_disp.columns:
            m_disp["Last Modified"] = ""
        # Per-machine total labor across the assembly stations.
        # Bat is a COUNT (not labor) so it's excluded from the sum.
        machine_labor_cols = ["Warehouse", "Wire", "Trailer", "FN_Assy",
                              "PDI", "QC", "Ship"]
        present_cols = [c for c in machine_labor_cols if c in m_disp.columns]
        m_disp["Total labor (p-min)"] = m_disp[present_cols].fillna(0).sum(axis=1).astype(int)
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
        st.caption(
            f"🟡 highlighted = used in current schedule "
            f"({sum(m_disp['In schedule'])} of {len(m_disp)} shown). "
            "**Total labor** = sum of Warehouse + Wire + Trailer + Final + PDI + QC + Ship "
            "(person-minutes per unit, excluding accessory + battery labor)."
        )
    else:  # Accessory catalog
        only_used = st.checkbox(
            "Show only SKUs used in current schedule", value=False, key="a_only_used"
        )
        a_disp = acc_df.copy()
        # Backward-compat: if the loaded DataFrame has the old "Compressor"
        # column (because cache hasn't refreshed), alias it to "ComAcc".
        if "Compressor" in a_disp.columns and "ComAcc" not in a_disp.columns:
            a_disp = a_disp.rename(columns={"Compressor": "ComAcc"})
        # Defensive: ensure Last Modified column exists (might be missing from stale cache)
        if "Last Modified" not in a_disp.columns:
            a_disp["Last Modified"] = ""
        a_disp.insert(0, "Used (qty)", a_disp.index.map(lambda s: used_acc.get(s, 0)))
        a_disp.insert(1, "In schedule", a_disp["Used (qty)"] > 0)
        # Per-accessory total uses the EXACT battery count from machine_clean.csv
        # for the family. PDS / SDG accessories → 0 (no battery multiplier).
        acc_labor_cols = ["Warehouse", "AccKIT", "Nameplate Prep", "BattSubRaw",
                          "PMAcc", "GenAcc", "ComAcc"]
        # Only sum columns that actually exist (defensive against stale cache)
        present_labor_cols = [c for c in acc_labor_cols if c in a_disp.columns]
        non_batt_cols = [c for c in present_labor_cols if c != "BattSubRaw"]
        bat_counts = a_disp.index.to_series().astype(str).apply(
            lambda s: _family_battery_count(machine_df, _accessory_family_hint(s))
        )
        base = a_disp[non_batt_cols].fillna(0).sum(axis=1)
        per_batt = a_disp["BattSubRaw"].fillna(0) if "BattSubRaw" in a_disp.columns else pd.Series(0, index=a_disp.index)
        a_disp["Bat"] = bat_counts.astype(int)
        a_disp["Total per unit (p-min)"] = (base + per_batt * bat_counts).astype(int)
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
        st.caption(
            f"🟡 highlighted = used in current schedule "
            f"({sum(a_disp['In schedule'])} of {len(a_disp)} shown). "
            "**Total per unit** = non-battery labor + `BattSubRaw × Bat`. "
            "`Bat` is the exact count from the machine catalog for the accessory's FG family."
        )


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
    item_master_df = _load_item_master_df(_csv_mtime("item_master.csv"))
    item_packages_df = _load_item_packages_df(_csv_mtime("item_packages.csv"))

    # ----------------------------------------------------------------
    # Load schedule (manual or CSV). If empty / invalid, we don't return —
    # instead we fall through with empty placeholders so the tabs still
    # render and the user can edit data via the 📁 Data & Setup tab.
    # ----------------------------------------------------------------
    schedule_df = pd.DataFrame()
    empty_banner_msg = None

    if inputs.get("schedule_mode") == "✏️ Type a few SKUs":
        manual_entries = inputs.get("manual_entries")
        if manual_entries is None or manual_entries.empty:
            empty_banner_msg = (
                "✏️ **Manual entry mode** — fill in FG SKU and Quantity in the sidebar to populate "
                "the analysis tabs. You can still use **📁 Data & Setup** to edit catalogs."
            )
        else:
            machine_skus = set(machine_df["SKU"])
            schedule_df = build_manual_schedule(
                manual_entries, location=inputs["location"], machine_skus=machine_skus,
            )
            if schedule_df.empty:
                empty_banner_msg = (
                    "✏️ **Manual entry mode** — fill in at least one row (FG SKU + Quantity > 0) in "
                    "the sidebar. You can still use **📁 Data & Setup** to edit catalogs."
                )
    else:
        schedule_df = _load_schedule_df(inputs["uploaded"], location=inputs["location"])
        if schedule_df.empty:
            empty_banner_msg = (
                f"⚠️ No schedule rows found for **{inputs['location']}**. "
                "Upload a CSV that contains this location, or select a different location. "
                "You can still use **📁 Data & Setup** to edit catalogs."
            )

    # ----------------------------------------------------------------
    # Compute analysis frames. Empty-safe: when there are no units, we
    # construct empty placeholders so downstream tabs can detect the
    # empty state and show their own banner instead of crashing.
    # ----------------------------------------------------------------
    if schedule_df.empty:
        units = pd.DataFrame()
    else:
        units = expand_schedule(schedule_df, machine_df, acc_df)
        if units.empty:
            empty_banner_msg = (
                "⚠️ Could not expand schedule into units. Check that FG / Accessory SKUs "
                "match the catalog. You can edit catalogs in **📁 Data & Setup**."
            )

    if units.empty:
        capacity = pd.DataFrame()
        batt_sku = pd.DataFrame(columns=["fg_base", "batt_type", "units", "batt_per_unit",
                                           "total_batteries", "pct_of_total"])
        batt_type = pd.DataFrame(columns=["batt_type", "total_batteries", "units_count",
                                           "pct_of_total"])
    else:
        capacity = build_capacity_table(
            units, inputs["crew_config"], inputs["shift"], inputs["days"],
            inputs["safety"], inputs["efficiency"],
        )
        batt_sku = battery_demand_by_sku(units)
        batt_type = battery_demand_by_type(units)

    # Detect schedule month for display (Mon-YY format rows, not carryover)
    if schedule_df.empty or "CARRYOVER" not in schedule_df.columns:
        schedule_month = ""
    else:
        current_months = schedule_df.loc[~schedule_df["CARRYOVER"], "PRODUCTION MONTH"].unique().tolist()
        schedule_month = current_months[0] if current_months else ""

    # Top-of-page banner when there's no schedule loaded
    if empty_banner_msg:
        st.info(empty_banner_msg)

    # Tabs — 6 top-level tabs, ordered from executive summary → planner detail → admin.
    # Batteries content folded into Capacity. Update Labor + Data Quality + Source Data
    # consolidated into the single 📁 Data & Setup tab.
    tabs = st.tabs([
        "🏠 Overview",
        "📊 Capacity",
        "🔧 Recommendations",
        "⏱ Build Time",
        "🔀 Process Flow",
        "📁 Data & Setup",
    ])

    # Helper — show an "empty state" message inside analysis tabs when there's no schedule
    def _empty_state(tab_name: str):
        st.info(
            f"📭 **{tab_name} needs a schedule to populate.**  \n"
            "• In the sidebar, switch to **📤 Upload schedule file** or **✏️ Type a few SKUs**.  \n"
            "• You can still edit catalogs, items, packages, and the process flow from the "
            "**📁 Data & Setup** tab without a schedule."
        )

    with tabs[0]:
        if units.empty:
            _empty_state("Overview")
        else:
            tab_overview(units, capacity, batt_type, inputs, schedule_month)
    with tabs[1]:
        if units.empty:
            _empty_state("Capacity")
        else:
            tab_capacity_vs_demand(capacity, inputs, batt_sku=batt_sku, batt_type=batt_type)
    with tabs[2]:
        if units.empty:
            _empty_state("Recommendations")
        else:
            tab_mitigation(capacity, batt_sku, units, inputs)
    with tabs[3]:
        if units.empty:
            _empty_state("Build Time")
        else:
            tab_cycle_time(units, inputs)
    with tabs[4]:
        # Process Flow can show the editable flow even without a schedule
        tab_process_flow(units, machine_df, acc_df, inputs)
    with tabs[5]:
        tab_data_setup(
            machine_df=machine_df, acc_df=acc_df, schedule_df=schedule_df,
            item_master_df=item_master_df, item_packages_df=item_packages_df,
        )


if __name__ == "__main__":
    main()
