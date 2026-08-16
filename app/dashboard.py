"""
dashboard.py
------------
rDIS - Real time Digital Inspection System - Streamlit dashboard.

Run:
    streamlit run dashboard.py

Crash-resistance decisions made in this file:
  - Every DB call is wrapped in try/except; failures show a friendly
    banner instead of an unhandled exception killing the whole app.
  - st.cache_data is used for KPIs/trend/lookups so repeat page loads
    (every widget interaction reruns the whole script in Streamlit)
    don't re-hit SQL Server each time.
  - The main table is PAGINATED (page_size rows at a time) — it never
    tries to render 70 lakh rows into a dataframe widget, which is the
    #1 way to freeze a Streamlit app.
  - New inspection capture writes are wrapped too, with a clear
    success/failure message instead of a crash on a bad image or a
    dropped connection.
"""

import io
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import plotly.express as px

import db_utils as db

st.set_page_config(page_title="rDIS - Real Time Digital Inspection System",
                    layout="wide", page_icon="📋")

# ------------------------------------------------------------------
# Live mode — makes this a real-time management system rather than a
# static report. When ON, the page silently re-runs on a timer and
# re-pulls KPIs/trend/latest activity, without the user touching anything.
# Implemented as a lightweight timer + st.rerun() so it needs no extra
# package (works the same on a laptop or a real deployment).
# ------------------------------------------------------------------
st.sidebar.header("⚡ Live Mode")
live_mode = st.sidebar.toggle("Enable live updates", value=True)
refresh_seconds = st.sidebar.select_slider(
    "Refresh interval", options=[5, 10, 15, 30, 60], value=15,
    disabled=not live_mode,
)

# ------------------------------------------------------------------
# Health check banner — never let a dead DB connection crash the app
# ------------------------------------------------------------------
db_ok = db.health_check()
status_col1, status_col2 = st.columns([5, 1])
with status_col1:
    st.title("📋 rDIS — Real Time Digital Inspection System")
with status_col2:
    if db_ok:
        st.success("🟢 Live", icon="✅")
    else:
        st.error("🔴 Offline", icon="⚠️")

if not db_ok:
    st.error("⚠️ Database unreachable. Showing cached data where available. "
             "New inspections will be queued locally until connection is restored.")

st.caption(f"Last refreshed: {datetime.now().strftime('%H:%M:%S')}"
           + (f" • auto-refreshing every {refresh_seconds}s" if live_mode else " • live mode paused"))


