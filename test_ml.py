"""Quick E2E test for the refactored decision engine."""
import requests
import json

URL = "http://localhost:5000"
CSV_PATH = r"c:\Users\Akansh Saini\Downloads\report (85).csv"

print("=== Testing /api/analyze ===")
with open(CSV_PATH, 'rb') as f:
    resp = requests.post(f"{URL}/api/analyze", files={'file': ('report.csv', f, 'text/csv')})

print(f"Status: {resp.status_code}")
if resp.status_code != 200:
    print(f"Error: {resp.json()}")
    exit(1)

data = resp.json()
print(f"Rows processed: {data['rows_processed']}")
print(f"Total segments basic: {data['total_segments_basic']}")
print(f"Total segments detailed: {data['total_segments_detailed']}")
print(f"Compressed basic rows: {len(data['basic'])}")
print(f"Compressed detailed rows: {len(data['detailed'])}")
print(f"Insights: {len(data['insights'])}")

print("\n--- Basic results (Device-Country tab) ---")
for i, r in enumerate(data['basic'][:5], 1):
    ad = r.get('ad_unit', '')[:50]
    print(f"\n[{i}] {ad}")
    print(f"    {r.get('country', '')} / {r.get('device', '')}")
    print(f"    Current: ${r.get('current_floor', 0):.2f} -> Suggested: ${r.get('suggested_floor', 0):.2f}")
    print(f"    Direction: {r.get('change_direction', '')} ({r.get('change_pct', 0):.1f}%)")
    print(f"    Confidence: {r.get('confidence', '')}")
    print(f"    Reason: {r.get('reason', '')}")

print("\n--- Detailed results (Device-Country-Browser-OS tab) ---")
for i, r in enumerate(data['detailed'][:5], 1):
    ad = r.get('ad_unit', '')[:50]
    print(f"\n[{i}] {ad}")
    print(f"    {r.get('country', '')} / {r.get('device', '')} / {r.get('browser', '')} / {r.get('os', '')}")
    print(f"    Current: ${r.get('current_floor', 0):.2f} -> Suggested: ${r.get('suggested_floor', 0):.2f}")
    print(f"    Direction: {r.get('change_direction', '')} ({r.get('change_pct', 0):.1f}%)")
    print(f"    Confidence: {r.get('confidence', '')}")
    print(f"    Reason: {r.get('reason', '')}")

print("\n--- Testing /api/download ---")
dl_resp = requests.post(f"{URL}/api/download",
                        json={'basic': data['basic'], 'detailed': data['detailed']})
print(f"Download Status: {dl_resp.status_code}")
print(f"Downloaded file size: {len(dl_resp.content)} bytes")

import pandas as pd
import io
df_dl = pd.read_csv(io.BytesIO(dl_resp.content))
print(f"Downloaded CSV rows: {len(df_dl)}")
print(f"Columns: {df_dl.columns.tolist()}")
print(df_dl.head(10).to_string(index=False))
