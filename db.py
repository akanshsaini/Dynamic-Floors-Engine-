import sqlite3
import datetime
import os
import pandas as pd
import numpy as np
import logging
import re

DB_PATH = 'ml_telemetry.db'

# Only the most recent N uploads feed the model-tuning aggregations. This bounds query
# cost no matter how large the telemetry DB grows, and recent data is more relevant for
# elasticity/volatility than years-old behavior.
RECENT_UPLOADS = 60
# Hard retention cap. Older uploads are pruned so the telemetry file can't grow forever.
# Kept well above RECENT_UPLOADS so the learning loop still has ample history.
KEEP_UPLOADS = 250
_RECENT_UPLOAD_IDS = f"(SELECT id FROM uploads ORDER BY id DESC LIMIT {RECENT_UPLOADS})"
_RECENT_FILTER = f"upload_id IN {_RECENT_UPLOAD_IDS}"
_RECENT_FILTER_OUTCOMES = f"followup_upload_id IN {_RECENT_UPLOAD_IDS}"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Track each upload/analysis session
    c.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            rows_processed INTEGER,
            signature TEXT
        )
    ''')
    # Back-fill the signature column for databases created before dedup support.
    try:
        cols = [r[1] for r in c.execute('PRAGMA table_info(uploads)').fetchall()]
        if 'signature' not in cols:
            c.execute('ALTER TABLE uploads ADD COLUMN signature TEXT')
    except Exception as e:
        logging.warning(f"Could not ensure uploads.signature column: {e}")

    # Track granular segment performance and the AI's recommendation
    c.execute('''
        CREATE TABLE IF NOT EXISTS floor_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER,
            granularity TEXT, -- 'basic' or 'detailed'
            ad_unit TEXT,
            country TEXT,
            device TEXT,
            browser TEXT,
            os TEXT,
            ssp TEXT,
            current_floor REAL,
            suggested_floor REAL,
            change_direction TEXT,
            change_pct REAL,
            confidence TEXT,
            reason TEXT,
            raw_imps INTEGER,
            raw_rev REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(upload_id) REFERENCES uploads(id)
        )
    ''')

    # Track whether prior recommendations worked when a later upload provides
    # the next observed performance for the same segment.
    c.execute('''
        CREATE TABLE IF NOT EXISTS recommendation_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id INTEGER,
            source_upload_id INTEGER,
            followup_upload_id INTEGER,
            granularity TEXT,
            ad_unit TEXT,
            country TEXT,
            device TEXT,
            ssp TEXT,
            predicted_direction TEXT,
            applied_direction TEXT,
            actual_direction TEXT,
            applied INTEGER,
            was_correct INTEGER,
            baseline_floor REAL,
            suggested_floor REAL,
            followup_floor REAL,
            baseline_imps INTEGER,
            baseline_rev REAL,
            baseline_ecpm REAL,
            followup_imps INTEGER,
            followup_rev REAL,
            followup_ecpm REAL,
            rev_change_pct REAL,
            ecpm_change_pct REAL,
            imp_change_pct REAL,
            evaluated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(recommendation_id, followup_upload_id),
            FOREIGN KEY(recommendation_id) REFERENCES floor_recommendations(id),
            FOREIGN KEY(source_upload_id) REFERENCES uploads(id),
            FOREIGN KEY(followup_upload_id) REFERENCES uploads(id)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_outcomes_segment ON recommendation_outcomes(country, device, predicted_direction)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_outcomes_upload ON recommendation_outcomes(followup_upload_id, source_upload_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_outcomes_applied ON recommendation_outcomes(applied)')
    # Indexes for the hot aggregation queries run on every analyze
    c.execute('CREATE INDEX IF NOT EXISTS idx_fr_gran_imps ON floor_recommendations(granularity, raw_imps)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_fr_upload ON floor_recommendations(upload_id, granularity)')

    conn.commit()
    conn.close()
    logging.info("ML Telemetry Database (SQLite) initialized successfully.")

def prune_old_telemetry(keep=KEEP_UPLOADS, vacuum=False):
    """
    Keep only the most recent `keep` uploads (plus their recommendations & outcomes).
    Bounds the telemetry file so it can't grow without limit. Without VACUUM the freed
    pages are reused by future inserts (size plateaus); pass vacuum=True for a one-time
    shrink of the existing file.
    """
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        row = c.execute(
            'SELECT id FROM uploads ORDER BY id DESC LIMIT 1 OFFSET ?', (keep,)
        ).fetchone()
        if not row:
            conn.close()
            return 0  # fewer than `keep` uploads — nothing to prune
        cutoff = row[0]
        c.execute('DELETE FROM recommendation_outcomes WHERE source_upload_id <= ? OR followup_upload_id <= ?', (cutoff, cutoff))
        c.execute('DELETE FROM floor_recommendations WHERE upload_id <= ?', (cutoff,))
        c.execute('DELETE FROM uploads WHERE id <= ?', (cutoff,))
        removed = conn.total_changes
        conn.commit()
        if vacuum:
            conn.execute('VACUUM')
        conn.close()
        if removed:
            logging.info(f"Pruned telemetry: removed {removed} rows for uploads <= {cutoff}.")
        return removed
    except Exception as e:
        logging.error(f"Telemetry prune failed: {e}")
        return 0


def _upload_signature(filename, rows_processed, basic_full):
    """Content fingerprint so the same report uploaded twice can't pollute telemetry."""
    try:
        imps = float(basic_full.get('_raw_imps', pd.Series(dtype=float)).sum()) if not basic_full.empty else 0.0
        rev = float(basic_full.get('_raw_rev', pd.Series(dtype=float)).sum()) if not basic_full.empty else 0.0
    except Exception:
        imps, rev = 0.0, 0.0
    return f"{filename}|{int(rows_processed)}|{round(imps, 2)}|{round(rev, 2)}"


def log_analysis(filename, rows_processed, basic_full, detailed_full):
    """
    Store the complete analysis pipeline results into SQLite for future ML training.
    Returns (upload_id, is_duplicate). Duplicate uploads reuse the existing id and
    skip re-inserting recommendations so the learning loop isn't double-counted.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # ── Duplicate guard ──────────────────────────────────────────────────
        signature = _upload_signature(filename, rows_processed, basic_full)
        existing = c.execute(
            'SELECT id FROM uploads WHERE signature = ? ORDER BY id DESC LIMIT 1',
            (signature,)
        ).fetchone()
        if existing:
            conn.close()
            logging.info(f"Duplicate upload detected (signature match, id={existing[0]}). Skipping telemetry insert.")
            return existing[0], True

        # Log upload
        c.execute('INSERT INTO uploads (filename, rows_processed, signature) VALUES (?, ?, ?)',
                  (filename, rows_processed, signature))
        upload_id = c.lastrowid
        
        # Helper to insert dataframe records
        def insert_records(df, granularity):
            if df.empty:
                return
            
            records = []
            for _, row in df.iterrows():
                records.append((
                    upload_id,
                    granularity,
                    str(row.get('ad_unit', '')),
                    str(row.get('country', '')),
                    str(row.get('device', '')),
                    str(row.get('browser', '')),
                    str(row.get('os', '')),
                    str(row.get('ssp', '')),
                    float(row.get('current_floor', 0.0)),
                    float(row.get('suggested_floor', 0.0)),
                    str(row.get('change_direction', '')),
                    float(row.get('change_pct', 0.0)),
                    str(row.get('confidence', '')),
                    str(row.get('reason', '')),
                    int(row.get('_raw_imps', 0)),
                    float(row.get('_raw_rev', 0.0))
                ))
            
            c.executemany('''
                INSERT INTO floor_recommendations (
                    upload_id, granularity, ad_unit, country, device, browser, os, ssp,
                    current_floor, suggested_floor, change_direction, change_pct, 
                    confidence, reason, raw_imps, raw_rev
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', records)

        # Only 'basic' rows feed the learning loop (elasticity, knowledge, outcomes all
        # query granularity='basic'). The detailed frame was never read back, so we no
        # longer persist it — this is the main driver of telemetry-file growth.
        insert_records(basic_full, 'basic')

        conn.commit()
        conn.close()
        logging.info(f"Successfully logged analysis data to database (Upload ID: {upload_id})")
        prune_old_telemetry()  # keep the telemetry file bounded
        return upload_id, False
    except Exception as e:
        logging.error(f"Failed to log data to DB: {e}")
        return None, False

def _pct_change(new_value, old_value):
    old_value = float(old_value or 0)
    new_value = float(new_value or 0)
    if old_value == 0:
        return 1.0 if new_value > 0 else 0.0
    return (new_value - old_value) / old_value

def _dominant_floor(seg_df):
    floors_with_data = seg_df[seg_df['floor'] > 0]
    if floors_with_data.empty:
        return 0.0
    floor_imps = floors_with_data.groupby('floor')['impressions'].sum()
    return float(floor_imps.idxmax())

def _normalize_ad_unit_key(ad_unit):
    key = str(ad_unit or '').lower().strip()
    key = re.sub(r'\s+', '_', key)
    key = re.sub(r'\(\d+\)', '', key).strip('_')
    key = re.sub(r'^(?:z1|ellipsis)_dfp_', 'dfp_', key)
    return key

def _current_segment_metrics(df):
    key_cols = ['ad_unit', 'country', 'device', 'ssp']
    if df.empty or any(c not in df.columns for c in key_cols):
        return {}

    metrics = {}
    for keys, seg in df.groupby(key_cols, observed=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        key = (
            _normalize_ad_unit_key(keys[0]),
            str(keys[1]),
            str(keys[2]),
            str(keys[3]),
        )
        imps = float(seg['impressions'].sum())
        rev = float(seg['revenue'].sum())
        if imps <= 0:
            continue
        metrics[key] = {
            'followup_floor': _dominant_floor(seg),
            'followup_imps': int(imps),
            'followup_rev': rev,
            'followup_ecpm': (rev / imps) * 1000,
        }
    return metrics

def _floor_direction(baseline_floor, followup_floor):
    baseline_floor = float(baseline_floor or 0)
    followup_floor = float(followup_floor or 0)
    if baseline_floor <= 0:
        return 'Increase' if followup_floor > 0 else 'No change'
    if followup_floor > baseline_floor * 1.05:
        return 'Increase'
    if followup_floor < baseline_floor * 0.95:
        return 'Decrease'
    return 'No change'

def _close_to_floor(actual_floor, target_floor):
    actual_floor = float(actual_floor or 0)
    target_floor = float(target_floor or 0)
    if target_floor <= 0:
        return actual_floor <= 0.05
    return abs(actual_floor - target_floor) / target_floor <= 0.08

def _classify_outcome(predicted, applied, rev_change, ecpm_change, imp_change):
    if not applied:
        return 'Not applied', 0

    if predicted == 'Increase':
        # Success only if revenue did not drop significantly AND eCPM increased or held steady
        success = rev_change >= -0.05 and ecpm_change >= 0.01
        return ('Increase' if success else 'No change'), int(success)

    if predicted == 'Decrease':
        # Success only if revenue did not drop significantly AND impressions increased
        success = rev_change >= -0.05 and imp_change >= 0.01
        return ('Decrease' if success else 'No change'), int(success)

    stable = abs(ecpm_change) <= 0.05 and rev_change >= -0.05
    if stable:
        return 'No change', 1
    return ('Increase' if ecpm_change > 0.05 else 'Decrease'), 0

def _detect_upload_network(c, upload_id, df=None):
    if df is not None and not df.empty and 'ad_unit' in df.columns:
        for au in df['ad_unit'].head(10):
            if str(au).startswith('z1_'):
                return 'z1'
            if str(au).startswith('ellipsis_'):
                return 'ellipsis'
    
    # Check recommendation table
    row = c.execute('SELECT ad_unit FROM floor_recommendations WHERE upload_id = ? LIMIT 5', (upload_id,)).fetchone()
    if row:
        au = str(row[0])
        if au.startswith('z1_'):
            return 'z1'
        if au.startswith('ellipsis_'):
            return 'ellipsis'
    return 'unknown'

def learn_from_followup_upload(followup_upload_id, df):
    """
    Use the newly uploaded report as the observed follow-up for the previous upload's
    recommendations. Only applied recommendations are counted as training outcomes.
    """
    summary = {
        'source_upload_id': None,
        'followup_upload_id': followup_upload_id,
        'evaluated': 0,
        'applied': 0,
        'correct': 0,
        'accuracy': None,
        'stored': 0,
    }
    if not followup_upload_id:
        return summary

    metrics = _current_segment_metrics(df)
    if not metrics:
        return summary

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Determine network of followup upload and find the previous upload of the same network
        current_net = _detect_upload_network(c, followup_upload_id, df)
        source_upload_id = None
        
        if current_net != 'unknown':
            prev_uploads = c.execute('SELECT id FROM uploads WHERE id < ? ORDER BY id DESC', (followup_upload_id,)).fetchall()
            for (p_id,) in prev_uploads:
                p_net = _detect_upload_network(c, p_id)
                if p_net == current_net:
                    source_upload_id = p_id
                    break
        else:
            row = c.execute('SELECT MAX(id) FROM uploads WHERE id < ?', (followup_upload_id,)).fetchone()
            source_upload_id = row[0] if row else None
            
        summary['source_upload_id'] = source_upload_id
        if not source_upload_id:
            conn.close()
            return summary

        recs = pd.read_sql('''
            SELECT id, upload_id, granularity, ad_unit, country, device, ssp,
                   current_floor, suggested_floor, change_direction, raw_imps, raw_rev
            FROM floor_recommendations
            WHERE upload_id=? AND granularity="basic" AND raw_imps > 0
        ''', conn, params=(source_upload_id,))

        records = []
        for _, rec in recs.iterrows():
            key = (
                _normalize_ad_unit_key(rec.get('ad_unit', '')),
                str(rec.get('country', '')),
                str(rec.get('device', '')),
                str(rec.get('ssp', '')),
            )
            observed = metrics.get(key)
            if not observed:
                continue

            predicted = str(rec.get('change_direction', 'No change'))
            baseline_floor = float(rec.get('current_floor', 0.0) or 0.0)
            suggested_floor = float(rec.get('suggested_floor', 0.0) or 0.0)
            followup_floor = float(observed['followup_floor'])
            applied_direction = _floor_direction(baseline_floor, followup_floor)

            baseline_imps = int(rec.get('raw_imps', 0) or 0)
            baseline_rev = float(rec.get('raw_rev', 0.0) or 0.0)
            baseline_ecpm = (baseline_rev / baseline_imps * 1000) if baseline_imps > 0 else 0.0

            rev_change = _pct_change(observed['followup_rev'], baseline_rev)
            ecpm_change = _pct_change(observed['followup_ecpm'], baseline_ecpm)
            imp_change = _pct_change(observed['followup_imps'], baseline_imps)

            # Self-match guard: consecutive uploads often cover OVERLAPPING report windows
            # (e.g. two 30-day exports pulled days apart), so the "followup" aggregates are
            # nearly identical to the baseline. That is the same window measured twice, not
            # an outcome — storing it floods the loop with fake 0% results (this was why
            # every per-segment outcome read 0.0%). Skip these entirely.
            if abs(rev_change) < 0.005 and abs(imp_change) < 0.005 and abs(ecpm_change) < 0.005:
                continue

            if predicted == 'No change':
                applied = applied_direction == 'No change'
            else:
                # An Increase/Decrease only counts as applied when the floor GENUINELY moved
                # in the predicted direction (>=5% via _floor_direction). A floor sitting
                # still is a non-event, not evidence about the recommendation.
                applied = (applied_direction == predicted) or (
                    _close_to_floor(followup_floor, suggested_floor)
                    and applied_direction != 'No change')
            actual_direction, was_correct = _classify_outcome(
                predicted, applied, rev_change, ecpm_change, imp_change
            )

            records.append((
                int(rec['id']),
                int(rec['upload_id']),
                int(followup_upload_id),
                str(rec.get('granularity', 'basic')),
                key[0], key[1], key[2], key[3],
                predicted,
                applied_direction,
                actual_direction,
                int(applied),
                int(was_correct),
                baseline_floor,
                suggested_floor,
                followup_floor,
                baseline_imps,
                baseline_rev,
                baseline_ecpm,
                int(observed['followup_imps']),
                float(observed['followup_rev']),
                float(observed['followup_ecpm']),
                rev_change * 100,
                ecpm_change * 100,
                imp_change * 100,
            ))

        summary['evaluated'] = len(records)
        summary['applied'] = sum(r[11] for r in records)
        summary['correct'] = sum(r[12] for r in records if r[11])
        summary['accuracy'] = (summary['correct'] / summary['applied']) if summary['applied'] else None

        before = c.execute(
            'SELECT COUNT(*) FROM recommendation_outcomes WHERE followup_upload_id=?',
            (followup_upload_id,)
        ).fetchone()[0]

        c.executemany('''
            INSERT OR IGNORE INTO recommendation_outcomes (
                recommendation_id, source_upload_id, followup_upload_id, granularity,
                ad_unit, country, device, ssp, predicted_direction, applied_direction,
                actual_direction, applied, was_correct, baseline_floor, suggested_floor,
                followup_floor, baseline_imps, baseline_rev, baseline_ecpm, followup_imps,
                followup_rev, followup_ecpm, rev_change_pct, ecpm_change_pct, imp_change_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', records)

        conn.commit()
        after = c.execute(
            'SELECT COUNT(*) FROM recommendation_outcomes WHERE followup_upload_id=?',
            (followup_upload_id,)
        ).fetchone()[0]
        conn.close()
        summary['stored'] = max(0, int(after - before))
        logging.info(
            "Learning pass stored %s outcomes (%s applied, %s correct).",
            summary['stored'], summary['applied'], summary['correct']
        )
        return summary
    except Exception as e:
        logging.error(f"Learning pass failed: {e}")
        return summary

def extract_ml_knowledge():
    """
    Analyzes historical database to calculate observed volatility per country/device.
    Returns a dictionary of conservative aggressiveness multipliers.
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql(f'''
            SELECT upload_id, country, device, current_floor, raw_imps, raw_rev
            FROM floor_recommendations
            WHERE granularity="basic" AND raw_imps > 50 AND {_RECENT_FILTER}
        ''', conn)
        outcomes = pd.read_sql('''
            SELECT country, device, predicted_direction, was_correct
            FROM recommendation_outcomes
            WHERE applied=1
        ''', conn)
        conn.close()

        if df.empty:
            return {}

        # Per-segment outcome accuracy (vectorized)
        outcome_stats = {}
        if not outcomes.empty:
            grp = outcomes.groupby(['country', 'device'])
            acc_all = grp['was_correct'].agg(['mean', 'count'])
            acc_inc = outcomes[outcomes['predicted_direction'] == 'Increase'].groupby(['country', 'device'])['was_correct'].mean()
            acc_dec = outcomes[outcomes['predicted_direction'] == 'Decrease'].groupby(['country', 'device'])['was_correct'].mean()
            for key, row in acc_all.iterrows():
                outcome_stats[key] = {
                    'applied_outcomes': int(row['count']),
                    'outcome_accuracy': float(row['mean']),
                    'increase_accuracy': float(acc_inc[key]) if key in acc_inc.index else None,
                    'decrease_accuracy': float(acc_dec[key]) if key in acc_dec.index else None,
                }

        # ── Vectorized eCPM stability per Geo+Device ───────────────────────────────
        # Step 1: collapse to one weighted eCPM per (upload, country, device).
        per_upload = df.groupby(['country', 'device', 'upload_id'], observed=True).agg(
            imps=('raw_imps', 'sum'), rev=('raw_rev', 'sum'), max_floor=('current_floor', 'max')
        ).reset_index()
        per_upload = per_upload[per_upload['imps'] > 0]
        per_upload['ecpm'] = per_upload['rev'] / per_upload['imps'] * 1000
        # Step 2: aggregate those per-upload values to one profile per (country, device).
        seg = per_upload.groupby(['country', 'device'], observed=True).agg(
            avg_ecpm=('ecpm', 'mean'), std_ecpm=('ecpm', 'std'),
            avg_imps=('imps', 'mean'), max_floor=('max_floor', 'max'),
            history_count=('upload_id', 'nunique')
        )

        knowledge = {}
        for (c, d), row in seg.iterrows():
            avg_ecpm = float(row['avg_ecpm']) if pd.notna(row['avg_ecpm']) else 0.0
            std_ecpm = float(row['std_ecpm']) if pd.notna(row['std_ecpm']) else 0.0
            max_floor = float(row['max_floor']) if pd.notna(row['max_floor']) else 0.0
            avg_imps = float(row['avg_imps']) if pd.notna(row['avg_imps']) else 0.0
            total_seen = int(row['history_count'])

            # Volatility Aggressiveness
            volatility = (std_ecpm / avg_ecpm) if avg_ecpm > 0 else 0.3
            aggressiveness = 1.0
            
            if volatility < 0.20:
                aggressiveness = 1.25  # Stable traffic -> Push 25% harder
            elif volatility > 0.60:
                aggressiveness = 0.75  # Volatile traffic -> Push 25% softer
 
            stats = outcome_stats.get((c, d), {})
            if stats.get('applied_outcomes', 0) >= 3:
                accuracy = stats.get('outcome_accuracy', 0.0)
                if accuracy >= 0.70:
                    aggressiveness *= 1.10
                elif accuracy < 0.45:
                    aggressiveness *= 0.85
 
                inc_acc = stats.get('increase_accuracy')
                if inc_acc is not None:
                    if inc_acc >= 0.70:
                        aggressiveness *= 1.08
                    elif inc_acc < 0.45:
                        aggressiveness *= 0.85
 
            aggressiveness = max(0.75, min(1.35, aggressiveness))
 
            knowledge[f"{c}_{d}"] = {
                'avg_ecpm': avg_ecpm,
                'max_historic_floor': max_floor,
                'aggressiveness': round(aggressiveness, 2),
                'history_count': total_seen,
                'observation_count': total_seen,
                'applied_outcomes': stats.get('applied_outcomes', 0),
                'outcome_accuracy': stats.get('outcome_accuracy'),
                'avg_imps': avg_imps
            }
        return knowledge
    except Exception as e:
        logging.error(f"ML Knowledge Extraction Failed: {e}")
        return {}

def get_learning_summary():
    """
    Return cumulative feedback-loop stats for the UI and report insights.
    """
    empty = {
        'total_outcomes': 0,
        'applied_outcomes': 0,
        'correct_outcomes': 0,
        'accuracy': None,
        'confusion_matrix': [],
    }
    if not os.path.exists(DB_PATH):
        return empty

    try:
        conn = sqlite3.connect(DB_PATH)
        totals = pd.read_sql('''
            SELECT
                COUNT(*) AS total_outcomes,
                SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) AS applied_outcomes,
                SUM(CASE WHEN applied=1 AND was_correct=1 THEN 1 ELSE 0 END) AS correct_outcomes
            FROM recommendation_outcomes
        ''', conn).iloc[0]
        confusion = pd.read_sql('''
            SELECT predicted_direction, actual_direction, COUNT(*) AS n
            FROM recommendation_outcomes
            WHERE applied=1
            GROUP BY predicted_direction, actual_direction
            ORDER BY predicted_direction, actual_direction
        ''', conn)
        conn.close()

        applied = int(totals.get('applied_outcomes') or 0)
        correct = int(totals.get('correct_outcomes') or 0)
        confusion_records = []
        for _, row in confusion.iterrows():
            confusion_records.append({
                'predicted_direction': str(row.get('predicted_direction', '')),
                'actual_direction': str(row.get('actual_direction', '')),
                'n': int(row.get('n') or 0),
            })

        return {
            'total_outcomes': int(totals.get('total_outcomes') or 0),
            'applied_outcomes': applied,
            'correct_outcomes': correct,
            'accuracy': (correct / applied) if applied else None,
            'confusion_matrix': confusion_records,
        }
    except Exception as e:
        logging.error(f"Learning summary failed: {e}")
        return empty


def extract_learned_elasticity():
    """
    Derive real price elasticity from applied outcomes instead of a hard-coded rule.

    elasticity = -(% impression change) / (% floor change) for floor INCREASES, and the
    symmetric volume-recapture coefficient for DECREASES. Returned per country_device with
    a global fallback under the key '__global__'. Values are clamped to sane bounds so a
    couple of noisy outcomes can't produce absurd projections.
    """
    default = {'__global__': {'increase': 0.5, 'decrease': 0.3, 'n': 0}}
    if not os.path.exists(DB_PATH):
        return default
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql(f'''
            SELECT country, device, baseline_floor, followup_floor,
                   imp_change_pct, rev_change_pct
            FROM recommendation_outcomes
            WHERE applied = 1 AND baseline_floor > 0 AND followup_floor > 0
              AND {_RECENT_FILTER_OUTCOMES}
        ''', conn)
        conn.close()
        if df.empty:
            return default

        df['floor_chg'] = (df['followup_floor'] - df['baseline_floor']) / df['baseline_floor']
        df = df[df['floor_chg'].abs() >= 0.02]  # ignore effectively-unchanged floors
        if df.empty:
            return default

        # elasticity of fill w.r.t. floor: how much volume moved per 1% floor move
        df['elasticity'] = (df['imp_change_pct'] / 100.0) / df['floor_chg']

        def _coeffs(sub):
            inc = sub[sub['floor_chg'] > 0]
            dec = sub[sub['floor_chg'] < 0]
            # For increases, impressions should fall -> elasticity negative; store its magnitude.
            inc_e = float(np.clip(-inc['elasticity'].median(), 0.05, 2.0)) if len(inc) >= 3 else None
            # For decreases, impressions should rise -> elasticity also negative; store magnitude.
            dec_e = float(np.clip(-dec['elasticity'].median(), 0.05, 2.0)) if len(dec) >= 3 else None
            return inc_e, dec_e

        out = {}
        g_inc, g_dec = _coeffs(df)
        out['__global__'] = {
            'increase': g_inc if g_inc is not None else 0.5,
            'decrease': g_dec if g_dec is not None else 0.3,
            'n': int(len(df)),
        }
        for (c, d), sub in df.groupby(['country', 'device']):
            inc_e, dec_e = _coeffs(sub)
            if inc_e is None and dec_e is None:
                continue
            out[f"{c}_{d}"] = {
                'increase': inc_e if inc_e is not None else out['__global__']['increase'],
                'decrease': dec_e if dec_e is not None else out['__global__']['decrease'],
                'n': int(len(sub)),
            }
        return out
    except Exception as e:
        logging.error(f"Elasticity extraction failed: {e}")
        return default


def get_cut_outcome_bias():
    """
    Per-segment memory of how FLOOR CUTS actually performed (measured, not modeled).
    For each (ad_unit, country, device) where the engine previously decreased the floor and
    it was applied, return the median realized revenue change. This is the do-no-harm signal:
    where cutting demonstrably lost revenue (e.g. premium sites whose price collapsed), the
    engine must stop cutting; where it won, the engine can press harder. Keyed the same way
    the learning loop stores outcomes (normalized ad_unit + country + device).
    Returns { (ad_unit, country, device): median_rev_change_pct }.
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        # Comparable-window guard: sequential uploads cover shifting date ranges, so raw
        # revenue deltas are inflated by traffic drift. Only score outcomes where volume
        # stayed comparable (|imp change| <= 60%) and price didn't silently collapse.
        df = pd.read_sql(f'''
            SELECT ad_unit, country, device, rev_change_pct
            FROM recommendation_outcomes
            WHERE applied = 1 AND predicted_direction = 'Decrease'
              AND ABS(imp_change_pct) <= 60 AND ecpm_change_pct > -30
              AND {_RECENT_FILTER_OUTCOMES}
        ''', conn)
        conn.close()
        if df.empty:
            return {}
        g = df.groupby(['ad_unit', 'country', 'device'])['rev_change_pct'].median()
        return {(a, c, d): float(v) for (a, c, d), v in g.items()}
    except Exception as e:
        logging.error(f"Cut-outcome bias query failed: {e}")
        return {}


def get_realized_uplift():
    """
    MEASURED (not modeled) uplift from applied recommendations. Aggregates the real
    before/after numbers stored in recommendation_outcomes so the UI can report the
    actual revenue and RPM lift the engine delivered.
    """
    empty = {
        'applied_segments': 0,
        'baseline_rev': 0.0, 'followup_rev': 0.0, 'rev_uplift_pct': None,
        'baseline_ecpm': 0.0, 'followup_ecpm': 0.0, 'ecpm_uplift_pct': None,
        'win_rate': None,
    }
    if not os.path.exists(DB_PATH):
        return empty
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql(f'''
            SELECT baseline_rev, followup_rev, baseline_imps, followup_imps, was_correct
            FROM recommendation_outcomes
            WHERE applied = 1 AND {_RECENT_FILTER_OUTCOMES}
        ''', conn)
        conn.close()
        if df.empty:
            return empty

        b_rev = float(df['baseline_rev'].sum())
        f_rev = float(df['followup_rev'].sum())
        b_imps = float(df['baseline_imps'].sum())
        f_imps = float(df['followup_imps'].sum())
        b_ecpm = (b_rev / b_imps * 1000) if b_imps > 0 else 0.0
        f_ecpm = (f_rev / f_imps * 1000) if f_imps > 0 else 0.0

        return {
            'applied_segments': int(len(df)),
            'baseline_rev': round(b_rev, 2),
            'followup_rev': round(f_rev, 2),
            'rev_uplift_pct': round((f_rev - b_rev) / b_rev * 100, 1) if b_rev > 0 else None,
            'baseline_ecpm': round(b_ecpm, 2),
            'followup_ecpm': round(f_ecpm, 2),
            'ecpm_uplift_pct': round((f_ecpm - b_ecpm) / b_ecpm * 100, 1) if b_ecpm > 0 else None,
            'win_rate': round(float(df['was_correct'].mean()) * 100, 1),
        }
    except Exception as e:
        logging.error(f"Realized uplift query failed: {e}")
        return empty
