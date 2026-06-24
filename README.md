# Dynamic Floors Engine

A bid-landscape-driven dynamic floor price optimizer for ad units / tags / sites.
Upload a performance report (and optionally a bid-range report) and get
revenue-maximizing floor recommendations, with a learning loop that measures the
actual revenue / RPM uplift of applied changes over time.

## Run

```bat
run.bat
```

Opens `http://localhost:5000`. Drop a performance CSV (and optionally a bid-landscape
CSV) and click **Analyze**.

## How it works

- **RPM-primary yield optimization** — optimizes revenue-per-request, not just eCPM.
- **Bid-landscape optimizer** — when a bid-range report is provided, sets the floor that
  maximizes the posted-price revenue curve `f · P(bid ≥ f)`, i.e. with the demand rather
  than above it. Confidence-scaled move size; clean `$0.25` increments with a hard minimum.
- **Learned price elasticity, significance gating, outlier winsorization** and a
  **duplicate-upload guard** keep recommendations grounded and trustworthy.
- **Learning loop** — stores each upload's recommendations and scores them against the next
  upload to report *measured* (not modeled) revenue / RPM uplift.

## Stack

Flask + pandas + numpy, vanilla JS front end (no build step). SQLite telemetry
(`ml_telemetry.db`, gitignored — regenerated locally).

## Layout

- `app.py` — Flask app, data cleaning, floor engine, routes
- `db.py` — SQLite telemetry, learning loop, elasticity & uplift queries
- `templates/index.html`, `static/style.css`, `static/script.js` — UI
- `run.bat` — set up venv, install deps, launch
