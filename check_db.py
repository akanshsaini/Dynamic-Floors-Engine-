import sqlite3
import pandas as pd

conn = sqlite3.connect('ml_telemetry.db')

# Get all uploads
uploads = pd.read_sql("SELECT * FROM uploads ORDER BY id DESC", conn)
print("=== ALL UPLOADS ===")
print(uploads.to_string())

# Get latest upload id
latest = uploads.iloc[0]['id']
print(f"\nLatest upload_id: {latest}")

# Group ad units by site (extract site name from ad_unit)
import re
units = pd.read_sql(f"""
    SELECT ad_unit, 
           SUM(raw_imps) as imps, 
           SUM(raw_rev) as rev,
           ROUND(SUM(raw_rev)/SUM(raw_imps)*1000, 2) as ecpm
    FROM floor_recommendations 
    WHERE upload_id={latest} AND granularity='basic'
    GROUP BY ad_unit ORDER BY rev DESC
""", conn)

# Extract site names
def extract_site(au):
    # patterns: ellipsis_dfp_v_SITE_vN_v_xxx or z1_dfp_v_SITE_v_xxx
    m = re.search(r'(?:ellipsis|z1)_dfp_v_(.+?)_v\d+', au)
    if m:
        return m.group(1).replace('_', '.')
    m = re.search(r'(?:ellipsis|z1)_dfp_v_(.+?)_v_', au)
    if m:
        return m.group(1).replace('_', '.')
    return au

units['site'] = units['ad_unit'].apply(extract_site)
units['gam_seat'] = units['ad_unit'].apply(lambda x: 'z1' if x.startswith('z1_') else 'ellipsis')

print("\n=== AD UNITS WITH SITE GROUPING ===")
print(units.to_string())

print("\n=== AGGREGATED BY SITE ===")
site_agg = units.groupby(['site', 'gam_seat']).agg(
    ad_units=('ad_unit', 'count'),
    total_imps=('imps', 'sum'),
    total_rev=('rev', 'sum'),
).reset_index()
site_agg['ecpm'] = (site_agg['total_rev'] / site_agg['total_imps'] * 1000).round(2)
site_agg = site_agg.sort_values('total_rev', ascending=False)
print(site_agg.to_string())

# How many total uploads per site historically
print("\n=== HISTORICAL DEPTH PER SITE (all uploads) ===")
all_units = pd.read_sql("""
    SELECT ad_unit, COUNT(DISTINCT upload_id) as uploads_seen,
           SUM(raw_imps) as total_imps, SUM(raw_rev) as total_rev
    FROM floor_recommendations WHERE granularity='basic'
    GROUP BY ad_unit ORDER BY total_rev DESC
""", conn)
all_units['site'] = all_units['ad_unit'].apply(extract_site)
hist_sites = all_units.groupby('site').agg(
    ad_units=('ad_unit', 'nunique'),
    max_uploads=('uploads_seen', 'max'),
    total_imps=('total_imps', 'sum'),
    total_rev=('total_rev', 'sum'),
).reset_index()
hist_sites['ecpm'] = (hist_sites['total_rev'] / hist_sites['total_imps'] * 1000).round(2)
hist_sites = hist_sites.sort_values('total_rev', ascending=False)
print(hist_sites.to_string())

# Check distinct sites
print(f"\n=== TOTAL DISTINCT SITES: {hist_sites['site'].nunique()} ===")

conn.close()
