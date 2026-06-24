import sqlite3, pandas as pd
conn = sqlite3.connect('ml_telemetry.db')
print('=== UPLOADS LOG ===')
uploads = pd.read_sql('SELECT id, filename, timestamp, rows_processed FROM uploads ORDER BY id DESC LIMIT 5', conn)
print(uploads.to_string(index=False))

print('\n=== OVERALL DB VOLUME ===')
vol = pd.read_sql('SELECT COUNT(*) as recs, COUNT(DISTINCT upload_id) as uploads FROM floor_recommendations', conn)
print(f"Total Uploads Analysed: {vol['uploads'][0]}")
print(f"Total Segments Tracked: {vol['recs'][0]}")

print('\n=== ML KNOWLEDGE MAP (Cross-upload geo volatility) ===')
q = '''
SELECT 
    country, 
    COUNT(DISTINCT upload_id) as uploads_seen,
    AVG(raw_imps) as avg_imps,
    AVG(raw_rev) as avg_rev,
    1.0 * SUM(CASE WHEN change_direction='Increase' THEN 1 ELSE 0 END) / COUNT(*) as bump_ratio
FROM floor_recommendations
WHERE granularity='basic' 
GROUP BY country
HAVING uploads_seen >= 3
ORDER BY avg_rev DESC
LIMIT 5
'''
geo_stats = pd.read_sql(q, conn)
print(geo_stats.to_string(index=False))

conn.close()
