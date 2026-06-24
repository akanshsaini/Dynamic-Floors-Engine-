import sqlite3, pandas as pd

conn = sqlite3.connect('ml_telemetry.db')

print("=== SEGMENTS WHERE eCPM < 0.8x FLOOR (FLOOR TOO HIGH) ===")
too_high = pd.read_sql("""
    SELECT ad_unit, country, device, current_floor, 
           ROUND(raw_rev/raw_imps*1000, 2) as ecpm,
           ROUND(raw_rev/raw_imps*1000 / current_floor, 2) as ratio,
           raw_imps, ROUND(raw_rev, 2) as raw_rev,
           change_direction, suggested_floor, reason
    FROM floor_recommendations
    WHERE upload_id=65 AND granularity='basic' 
          AND current_floor > 0
          AND raw_rev/raw_imps*1000 / current_floor < 0.8
    ORDER BY raw_imps DESC
    LIMIT 15
""", conn)
print(too_high.to_string())

print()
print("=== THE CORE PROBLEM ===")
# Count segments by what the single-floor step logic WOULD do
ratio_data = pd.read_sql("""
    SELECT current_floor, raw_imps, raw_rev,
           ROUND(raw_rev/raw_imps*1000, 2) as ecpm,
           ROUND(raw_rev/raw_imps*1000 / current_floor, 2) as ratio,
           change_direction, suggested_floor, reason
    FROM floor_recommendations
    WHERE upload_id=65 AND granularity='basic' AND current_floor > 0
""", conn)

ratio_data['ratio'] = ratio_data['ecpm'] / ratio_data['current_floor']
print(f"Total segments: {len(ratio_data)}")
print(f"  ratio < 0.8 (floor too high, should decrease): {len(ratio_data[ratio_data['ratio'] < 0.8])}")
print(f"  ratio 0.8-1.05 (slightly high, step down): {len(ratio_data[(ratio_data['ratio'] >= 0.8) & (ratio_data['ratio'] <= 1.05)])}")
print(f"  ratio 1.05-1.2 (near optimal): {len(ratio_data[(ratio_data['ratio'] > 1.05) & (ratio_data['ratio'] < 1.2)])}")
print(f"  ratio 1.2-1.5 (room to increase): {len(ratio_data[(ratio_data['ratio'] >= 1.2) & (ratio_data['ratio'] < 1.5)])}")
print(f"  ratio >= 1.5 (big room to increase): {len(ratio_data[ratio_data['ratio'] >= 1.5])}")
print()
print(f"Yet only {len(ratio_data[ratio_data['change_direction'] == 'Decrease'])} decreases and {len(ratio_data[ratio_data['change_direction'] == 'Increase'])} increases recommended!")
print(f"1306 segments trapped in '5% no-change band'")

print()
print("=== WHY THE 5% BAND TRAPS EVERYTHING ===")
# With aggressiveness=0.73, steps are tiny
agg = 0.73  # average aggressiveness
print(f"Average ML aggressiveness: {agg}")
print(f"  step_up_major = 10% * {agg} = {10*agg:.1f}%")
print(f"  step_up_minor = 5% * {agg} = {5*agg:.1f}%")
print(f"  step_down_major = 10% * (1.5 - {agg}) = {10*(1.5-agg):.1f}%")
print(f"  NO_CHANGE_BAND = 5%")
print(f"  => Effective increase window: {10*agg - 5:.1f}% to {10*agg:.1f}% (barely above the band!)")
print(f"  => For a $7.50 floor: suggested must differ by at least ${7.50*0.05:.2f} to escape the band")
print(f"     But step_up_minor produces ${7.50*(1+0.05*agg):.2f} (delta=${7.50*0.05*agg:.2f})")
print(f"     5% band requires delta >= ${7.50*0.05:.2f}")
print(f"     ${7.50*0.05*agg:.2f} < ${7.50*0.05:.2f} => TRAPPED IN NO-CHANGE BAND!")

conn.close()
