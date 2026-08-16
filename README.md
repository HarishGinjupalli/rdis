# rDIS — Real Time Digital Inspection System

A Streamlit + SQL Server dashboard for capturing, storing, and analyzing
inspection data (geotagged, AI-assisted, offline-capable), built to stay
fast and stable from a handful of records up to **70,00,000+ (70 lakh)**
inspection rows.

## Real-time behaviour

- **Live Mode** toggle in the sidebar (on by default) auto-refreshes the whole
  dashboard on a timer (5–60s, your choice) — KPIs, trend chart, and a
  **Live Activity** feed of the most recent inspections update without the
  user clicking anything, like a wall-mounted ops screen.
- The 🟢 Live / 🔴 Offline badge next to the title reflects the DB health
  check on every refresh, so a connection drop is visible immediately
  instead of the app silently going stale.
- Turn Live Mode off if you're just browsing historical data — it stops the
  auto-refresh loop so it won't fight with manual filtering.

## Why this won't crash at scale

| Risk at 70L rows | How this project avoids it |
|---|---|
| Dashboard freezes computing COUNT/SUM over 70L rows on every page load | KPIs are read from a pre-aggregated `DailySummary` table (`usp_RefreshDailySummary`), which is O(days), not O(rows) |
| `st.dataframe` trying to render millions of rows at once | The record table is **paginated** server-side with `OFFSET...FETCH`, always bounded to `page_size` rows |
| Slow filters/sorts on unindexed columns | Every filterable/sortable column (`InspectionDate`, `SiteID`, `InspectorID`, `Status`) has an index |
| App crashing on a dropped DB connection | `pool_pre_ping=True` + a `@retry` decorator around every DB call + a health-check banner instead of an unhandled exception |
| Slow/failed bulk inserts (e.g. syncing offline devices) | Batched inserts (`chunksize`) with `fast_executemany=True`, 10–50x faster than row-by-row |
| Streamlit re-querying SQL Server on every widget click | `st.cache_data` on lookups/KPIs/trend, with short TTLs so data still stays fresh |

## Project structure

```
rDIS_Project/
├── database/
│   ├── schema.sql                  # tables, indexes, summary table, stored proc
│   └── generate_synthetic_data.py  # load-test data generator, up to 70L rows
├── app/
│   ├── db_utils.py                 # pooled connections, retries, paginated queries
│   └── dashboard.py                # Streamlit UI
├── requirements.txt
└── README.md
```

## Setup

1. Install SQL Server (Express is fine) and the ODBC Driver 17 for SQL Server.
2. Run `database/schema.sql` in SSMS to create the database, tables, and stored procedure.
3. `pip install -r requirements.txt`
4. Edit the connection settings at the top of `app/db_utils.py` (server name, auth mode).
5. (Optional) Load test data:
   ```
   python database/generate_synthetic_data.py --rows 100000
   ```
   Scale this up to `--rows 7000000` once you've confirmed everything works —
   generate in stages (100K → 1M → 7M) and watch the index/page performance
   at each step rather than jumping straight to 70L.
6. Launch the dashboard:
   ```
   streamlit run app/dashboard.py
   ```

## Notes on the 70 lakh target

"70 lakh" wasn't specified as total-rows vs daily-throughput when this was
built, so the design handles **both**:
- As **total stored records**: indexing + pagination + the summary table
  keep queries fast regardless of table size.
- As **daily real-time load**: batched bulk inserts + connection pooling
  mean the ingestion path doesn't bottleneck on a single row-by-row insert
  loop.

If your real number turns out to be "70 lakh rows/day," consider adding
table partitioning by month (schema.sql has a commented-out starting point)
once you're actually operating at that volume.
