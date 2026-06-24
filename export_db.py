import sqlite3
import pandas as pd
import os

DB_PATH = 'ml_telemetry.db'

def export():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    
    # Export uploads
    df_uploads = pd.read_sql('SELECT * FROM uploads', conn)
    df_uploads.to_csv('historic_uploads.csv', index=False)
    
    # Export floor recommendations
    df_recs = pd.read_sql('SELECT * FROM floor_recommendations', conn)
    
    # Provide a nicely formatted timestamp
    if 'timestamp' in df_recs.columns:
        df_recs['timestamp'] = pd.to_datetime(df_recs['timestamp'])
        
    df_recs.to_csv('historic_ml_data.csv', index=False)
    conn.close()
    
    print(f"\n=============================================")
    print(f"SUCCESS! Database Exported to CSV.")
    print(f"=============================================")
    print(f"-> Created 'historic_uploads.csv' ({len(df_uploads)} rows)")
    print(f"-> Created 'historic_ml_data.csv' ({len(df_recs)} rows)")
    print(f"Open these in Excel to view your raw ML telemetry data!\n")

if __name__ == '__main__':
    export()