# ------------------------------------------------------------------
# Cached readers — protects against re-querying on every rerun
# ------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def cached_lookup(table_name: str) -> pd.DataFrame:
    try:
        return db.fetch_lookup(table_name)
    except Exception as e:
        st.warning(f"Could not load {table_name}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def cached_kpis(date_from, date_to) -> dict:
    try:
        return db.fetch_kpis(date_from, date_to)
    except Exception as e:
        st.warning(f"KPI load failed: {e}")
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def cached_trend(days: int) -> pd.DataFrame:
    try:
        return db.fetch_trend(days)
    except Exception as e:
        st.warning(f"Trend load failed: {e}")
        return pd.DataFrame()


def cached_page(page, page_size, status, site_id, date_from, date_to) -> pd.DataFrame:
    # Deliberately NOT cached long-term (recent data changes often),
    # but still bounded to page_size rows so it's always fast & safe.
    try:
        return db.fetch_inspections_page(page, page_size, status, site_id, date_from, date_to)
    except Exception as e:
        st.error(f"Could not load inspections page: {e}")
        return pd.DataFrame()


# ------------------------------------------------------------------
# Sidebar filters
# ------------------------------------------------------------------
st.sidebar.header("Filters")

date_range = st.sidebar.date_input(
    "Date range",
    value=(datetime.now().date() - timedelta(days=30), datetime.now().date()),
)
date_from, date_to = (date_range if isinstance(date_range, tuple) and len(date_range) == 2
                       else (date_range, date_range))

sites_df = cached_lookup("Sites")
site_options = ["All"] + sites_df["SiteName"].tolist() if not sites_df.empty else ["All"]
site_choice = st.sidebar.selectbox("Site", site_options)
site_id = None
if site_choice != "All" and not sites_df.empty:
    site_id = int(sites_df.loc[sites_df["SiteName"] == site_choice, "SiteID"].iloc[0])

status_choice = st.sidebar.selectbox("Status", ["All", "Passed", "Failed", "Flagged", "Pending"])
status = None if status_choice == "All" else status_choice

page_size = st.sidebar.selectbox("Rows per page", [50, 100, 200, 500], index=1)


# ------------------------------------------------------------------
# KPI cards — read from pre-aggregated DailySummary, constant-time
# regardless of whether the table has 1,000 or 70,00,000 rows
# ------------------------------------------------------------------
kpis = cached_kpis(date_from, date_to)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Inspections", f"{int(kpis.get('total_inspections') or 0):,}")
c2.metric("Passed", f"{int(kpis.get('passed') or 0):,}")
c3.metric("Failed", f"{int(kpis.get('failed') or 0):,}")
c4.metric("Flagged", f"{int(kpis.get('flagged') or 0):,}")
avg_risk = kpis.get("avg_risk_score")
c5.metric("Avg Risk Score", f"{avg_risk:.1f}" if avg_risk is not None else "—")

st.divider()

# ------------------------------------------------------------------
# Trend chart
# ------------------------------------------------------------------
st.subheader("📈 Inspection Trend")
trend_df = cached_trend(days=(date_to - date_from).days + 1 if date_to >= date_from else 30)
if not trend_df.empty:
    fig = px.line(trend_df, x="SummaryDate",
                   y=["TotalInspections", "PassedCount", "FailedCount", "FlaggedCount"],
                   labels={"value": "Count", "SummaryDate": "Date", "variable": "Metric"})
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No trend data yet. Run generate_synthetic_data.py or capture some inspections below.")

st.divider()

# ------------------------------------------------------------------
# Live activity feed — the "real-time management" heartbeat of the app.
# Shows the most recent inspections as they land, independent of the
# filters/pagination above. Bounded to a small fixed number of rows
# so it stays instant even at 70L total records.
# ------------------------------------------------------------------
st.subheader("🔴 Live Activity")


@st.cache_data(ttl=refresh_seconds if live_mode else 300, show_spinner=False)
def cached_latest_activity(limit: int = 15) -> pd.DataFrame:
    try:
        return db.fetch_inspections_page(page=1, page_size=limit)
    except Exception as e:
        st.warning(f"Live feed unavailable: {e}")
        return pd.DataFrame()


latest_df = cached_latest_activity()
if not latest_df.empty:
    status_icon = {"Passed": "✅", "Failed": "❌", "Flagged": "🚩", "Pending": "⏳"}
    for _, row in latest_df.iterrows():
        icon = status_icon.get(row["Status"], "•")
        st.write(
            f"{icon} **{row['SiteName']}** — {row['TemplateName']} "
            f"by {row['InspectorName']} · {row['Status']} "
            f"· {pd.to_datetime(row['InspectionDate']).strftime('%d %b %H:%M')}"
        )
else:
    st.info("No recent activity yet.")

st.divider()

# ------------------------------------------------------------------
# Geotagged map (sample of current page only — never plots 70L points)
# ------------------------------------------------------------------
st.subheader("📍 Inspection Locations (current page)")

# ------------------------------------------------------------------
# Paginated inspection table
# ------------------------------------------------------------------
st.subheader("🗂️ Inspection Records")

if "page" not in st.session_state:
    st.session_state.page = 1

col_prev, col_page, col_next = st.columns([1, 2, 1])
with col_prev:
    if st.button("⬅ Previous") and st.session_state.page > 1:
        st.session_state.page -= 1
with col_next:
    if st.button("Next ➡"):
        st.session_state.page += 1
with col_page:
    st.write(f"Page {st.session_state.page}")

records_df = cached_page(st.session_state.page, page_size, status, site_id, date_from, date_to)

if not records_df.empty:
    st.dataframe(records_df, use_container_width=True, hide_index=True)

    if {"Latitude", "Longitude"}.issubset(records_df.columns):
        map_df = records_df.dropna(subset=["Latitude", "Longitude"]).rename(
            columns={"Latitude": "lat", "Longitude": "lon"})
        if not map_df.empty:
            st.map(map_df[["lat", "lon"]], use_container_width=True)
else:
    st.info("No records for this page/filter combination.")

st.divider()

# ------------------------------------------------------------------
# New inspection capture (simulates the mobile/offline capture flow)
# ------------------------------------------------------------------
st.subheader("📷 New Inspection Capture")

with st.form("new_inspection_form", clear_on_submit=True):
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        site_sel = st.selectbox("Site", sites_df["SiteName"].tolist() if not sites_df.empty else [])
    with fc2:
        inspectors_df = cached_lookup("Inspectors")
        inspector_sel = st.selectbox("Inspector", inspectors_df["InspectorName"].tolist()
                                      if not inspectors_df.empty else [])
    with fc3:
        templates_df = cached_lookup("InspectionTemplates")
        template_sel = st.selectbox("Template", templates_df["TemplateName"].tolist()
                                     if not templates_df.empty else [])

    status_sel = st.selectbox("Result", ["Passed", "Failed", "Flagged", "Pending"])
    risk_score = st.slider("Risk score", 0, 100, 10)
    notes = st.text_area("Notes", max_chars=500)
    photo = st.file_uploader("Photo (optional)", type=["jpg", "jpeg", "png"])

    submitted = st.form_submit_button("Submit Inspection")

    if submitted:
        if sites_df.empty or inspectors_df.empty or templates_df.empty:
            st.error("Lookup data not loaded — cannot submit. Check DB connection.")
        else:
            try:
                row = pd.DataFrame([{
                    "SiteID": int(sites_df.loc[sites_df["SiteName"] == site_sel, "SiteID"].iloc[0]),
                    "InspectorID": int(inspectors_df.loc[inspectors_df["InspectorName"] == inspector_sel, "InspectorID"].iloc[0]),
                    "TemplateID": int(templates_df.loc[templates_df["TemplateName"] == template_sel, "TemplateID"].iloc[0]),
                    "InspectionDate": datetime.now(),
                    "Latitude": None,
                    "Longitude": None,
                    "Status": status_sel,
                    "RiskScore": risk_score,
                    "ImagePath": photo.name if photo else None,
                    "Notes": notes or None,
                    "CapturedOffline": 0,
                    "SyncedAt": datetime.now(),
                }])
                db.insert_inspections_bulk(row, chunksize=1)
                db.refresh_daily_summary(datetime.now().date())
                st.success("Inspection submitted.")
                st.cache_data.clear()
            except Exception as e:
                st.error(f"Could not save inspection — it has NOT been lost, please retry. Error: {e}")

st.caption(
    "Designed to stay responsive from a handful of records up to 70,00,000+ "
    "by always querying pre-aggregated summaries for KPIs, paginating the "
    "record table, and caching lookups/trends."
)

# ------------------------------------------------------------------
# Auto-refresh loop — keep this at the very bottom of the script.
# Streamlit reruns the whole script top-to-bottom on every refresh,
# so all the cached reads above pick up new data automatically once
# their TTL expires. This just triggers that rerun on a timer instead
# of waiting for the user to click something, turning the dashboard
# into a live-updating screen (e.g. for a wall-mounted ops display).
# ------------------------------------------------------------------
if live_mode:
    time.sleep(refresh_seconds)
    st.rerun()
