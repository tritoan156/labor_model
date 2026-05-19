# Labor Capacity Tool

A Streamlit web app for labor capacity planning across three manufacturing facilities: Henderson, Spartanburg, and Cypress.

## Features

- **Multi-location support:** Filter production schedules by facility
- **Real-time calculations:** Compute labor demand, capacity utilization, and bottlenecks
- **Battery scheduling:** Optimize battery production across parallel cells
- **8 interactive dashboards:**
  - Overview
  - Capacity vs Demand
  - Battery Throughput
  - Battery Allocation
  - Mitigation options
  - Floor Verification
  - Cycle Time
  - Source Data

## Quick Start

### Local

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

### Online (Streamlit Cloud)

Deployed at: https://[your-app].streamlit.app

## Data

- `data/machine_clean.csv` — Machine (FG SKU) labor by station
- `data/acc_clean.csv` — Accessory SKU labor by station
- `data/may_schedule.csv` — Sample production schedule

Upload your own schedule CSV with columns:
- `LOCATION` (Henderson, Spartanburg, or Cypress)
- `PRODUCTION MONTH`
- `FG SKU ID`
- `FG ACCRY SKU ID`
- `BUILD QTY`

## Configuration

Edit `core/constants.py` to adjust:
- Station headcount, concurrent bays, crew size
- Battery defaults and cycle times
- Final Assembly labor by unit class
- Battery count overrides for specific SKUs
