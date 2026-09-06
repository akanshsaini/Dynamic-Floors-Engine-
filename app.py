"""
Ad Floor Suggestions — Clean Rewrite
Decision engine: deterministic, data-grounded, UPR-ready output.
UI and Excel export are guaranteed to show identical floors.
"""

import datetime as _dt
import hashlib
import io
import json
import logging
import math
import os
import re
import traceback

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_file, render_template
from dotenv import load_dotenv
import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
db.init_db()

@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response

# ─── Constants ────────────────────────────────────────────────────────────────
MIN_IMPS_CANDIDATE  = 10      # Minimum impressions for a floor candidate to be evaluated
MIN_IMPS_SEGMENT    = 50      # Minimum impressions for a segment to get a direct recommendation
MAX_CHANGE_PCT      = 0.50    # Never suggest more than ±50% from current floor
NO_CHANGE_BAND      = 0.02    # Treat as no-change if suggested is within 2% of current (revamped from 5%)
WEAK_GAIN_THRESHOLD = 0.03    # Must improve the target metric by at least 3% to recommend an increase
IMP_DROP_THRESHOLD  = 0.30    # Reject if impressions drop >30% with negligible yield gain
INITIAL_FLOOR_RATIO = 0.70    # When no floor is set, recommend 70% of cleared eCPM
MAX_OUTPUT_ROWS     = None    # None = unlimited; return every actionable recommendation
FLOOR_STEP          = 0.25    # Floors are always snapped to this currency increment
Z_SIGNIFICANCE      = 1.96    # ~95% confidence gate for declaring a metric gap is real (not noise)

# Fallback price elasticity (fill-drop fraction per 1.0 fractional floor change) used only
# until the DB has ≥3 applied outcomes to learn a real per-segment elasticity.
DEFAULT_ELASTICITY  = {'increase': 0.5, 'decrease': 0.3}
MIN_BIDS_LANDSCAPE  = 200      # Minimum bids in a segment before we trust its bid-landscape floor
# Do-no-harm controller thresholds (measured revenue change of prior applied CUTS):
CUT_LOSS_PCT        = -2.0     # prior cuts lost >2% revenue -> stop cutting this segment
CUT_WIN_PCT         = 2.0      # prior cuts gained >2% -> press toward the bid optimum
MAX_DOWN_UNPROVEN   = 0.35     # cap a cut at -35% until measured outcomes justify going deeper
PROBE_PCT           = 0.12     # unproven changes ship as ±12% probes the next upload can score
# Exploration budget (manual-anchor mode): to beat — not just match — manual, deviate a
# rotating slice of segments off the manual floor, measure, and promote winners.
EXPLORE_EVERY       = 8        # ~1/8 = 12.5% of anchored segments explore each week
EXPLORE_PROBE       = 0.12     # ±12% probe around the manual floor

# ── REVENUE-WEIGHTED EXPLORATION BUDGET (2026-08-13) ─────────────────────────
# Exploration used to be uniform-random (hash-only), so a segment earning
# $0.001 got the same optimization budget as one earning $44/day. Measured
# distribution over a 30-day report:
#     zero1     33 segments (1.5%) carry 83% of revenue; ~2,000 carry <2%
#     ellipsis  19 segments (1.3%) carry 73% of revenue; ~1,390 carry <2%
# Probing the long tail cannot pay — the revenue is too small to measure a
# result against, and every probe still consumes blast-radius budget and adds
# churn. So the cadence now scales with revenue contribution.
# Thresholds are TOTAL segment revenue over the report window (~30 days, the
# standing GAM schedule); retune here if that window changes.
EXPLORE_REV_HIGH    = 10.00    # top ~1.5% of segments, ~75-83% of revenue
EXPLORE_REV_MID     = 1.00     # top ~5%, ~95% of revenue
EXPLORE_REV_LOW     = 0.25     # top ~8-10%, ~98% of revenue
EXPLORE_EVERY_HIGH  = 2        # big earners: explore ~every other week
EXPLORE_EVERY_LOW   = 24       # small earners: rarely
# below EXPLORE_REV_LOW -> never explore; hold the proven manual floor

# ── STAGED WIDENING (2026-08-12) ─────────────────────────────────────────────
# Three independent measurements agree that floors sit too HIGH:
#   1. bid landscape — the posted-price optimum is BELOW manual on 88% (zero1) /
#      93% (ellipsis) of segments; 94–96% need a move bigger than ±12%;
#      floors currently clear just 1.8% / 0.2% of measured bids
#   2. A/B scoreboard — engine runs ~12% under manual and is at parity (zero1) /
#      +10.7% ad-req CPM (ellipsis)
#   3. outcome telemetry — RAISING loses (median −8.4%, bootstrap CI excludes 0);
#      cutting is neutral-to-positive
# So the leash is widened ASYMMETRICALLY: cuts may go deeper as evidence accrues,
# raises stay pinned near manual. We deliberately do NOT jump to the computed
# optimum — that formula assumes the winner pays our floor, which is false in a
# real auction, so it is an upper bound, not a forecast. Instead we step toward
# it, measure, and let promotion ratchet.
# ── PER-SITE ANCHOR DISCOUNT (2026-08-16) ────────────────────────────────────
# Anchoring to manual assumes the variant tag faces the SAME demand curve as the
# control tag. On socialnieuws_nl it demonstrably does not: at an IDENTICAL floor
# ($9.25, NL/High-end/mid1) manual fills 0.496% while the variant fills 0.370%,
# costing $8.14 in a week on that one segment. Measured across both networks over
# 7 days:
#     engine floor ABOVE manual  -> 78 segments, -$41.71
#     engine floor BELOW manual  -> 31 segments,  +$7.01
# Per-site seed discount: seed BELOW the manual anchor where a site is measured to
# under-fill at manual's price. Keyed by site (bid_join_key prefix); absent = no discount.
#
# 2026-08-21: socialnieuws_nl REMOVED. The 20% discount shipped 2026-08-16 and did not
# work — over the 9-day even-split window (Aug 12-20) the tag still ran -7.6% RPM and,
# decisively, delivered 22.4% FEWER impressions than manual off equal pageviews. A lower
# floor that fills LESS means price was never the binding constraint, so discounting
# further cannot fix it. Site moved to SITE_POLICY 'manual' below pending an ad-ops
# look at the fill gap. Keep this dict — the mechanism is sound, this site just wasn't
# the right diagnosis.
SITE_ANCHOR_DISCOUNT = {}

# Per-site policy. 'manual' pins every segment on that site to the proven human floor:
# no probes, no promoted deviations, no discount. Use it where a measured, equal-traffic
# A/B says the engine cannot beat the desk on that site — holding is then the
# revenue-maximising choice, not a retreat. Reviewed against each fresh even-split window;
# a site earns its way back out by the same evidence that put it here.
# Measured over Aug 12-20 2026 (9 days, 50/50 split, mix-adjusted per day x site):
SITE_POLICY = {}
# 2026-09-07: weetjewijzer_nl and socialnieuws_nl UNPINNED.
#
# They were pinned on 2026-08-21 on nine days of even-split evidence (weetjewijzer ahead
# 0 of 9, socialnieuws 2 of 9). That evidence is void. It was measured while the
# per-ad-unit engine was colliding at tag level, and the collision had a systematic
# UPWARD bias — a floor computed for an expensive position (pre, mid1) landed on the tag
# and applied to the cheap ones too. Every live floor sat far above its anchor:
#
#   live floor vs manual anchor, measured 2026-09-07 on the pre-redesign rule set
#     socialnieuws_nl   zero1 +229%   ellipsis +138%
#     weetjewijzer_nl   zero1 +157%   ellipsis +150%
#
# socialnieuws was 138% above anchor while nominally "pinned to manual", so the pin never
# actually held: what lost was not engine pricing, it was a corrupted floor. Both sites
# have to be re-measured now that the engine decides at the deployable key and the pin
# can mean what it says.
#
# Keep the MECHANISM — it is the right tool when a site genuinely cannot be beaten. Re-pin
# only on evidence from a clean even-split window under the tag-level engine (earliest
# 2026-09-09, since 2026-09-07 was priced by the old floors until 03:00).
# Left on normal learning, deliberately:
#   paparazzi_ar       +12.8% RPM, 9 of 9 days, +$36.73 — the engine's best proof.
#   1point3acres        +6.7% RPM, 7 of 9 days, +$18.80.
#   filmpjevandedag_nl  -6.0% RPM, 4 of 9 days,  -$3.33 — daily swing is +17%..-24% on
#     the smallest revenue base we have; 4/9 is not separable from noise. Pinning it
#     would be acting on variance. Watch, do not touch.


def _site_policy(anchor_key):
    """Policy for the site an anchor key belongs to, or None. Prefix match on site."""
    site = str(anchor_key).split('|')[0]
    for _site, _pol in SITE_POLICY.items():
        if site.startswith(_site):
            return _pol
    return None

ANCHOR_BAND_UP      = 0.12     # raises stay TIGHT — measured to lose money
ANCHOR_BAND_DOWN    = 0.45     # proven cuts may ratchet to −45% below manual
CUT_RAMP_FULL_PCT   = 10.0     # cut_bias at/above this earns the full band depth
DEEP_CUT_EVERY      = 16       # ~1/16 = 6.25% of segments get a DEEP cut probe/week
DEEP_CUT_MIN        = 0.30     # a deep probe cuts at least 30% below manual
DEEP_CUT_MAX        = 0.45     # ...and never more than 45% in one step
DEEP_CUT_MAX_P      = 0.05     # only where the floor clears <5% of measured bids
FLOOR_MIN_ABS       = 0.05     # never ship a floor below this, whatever the math says
# The desk's 1%..1.5% match band is a HEURISTIC, not a law, and our own data rejects it.
# Measured on the 2026-08-27 report (30-day window, true match rate = responses/requests):
# revenue per 1,000 ad requests rises MONOTONICALLY with match rate on every single site —
# there is no interior peak to steer into. Not one site peaks in 1%..1.5%.
#
#   yield per 1k requests    1point3acres  filmpje  paparazzi  socialnieuws  weetjewijzer
#     match 0.8-1.0%               0.069     0.062      0.011         0.023         0.033
#     match 1.0-1.5% (the band)    0.133     0.063      0.018         0.095         0.039
#     match 2.0-3.0%               0.237     0.182      0.021         0.165         0.160
#     match 5-10%                      ·         ·      0.026             ·         0.686
#
# Spearman of match rate vs yield: +0.22..+0.65 per site. Critically it also holds WITHIN
# ad unit x country groups (median +0.535, positive in 81.4% of 429 groups), so it is not
# merely "good inventory fills more" — controlling for the inventory, more fill still pays.
# Yield at the top fill decile runs 7x-68x the bottom decile.
#
# Consequence: there is no upper trigger. Match rate above the old band is NOT a reason to
# raise a floor; the evidence points the other way. The signal is used in one direction
# only — to size and prioritise CUTS where fill is worst.
MATCH_LOW           = 0.010    # retained: legacy direction hint in the explore rung
MATCH_HIGH          = 0.015
MATCH_OVERRIDE      = True     # set False to disable the match-driven cut entirely
MATCH_MIN_REQS      = 200      # ignore segments too small for the rate to mean anything
MATCH_CUT_MAX       = 0.35     # deepest cut this signal may earn on its own
# Fire only on SEVERE under-fill — below this fraction of the site target.
# Ungated it hits 98.6% of eligible segments and 95% of revenue: that is not an override,
# it is a blanket repricing, and because this rung outranks the probes it would also stop
# the engine exploring on almost everything and starve the outcome telemetry that is our
# ground truth. Gated at 0.33 it hits 75% of segments but only 20.7% of revenue, because
# badly under-filled segments are by definition the ones earning least (the worst-filled
# half of the estate carries 6.8% of revenue). So the gate targets exactly where the
# upside is and the downside is bounded, and leaves high-revenue near-target segments to
# the measured cut/raise loop. Raise toward 1.0 to widen once this is measured.
MATCH_SEVERE_RATIO  = 0.33

# ─── BID-LANDSCAPE OPTIMUM AS A FIRST-CLASS SIGNAL ─────────────────────────────
# We fetch ~85 MB of bid-range report per network per day and it decided 4 of 107
# floors (3.7%, measured 2026-08-21) — because the landscape was only consulted in
# the deep-probe rung, gated behind a 1-in-16 weekly slice AND p<5% AND seg_rev>=$1.
# Coverage was never the problem: 99.6% of all bids already sit in a key with enough
# volume to compute an optimum. The data was simply never asked.
#
# So consult it every run, wherever there are enough bids to trust it. It stays BELOW
# measured outcomes — argmax f*P(bid>=f) is a model, and its revenue assumption (the
# winner pays our floor) is false in a live auction, so a realised revenue result
# outranks it. And the move toward it is clamped to the same asymmetric bands as
# everything else: cuts may run deep, raises stop at +12%, because the model biases
# high and raising is measured to lose.
BID_OPT_ENABLED     = True
BID_OPT_MIN_MOVE    = 0.10     # ignore optima within 10% of the anchor — pure churn
# Per-site fill target: the level at/above which that site's yield stops improving in the
# measured data. Below target the floor is provably leaving money on the table.
# DELIBERATELY STATIC, not re-derived per run: cutting raises fill, which would raise an
# auto-derived target, which would justify deeper cuts — a runaway loop. Re-derive by hand
# from a fresh report (see the table above) and review the change.
MATCH_TARGET_DEFAULT = 0.020
MATCH_TARGET = {
    '1point3acres':       0.025,   # yield still climbing at 2-3%, 33.6% of its revenue there
    'filmpjevandedag_nl': 0.025,   # climbing through 2-3%
    'socialnieuws_nl':    0.025,   # peaks 2-3%, collapses beyond — the one site with a top
    'paparazzi_ar':       0.030,   # best bucket 5-10% but thin; 3% is the supported level
    'weetjewijzer_nl':    0.050,   # best bucket 5-10% (moot while the site is pinned)
}

# ─── Column Detection ──────────────────────────────────────────────────────────
COLUMN_ALIASES = {
    'ad_unit':     ['ad unit', 'ad_unit', 'tag', 'placement', 'slot', 'ad unit (all levels)'],
    'country':     ['country', 'geo', 'country name'],
    'device':      ['device', 'device type', 'device category'],
    'browser':     ['browser', 'browser name', 'browser category'],
    'os':          ['os', 'operating system', 'platform', 'operating system category'],
    'ssp':         ['ssp', 'exchange', 'yield partner', 'ssp/exchange'],
    'floor':       ['floor', 'floor price', 'price floor', 'pricing rule'],
    'impressions': ['impressions', 'matched impressions', 'ad exchange impressions'],
    'revenue':     ['revenue', 'estimated revenue', 'ad exchange revenue ($)', 'ad exchange revenue'],
    'requests':    ['requests', 'ad requests', 'ad exchange ad requests',
                    'ad exchange total requests', 'total requests', 'eligible impressions'],
    'date':        ['date', 'date/time', 'day'],
    'day_of_week': ['day of week', 'day_of_week', 'weekday'],
    # ── Bid-landscape signals (used when present; engine falls back gracefully) ──
    'responses':   ['responses', 'bid responses', 'ad exchange responses served', 'responses served'],
    'unfilled':    ['unfilled impressions', 'unmatched ad requests', 'unfilled', 'unmatched impressions'],
    'avg_bid_cpm': ['average bid cpm', 'avg bid cpm', 'bid cpm', 'mean bid cpm'],
    'bids':        ['bids', 'bid count', 'total bids', 'number of bids'],
}
REQUIRED = ['ad_unit', 'country', 'device', 'floor', 'impressions', 'revenue']


def detect_columns(df):
    col_map = {}
    lower_cols = {c.lower().strip(): c for c in df.columns}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower_cols:
                col_map[target] = lower_cols[alias]
                break
    return col_map


# ─── Data Cleaning ─────────────────────────────────────────────────────────────
def extract_floor(val):
    """Extract numeric floor from values like 'Price Rule $3.5 TCPM' or '(No pricing rule applied)'."""
    if pd.isna(val):
        return 0.0
    val = str(val).strip()
    if not val or val == '(No pricing rule applied)' or val == 'Price Rule Google OPT':
        return 0.0
    m = re.search(r'\$([\d.]+)', val)
    if m:
        return float(m.group(1))
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def clean_numeric(series):
    if series.dtype in (np.float64, np.int64, float, int):
        return pd.to_numeric(series, errors='coerce').fillna(0)
    return pd.to_numeric(
        series.astype(str).str.replace(',', '', regex=False)
                          .str.replace('%', '', regex=False)
                          .str.replace('$', '', regex=False)
                          .str.strip(),
        errors='coerce'
    ).fillna(0)


def clean_ad_unit(val):
    """Strip parent names and numeric IDs: 'Parent » Child (12345)' -> 'Child'."""
    parts = re.split(r'[»›>\xbb\ufffd]', str(val))
    last = parts[-1].strip()
    clean = re.sub(r'\(\d+\)', '', last).strip()
    return clean if clean else last




# ─── TAG-LEVEL MODE ────────────────────────────────────────────────────────────
# The dashboard cannot store a floor per ad unit. Uploads go in as "Tag-Specific
# Rule": it resolves the ad-unit name to its TAG and stores one rule per
#     tag x country x device x os x browser
# Measured 2026-08-31: the engine was deciding 6,466 floors that collapsed into
# 1,328 storable rules — 4.9 decisions per rule, disagreeing in 74% of cases with a
# median spread of $2.50 and a worst case of $17.55. Whichever ad unit uploaded last
# silently set the price for all the others.
#
# So the engine now decides AT the deployable key. Collapsing ad_unit to the tag
# identity in load_and_clean propagates everywhere for free, because segmentation,
# the bid join, the anchor lookup, the learning loop and the export all key off
# ad_unit. It also repairs the learning loop: outcomes used to be attributed per ad
# unit while the floor that produced them was a tag-level collision, so cut_bias was
# being trained on mislabelled experiments.
TAG_LEVEL = True
_POS_RE = re.compile(r'_(mid|pre|post).*$')
_TAG_REP = {}          # tag identity -> a real ad-unit name the dashboard can resolve


def tag_key(ad_unit):
    """The deployable identity: site/tag, with ad position and instance stripped.
    Idempotent — tag_key(tag_key(x)) == tag_key(x) — so it is safe to re-apply."""
    return _POS_RE.sub('', bid_join_key(ad_unit))


def bid_join_key(ad_unit):
    """
    Normalize an ad-unit name to a stable site+position key for joining the performance
    report to the bid-landscape report. The two GAM reports name the same inventory
    differently — the version token (v107 / fixed) and trailing instance number differ:
        z1_dfp_v_1point3acres_v107_v_mid4_1   (performance)
        z1_dfp_v_1point3acres_fixed_v_mid4_7  (bid landscape)
    Both must reduce to '1point3acres_mid4'. We strip the network prefix, drop the version
    token, and drop the trailing instance index, keeping site + ad position.
    """
    s = str(ad_unit).lower().strip()
    s = re.sub(r'^[a-z0-9]+_dfp_v_', '', s)          # strip z1_dfp_v_ / ellipsis_dfp_v_
    parts = s.split('_v_', 1)                          # site[_version] | position[_instance]
    site = re.sub(r'_(v\d+|fixed|v)$', '', parts[0])   # drop version token if present
    pos = parts[1] if len(parts) > 1 else ''
    pos = re.sub(r'_\d+$', '', pos)                    # drop trailing instance number
    key = f'{site}_{pos}'.strip('_')
    return key or s


def map_device(val):
    v = str(val).lower().strip()
    if 'smartphone' in v or 'mobile' in v or 'high-end' in v or 'phone' in v:
        return 'High-end'
    if 'tablet' in v:
        return 'Tablet'
    if 'desktop' in v or 'computer' in v:
        return 'Desktop'
    if 'connected tv' in v or 'ctv' in v:
        return 'CTV'
    if v in ('nan', '', 'none', 'unknown'):
        return 'Other'
    return str(val).strip() if str(val).strip() else 'Other'


def map_browser(val):
    if pd.isna(val):
        return 'Other'
    v = str(val).lower().strip()
    if 'chrome' in v and 'edge' not in v: return 'Google Chrome'
    if 'safari' in v: return 'Safari'
    if 'firefox' in v: return 'Firefox'
    if 'edge' in v: return 'Microsoft Edge'
    if 'opera' in v: return 'Opera'
    if 'webview' in v or 'in-app' in v or 'in_app' in v: return 'In-app browser'
    return 'Other'


def map_os(val):
    if pd.isna(val):
        return 'Other'
    v = str(val).lower().strip()
    if 'ios' in v or 'ipad' in v or 'apple' in v: return 'Apple iOS'
    if 'android' in v: return 'Android'
    if 'windows' in v: return 'Windows'
    if 'mac' in v or 'osx' in v: return 'macOS'
    if 'linux' in v: return 'Linux'
    return 'Other'


# ─── Load & Clean CSV ──────────────────────────────────────────────────────────
def load_and_clean(file_obj):
    content = file_obj.read().decode('utf-8', errors='replace')
    # Auto-detect header row from the first ~30 lines only. Memory matters: these
    # reports can be hundreds of MB — never build a full line list or rejoin the text.
    head = content.split('\n', 31)[:30]
    header_idx = 0
    for i, line in enumerate(head):
        ll = line.lower()
        if 'impression' in ll and 'revenue' in ll:
            header_idx = i
            break

    df = pd.read_csv(io.StringIO(content), skiprows=header_idx, low_memory=False)
    del content
    col_map = detect_columns(df)
    missing = [c for c in REQUIRED if c not in col_map]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}. Found: {list(df.columns)}")

    rename = {v: k for k, v in col_map.items()}
    df = df.rename(columns=rename)

    # Apply cleaning
    df['ad_unit']     = df['ad_unit'].apply(clean_ad_unit)
    df['floor']       = df['floor'].apply(extract_floor)
    df['impressions'] = clean_numeric(df['impressions'])
    df['revenue']     = clean_numeric(df['revenue'])
    df['device']      = df['device'].apply(map_device) if 'device' in df.columns else 'Other'
    df['browser']     = df['browser'].apply(map_browser) if 'browser' in df.columns else 'Other'
    df['os']          = df['os'].apply(map_os) if 'os' in df.columns else 'Other'
    if 'ssp' not in df.columns:
        # Derive SSP from the ad-unit network prefix: the Ellipsis network trades through
        # the APAC MCM seat. (Previously everything was stamped google_mcm, which produced
        # wrong ssp values on Ellipsis export rows.)
        df['ssp'] = np.where(
            df['ad_unit'].astype(str).str.lower().str.startswith('ellipsis'),
            'google_mcm_apac', 'google_mcm')
    if 'country' not in df.columns:
        df['country'] = 'Unknown'
    if 'requests' in df.columns:
        df['requests'] = clean_numeric(df['requests'])
    if 'responses' in df.columns:
        df['responses'] = clean_numeric(df['responses'])
    if 'unfilled' in df.columns:
        df['unfilled'] = clean_numeric(df['unfilled'])

    has_requests = 'requests' in df.columns and df['requests'].sum() > 0
    has_date = 'date' in df.columns
    date_str = ""

    if has_date:
        try:
            # Parse dates and localize to PDT (GMT-7:00)
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            valid_dates = df['date'].dropna()
            if not valid_dates.empty:
                min_date = valid_dates.min().strftime('%Y-%m-%d')
                max_date = valid_dates.max().strftime('%Y-%m-%d')
                date_str = f"{min_date} to {max_date} (PDT)"
            
            if 'day_of_week' not in df.columns or df['day_of_week'].isna().all():
                df['day_of_week'] = df['date'].dt.day_name()
        except Exception as e:
            logging.warning(f"Date parsing failed: {e}")

    if 'day_of_week' in df.columns:
        df['day_of_week'] = df['day_of_week'].astype(str).str.strip().str.title()

    # Remove total/summary rows
    df = df[~df['ad_unit'].str.lower().str.startswith('total', na=False)]

    # ── COLLAPSE TO THE DEPLOYABLE KEY ───────────────────────────────────────
    # Do this BEFORE any aggregation so every downstream group-by, join and
    # telemetry key is already at tag level. Keep one real ad-unit name per tag so
    # the export can still hand the dashboard something it can resolve.
    if TAG_LEVEL and 'ad_unit' in df.columns:
        _raw = df['ad_unit'].astype(str)
        _tag = _raw.map(tag_key)
        for t, a in zip(_tag, _raw):
            if t not in _TAG_REP:
                _TAG_REP[t] = a
        _before = _raw.nunique()
        df['ad_unit'] = _tag
        logging.info('TAG_LEVEL: %d ad units collapsed to %d tags',
                     _before, df['ad_unit'].nunique())

    # TRUE match rate, computed BEFORE unfilled rows are dropped.
    # The impressions>0 filter below removes exactly the requests that did NOT fill, so
    # any ratio taken afterwards is measured only over inventory that already filled.
    # Measured 2026-08-31: post-filter imps/requests reads 91% against a true rate of
    # 1.12%, so every segment tripped the "match too high -> raise" branch in
    # _anchored_floor — permanently pushing the one direction measured to lose money.
    # GAM's own "Ad Exchange match rate" column is responses/requests (verified to
    # correlate 1.0000 with it), so reproduce that definition here per segment.
    if 'requests' in df.columns and 'responses' in df.columns:
        _keys = [c for c in ('ad_unit', 'country', 'device', 'os', 'browser') if c in df.columns]
        if _keys:
            _agg = df.groupby(_keys, observed=True)[['requests', 'responses']].sum()
            _agg['true_match_rate'] = (_agg['responses']
                                       / _agg['requests'].where(_agg['requests'] > 0))
            _agg = _agg.rename(columns={'requests': 'true_requests'})
            df = df.merge(_agg[['true_requests', 'true_match_rate']],
                          left_on=_keys, right_index=True, how='left')

    df = df[df['impressions'] > 0]
    df = df.reset_index(drop=True)

    df = winsorize_ecpm(df)

    return df, col_map, has_requests, has_date, date_str


def bid_prob_at_least(seg_bid, floor):
    """P(bid >= floor) from a segment's bid histogram (bucketed by FLOOR_STEP)."""
    hist = seg_bid.get('hist')
    total = seg_bid.get('bids', 0)
    if not hist or total <= 0:
        return None
    above = sum(b for c, b in hist.items() if c >= floor)
    return above / total


def bid_optimal_floor(seg_bid):
    """
    Posted-price revenue maximization over the bid distribution:
        floor* = argmax_f  f · P(bid >= f)
    This is the textbook optimal posted price facing a willingness-to-pay distribution —
    it puts the floor WITH the demand mass instead of above it. Candidate floors are the
    observed bid buckets (the revenue curve only kinks there). Conservative: the winner
    usually pays above the floor, so this is a lower-bound objective and slightly biases
    high, which is the safe direction for a floor.

    Returns (floor, p_at_floor, exp_rev_index) or None if too few bids.
    """
    hist = seg_bid.get('hist') if seg_bid else None
    total = float(seg_bid.get('bids', 0)) if seg_bid else 0.0
    if not hist or total < MIN_BIDS_LANDSCAPE:
        return None
    buckets = sorted(hist.items())                      # [(cpm, bids), ...] ascending
    suffix = 0.0
    best = None
    # walk high→low so suffix = bids at >= current cpm
    for cpm, bids in reversed(buckets):
        suffix += bids
        if cpm <= 0:
            continue
        p = suffix / total
        rev = cpm * p
        if best is None or rev > best[2]:
            best = (cpm, p, rev)
    return best


def load_bid_landscape(file_obj):
    """
    Parse an OPTIONAL bid-range report (per-advertiser 'Average bid CPM' + 'Bids') and
    aggregate it into a per-segment bid landscape keyed by (ad_unit, country, device).

    Reads ONLY the columns it needs (these reports are millions of rows) and does NOT drop
    impressions=0 rows — bids that never served are exactly the below-floor demand we want.
    'Average bid CPM' is aggregated BID-WEIGHTED (never a naive mean across advertisers).

    Returns (bid_map, info). Each entry: {requests, responses, bids, bid_cpm (wtd avg),
    hist {cpm_bucket: bids}} so the engine can compute P(bid>=floor) and find a floor that
    sits with the demand instead of above it.
    """
    try:
        raw = file_obj.read()
        text = raw.decode('utf-8', errors='replace')
        del raw
        head = text.split('\n', 31)[:30]
        header_idx = 0
        for i, line in enumerate(head):
            ll = line.lower()
            if ('bid' in ll or 'request' in ll or 'response' in ll) and ('ad unit' in ll or 'country' in ll or 'device' in ll):
                header_idx = i
                break

        # Detect columns from the header alone, then read only those (memory/speed).
        head_df = pd.read_csv(io.StringIO('\n'.join(head[header_idx:header_idx + 2])))
        col_map = detect_columns(head_df)
        if 'ad_unit' not in col_map:
            return {}, 'Bid landscape ignored: no ad-unit column found.'

        wanted = [k for k in ('ad_unit', 'country', 'device', 'requests', 'responses',
                              'unfilled', 'avg_bid_cpm', 'bids') if k in col_map]
        usecols = [col_map[k] for k in wanted]
        df = pd.read_csv(io.StringIO(text), skiprows=header_idx, usecols=usecols, low_memory=False)
        del text
        df = df.rename(columns={col_map[k]: k for k in wanted})

        df['ad_unit'] = df['ad_unit'].apply(clean_ad_unit).apply(bid_join_key)  # site+position key
        df['country'] = df['country'].astype(str).str.strip() if 'country' in df.columns else 'Unknown'
        df['device']  = df['device'].apply(map_device) if 'device' in df.columns else 'Other'
        for col in ('requests', 'responses', 'unfilled', 'avg_bid_cpm', 'bids'):
            if col in df.columns:
                df[col] = clean_numeric(df[col])

        has_bids = 'avg_bid_cpm' in df.columns and 'bids' in df.columns
        sum_cols = [c for c in ('requests', 'responses', 'unfilled') if c in df.columns]

        bid_map = {}
        # Sum volume metrics per segment
        if sum_cols:
            for key, row in df.groupby(['ad_unit', 'country', 'device'], observed=True)[sum_cols].sum().iterrows():
                bid_map[key] = {c: float(row[c]) for c in sum_cols}

        net_bids = 0
        if has_bids:
            bd = df[df['bids'] > 0].copy()
            bd['bucket'] = (bd['avg_bid_cpm'] / FLOOR_STEP).round() * FLOOR_STEP  # snap to $0.25
            bd['wsum'] = bd['avg_bid_cpm'] * bd['bids']
            for key, g in bd.groupby(['ad_unit', 'country', 'device'], observed=True):
                tot = float(g['bids'].sum())
                if tot <= 0:
                    continue
                entry = bid_map.setdefault(key, {})
                entry['bids'] = tot
                entry['bid_cpm'] = float(g['wsum'].sum()) / tot          # bid-weighted avg
                entry['hist'] = {float(c): float(b) for c, b in g.groupby('bucket')['bids'].sum().items()}
                net_bids += tot

        if not bid_map:
            return {}, 'Bid landscape ignored: no usable bid/request columns found.'

        extras = []
        if has_bids and net_bids:
            extras.append(f'{int(net_bids):,} bids')
        if sum_cols:
            extras.append('+'.join(sum_cols))
        info = f'Bid landscape merged: {len(bid_map):,} segments ({", ".join(extras)}).'
        logging.info(info)
        return bid_map, info
    except Exception as e:
        logging.warning(f'Bid landscape parse failed: {e}')
        return {}, f'Bid landscape could not be parsed: {e}'


def winsorize_ecpm(df):
    """
    Cap per-row eCPM at a robust upper bound so a single freak high-revenue row can't
    skew a segment's eCPM/RPM and trigger a bogus floor change. Uses the IQR rule on
    log-eCPM (revenue distributions are heavy-tailed). Revenue is rescaled to the capped
    eCPM; impressions are preserved so volume math is unaffected.
    """
    if df.empty or 'revenue' not in df.columns or 'impressions' not in df.columns:
        return df
    imps = df['impressions'].clip(lower=1)
    ecpm = df['revenue'] / imps * 1000
    valid = ecpm[ecpm > 0]
    if len(valid) < 20:
        return df  # too little data for a stable bound
    q1, q3 = np.percentile(valid, [25, 75])
    iqr = q3 - q1
    upper = q3 + 3.0 * iqr  # generous 3×IQR cap — only clips extreme outliers
    if upper <= 0:
        return df
    capped = ecpm.clip(upper=upper)
    n_clipped = int((ecpm > upper).sum())
    if n_clipped:
        df = df.copy()
        df['revenue'] = capped * imps / 1000
        logging.info(f"Winsorized {n_clipped} outlier rows above eCPM ${upper:.2f}.")
    return df


# ─── Floor Recommendation Engine ───────────────────────────────────────────────
def detect_current_floor(seg_df):
    """Return the dominant floor for this segment (by impression-weighted mode)."""
    floors_with_data = seg_df[seg_df['floor'] > 0]
    if len(floors_with_data) == 0:
        return 0.0
    floor_imps = floors_with_data.groupby('floor')['impressions'].sum()
    return float(floor_imps.idxmax())


def evaluate_floor_candidates(seg_df, has_requests):
    """
    For each floor value seen in this segment, compute aggregated performance.
    Returns {floor_value: {imps, rev, ecpm, rpm, fill_rate}}.
    """
    agg = {'imps': ('impressions', 'sum'), 'rev': ('revenue', 'sum')}
    if has_requests and 'requests' in seg_df.columns:
        agg['reqs'] = ('requests', 'sum')

    grouped = seg_df.groupby('floor').agg(**agg).reset_index()
    perf = {}
    for _, row in grouped.iterrows():
        f    = float(row['floor'])
        imps = float(row['imps'])
        rev  = float(row['rev'])
        if imps < MIN_IMPS_CANDIDATE:
            continue
        ecpm = rev / imps * 1000 if imps > 0 else 0.0
        entry = {'imps': imps, 'rev': rev, 'ecpm': ecpm}
        if has_requests and 'reqs' in row.index:
            reqs = float(row['reqs'])
            entry['rpm']       = rev / reqs * 1000 if reqs > 0 else 0.0
            entry['fill_rate'] = imps / reqs if reqs > 0 else 0.0
        perf[f] = entry
    return perf


def _significant(metric_chg, n_cur, n_cand):
    """
    Gate a metric difference against sampling noise. Treats the mean eCPM/RPM as having a
    relative standard error ~1/sqrt(n); a gap is 'real' only if it clears Z * combined SE.
    """
    n_cur = max(float(n_cur or 0), 1.0)
    n_cand = max(float(n_cand or 0), 1.0)
    noise = Z_SIGNIFICANCE * math.sqrt(1.0 / n_cur + 1.0 / n_cand)
    return abs(metric_chg) > noise


def _bid_confidence(bids):
    if bids >= MIN_BIDS_LANDSCAPE * 10: return 'High'
    if bids >= MIN_BIDS_LANDSCAPE * 3:  return 'Medium'
    return 'Low'


def recommend_floor(current_floor, perf, seg_ecpm, has_requests, ml_profile=None,
                    elasticity=None, seg_bid=None, cut_bias=None):
    """
    Core decision logic. Returns (suggested_floor, direction, change_pct, confidence, reason).

    PRIMARY signal when a bid landscape is present: set the floor that maximizes the
    posted-price revenue curve f·P(bid>=f) — i.e., with the demand, not above it. This is
    the only signal that can find genuinely revenue-maximizing floors, so it takes priority.
    Falls back to RPM-yield maximization over observed floors when no bid data exists.
    """
    if ml_profile is None: ml_profile = {}
    if elasticity is None: elasticity = DEFAULT_ELASTICITY
    e_inc = float(elasticity.get('increase', DEFAULT_ELASTICITY['increase']))
    e_dec = float(elasticity.get('decrease', DEFAULT_ELASTICITY['decrease']))
    metric_name = 'RPM' if has_requests else 'eCPM'
    aggressiveness = ml_profile.get('aggressiveness', 1.0)
    avg_imps = ml_profile.get('avg_imps', 0)
    history_count = ml_profile.get('history_count', 0)
    
    # filter perf to non-zero floors only (zero is "no rule", not a real candidate)
    real_perf = {f: p for f, p in perf.items() if f > 0}
    current_p = real_perf.get(current_floor) if real_perf else None

    # ── PRIMARY: bid-landscape posted-price optimum (when bid data exists) ──────
    # f* = argmax f·P(bid>=f) — the only signal that sees where demand actually sits, so it
    # overrides the cleared-eCPM heuristics. Move size is confidence-scaled: when there are
    # many bids and the current floor clears almost nothing, we move most of the way to the
    # optimum in one go (lowering toward proven demand is low-risk and captures revenue now);
    # thin-data segments stay capped to avoid overshooting. Raising a floor stays capped at
    # MAX_CHANGE_PCT either way, since over-flooring kills fill.
    bo = bid_optimal_floor(seg_bid)
    if bo is not None:
        opt_f, p_opt, _ = bo
        bids = float(seg_bid.get('bids', 0))
        p_cur = bid_prob_at_least(seg_bid, current_floor) if current_floor > 0 else None
        conf = _bid_confidence(bids)
        target = max(FLOOR_STEP, round(opt_f / FLOOR_STEP) * FLOOR_STEP)
        if current_floor > 0:
            wants_cut = target < current_floor
            # ── Do-no-harm controller (measured, not modeled) ──────────────────
            # The bid report can't tell a "stranded" floor from a premium floor whose
            # price would collapse if cut — only the realized outcome can. So size
            # the cut by what cutting THIS segment actually did to revenue last period.
            if wants_cut and cut_bias is not None and cut_bias <= CUT_LOSS_PCT:
                return current_floor, 'No change', 0.0, conf, (
                    f'Bid landscape suggests ${opt_f:.2f}, but measured outcomes show cutting '
                    f'this segment lost revenue ({cut_bias:+.0f}%) — holding ${current_floor:.2f} '
                    f'(price-support protected).')
            # Treat near-zero bias as NO trustworthy evidence — stay gentle until the
            # learning loop measures a real per-segment outcome from a later upload.
            has_evidence = cut_bias is not None and abs(cut_bias) >= 1.0
            if not wants_cut:
                max_down = MAX_CHANGE_PCT
            elif has_evidence and cut_bias >= CUT_WIN_PCT:
                max_down = 0.50                       # proven winner: press, but never violently
            elif has_evidence and cut_bias > CUT_LOSS_PCT:
                max_down = 0.25                       # mild positive evidence
            else:
                max_down = MAX_DOWN_UNPROVEN          # unproven/noisy: gentle, then measure
            lo = current_floor * (1 - max_down)
            hi = current_floor * (1 + MAX_CHANGE_PCT)
            target = min(hi, max(lo, target))
            target = max(FLOOR_STEP, round(target / FLOOR_STEP) * FLOOR_STEP)
            chg = (target - current_floor) / current_floor
        else:
            chg = 1.0
        if current_floor > 0 and abs(chg) < NO_CHANGE_BAND:
            return current_floor, 'No change', 0.0, conf, (
                f'Bid landscape: floor ${current_floor:.2f} already near the demand-optimal '
                f'${opt_f:.2f} ({p_opt*100:.0f}% of bids clear it).')
        direction = 'Increase' if chg > 0 else 'Decrease'
        pcur_txt = f'{p_cur*100:.0f}% of bids clear current ${current_floor:.2f}; ' if p_cur is not None else ''
        reason = (f'Bid landscape ({int(bids):,} bids): {pcur_txt}'
                  f'revenue-maximizing floor ≈ ${opt_f:.2f} where {p_opt*100:.0f}% of bids clear. '
                  f'Moving to ${target:.2f}.')
        return target, direction, abs(chg) * 100, conf, reason

    # Compute volume crash detection
    current_imps = current_p['imps'] if current_p else 0
    is_volume_crash = (history_count >= 3 and avg_imps > 150 and current_imps / avg_imps < 0.25)

    # ── CASE 0: No floor set yet ───────────────────────────────────────────────
    if current_floor == 0:
        # Use priced impressions eCPM if available, else segment eCPM
        priced = {f: p for f, p in perf.items() if f > 0}
        if priced:
            best_f = max(priced, key=lambda f: priced[f]['ecpm'])
            ecpm = priced[best_f]['ecpm']
        elif seg_ecpm and seg_ecpm > 0:
            ecpm = seg_ecpm
        else:
            return 0.0, 'No change', 0.0, 'Low', 'No eCPM data to base a floor recommendation on.'

        if ecpm < 0.5:
            return 0.0, 'No change', 0.0, 'Low', f'eCPM ${ecpm:.2f} too low to set an initial floor safely.'

        # Apply ML elasticity aggressiveness to initial ratio (baseline 70%, scaled by ML up to max 85%)
        safe_ratio = min(0.85, INITIAL_FLOOR_RATIO * aggressiveness)
        initial = max(0.25, round(ecpm * safe_ratio / 0.25) * 0.25)
        conf = 'High' if ecpm > 2.0 else 'Medium'
        reason = f'No floor set. Cleared eCPM ${ecpm:.2f} → initial floor ${initial:.2f} ({int(safe_ratio*100)}% of eCPM).'
        if aggressiveness != 1.0: reason += ' [Historically Tuned]'
        return initial, 'Increase', 100.0, conf, reason

    if not real_perf:
        # Current floor > 0 but no data at all after thresholding
        return current_floor, 'No change', 0.0, 'Low', 'Insufficient impression data to evaluate this floor.'

    if current_p is None:
        # Re-anchor: use the floor with most impressions as eff. current
        current_floor = max(real_perf, key=lambda f: real_perf[f]['imps'])
        current_p = real_perf[current_floor]

    # ── CASE A: Multiple floors – equal-volume projection comparison ──────────
    # Raw impression/revenue sums are biased by how long each floor was active.
    # Project every candidate's observed eCPM/RPM onto the current floor's
    # baseline volume so the comparison is duration-neutral.
    if len(real_perf) > 1:
        if is_volume_crash:
            # Force decrease to recover volume
            best_floor = max(0.25, round(current_floor * 0.75 / 0.25) * 0.25)
            if best_floor >= current_floor:
                best_floor = round(current_floor * 0.75, 2)
        else:
            # ── YIELD (RPM) MAXIMIZATION over the observed demand curve ─────────
            # Each observed floor is a sample of the segment's price→fill response.
            # Project every candidate onto the CURRENT floor's request volume and pick
            # the floor that maximizes projected revenue-per-request. The price↑/fill↓
            # trade-off is captured in the projection, so this directly optimizes yield
            # the way Google Optimized Pricing / Magnite ALG do — not an eCPM heuristic.
            # Opportunity-cost guard: a candidate must beat current yield by the weak-gain
            # margin AND be statistically significant, else we hold.
            best_floor = current_floor
            best_yield = current_p['rev']                       # projected rev at equal reqs
            threshold  = current_p['rev'] * (1.0 + WEAK_GAIN_THRESHOLD)

            for cand_f, cand_p in real_perf.items():
                if cand_f == current_floor:
                    continue
                if current_floor > 0 and abs(cand_f - current_floor) / current_floor > MAX_CHANGE_PCT:
                    continue

                is_increase = cand_f > current_floor
                floor_chg_pct = abs(cand_f - current_floor) / current_floor * 100 if current_floor > 0 else 0

                if has_requests and current_p.get('fill_rate', 0) > 0 and cand_p.get('fill_rate', 0) > 0:
                    # Observed RPM at this floor, projected onto current request volume
                    c_reqs = current_p['imps'] / current_p['fill_rate']
                    projected_cand_rev = (cand_p.get('rpm', 0.0) / 1000) * c_reqs
                    cr = current_p.get('rpm', 0.0); kr = cand_p.get('rpm', 0.0)
                else:
                    # No request data: project fill via LEARNED elasticity, value at observed eCPM
                    coeff = e_inc if is_increase else e_dec
                    fill_delta = (floor_chg_pct / 100.0) * coeff
                    projected_imps = current_p['imps'] * ((1 - fill_delta) if is_increase else (1 + fill_delta))
                    projected_cand_rev = projected_imps * cand_p['ecpm'] / 1000
                    cr = current_p['ecpm']; kr = cand_p['ecpm']

                metric_chg = (kr - cr) / cr if cr > 0 else (1.0 if kr > 0 else 0.0)
                # Significance gate against sampling noise
                if not _significant(metric_chg, current_p['imps'], cand_p['imps']):
                    continue
                # Opportunity-cost guard + pick the genuine yield maximizer
                if projected_cand_rev <= threshold:
                    continue
                if projected_cand_rev > best_yield:
                    best_yield = projected_cand_rev
                    best_floor = cand_f

    # ── CASE B: Single floor – eCPM gap step logic (historically tuned) ─────────
    else:
        ecpm  = current_p['ecpm']
        ratio = ecpm / current_floor if current_floor > 0 else 0

        # Base steps 10% / 5% / -10% scaled by ML aggressiveness
        step_up_major = 0.10 * aggressiveness
        step_up_minor = 0.05 * aggressiveness
        step_down_major = 0.10 * (1.5 - aggressiveness) # If highly aggressive holding, drop less. If conservative, drop faster.

        if is_volume_crash:
            # Force aggressive floor decrease to recapture traffic
            best_floor = max(0.25, round(current_floor * 0.75 / 0.25) * 0.25)
            if best_floor >= current_floor:
                best_floor = round(current_floor * 0.75, 2)
        elif ratio >= 1.5:
            best_floor = round(current_floor * (1.0 + step_up_major), 2)
        elif ratio >= 1.2:
            best_floor = round(current_floor * (1.0 + step_up_minor), 2)
        elif ratio < 0.5:
            # Emergency correction: floor is 2x+ above eCPM, cut to 80% of eCPM
            best_floor = max(0.25, round(ecpm * 0.80 / 0.25) * 0.25)
        elif ratio < 0.8:
            best_floor = round(current_floor * (1.0 - (step_down_major * 2.0)), 2)
        elif ratio <= 1.05:
            best_floor = round(current_floor * (1.0 - step_down_major), 2)
        else:
            conf = _confidence(current_p['imps'])
            return current_floor, 'No change', 0.0, conf, \
                f'Floor ${current_floor:.2f} near optimal (eCPM ${ecpm:.2f}).'

        # Enforce minimum step to escape no-change band if suggested != current
        min_step = NO_CHANGE_BAND * 1.5
        chg = abs(best_floor - current_floor) / current_floor if current_floor > 0 else 0
        if current_floor > 0 and chg > 0 and chg < min_step and not is_volume_crash:
            direction_sign = 1 if best_floor > current_floor else -1
            best_floor = round(current_floor * (1 + direction_sign * min_step), 2)

    # ── Final direction + confidence ───────────────────────────────────────────
    chg_raw = (best_floor - current_floor) / current_floor if current_floor > 0 else 1.0

    if abs(chg_raw) < NO_CHANGE_BAND and not is_volume_crash:
        conf = _confidence(current_p['imps'])
        return current_floor, 'No change', 0.0, conf, \
            f'Suggested floor ${best_floor:.2f} within {int(NO_CHANGE_BAND * 100)}% band of current ${current_floor:.2f}.'

    direction  = 'Increase' if chg_raw > 0 else 'Decrease'
    change_pct = abs(chg_raw) * 100
    best_p     = real_perf.get(best_floor, current_p)
    confidence = _confidence(best_p['imps'])

    # ── REVENUE BREAKEVEN GUARD ────────────────────────────────────────────────
    # For increases: mathematically verify net revenue won't decrease.
    # breakeven_fill = current_rev / new_floor. If current_imps * (1 - estimated_drop) < breakeven, block.
    # REVAMP: Only active for increases > 5%
    if direction == 'Increase' and change_pct > 5 and current_p['imps'] > 0 and best_floor > 0:
        current_rev_per_imp = current_p['rev'] / current_p['imps'] if current_p['imps'] > 0 else 0
        # Learned elasticity: fill drop = (fractional floor increase) * elasticity coefficient
        estimated_fill_drop = (change_pct / 100.0) * e_inc
        surviving_imps = current_p['imps'] * (1 - estimated_fill_drop)
        # At the new higher floor, revenue per impression shifts up by floor ratio
        projected_new_rev = surviving_imps * current_rev_per_imp * (best_floor / current_floor)
        
        if projected_new_rev < current_p['rev'] * 1.02:  # Must beat 102% of current rev
            # Block: this increase would likely LOSE revenue
            conf = _confidence(current_p['imps'])
            return current_floor, 'No change', 0.0, conf, \
                f'Floor increase to ${best_floor:.2f} blocked by Revenue Guard — estimated fill drop of {estimated_fill_drop*100:.1f}% would cause net revenue loss.'

    reason = _build_reason(direction, current_floor, best_floor, change_pct,
                           confidence, current_p, best_p, is_volume_crash, avg_imps)
                           
    history_count = ml_profile.get('history_count', 0)
    if history_count >= 3 and abs(chg_raw) > 0 and not is_volume_crash:
        if aggressiveness >= 1.25:
            reason += ' [Stable historical eCPM]'
        elif aggressiveness <= 0.75:
            reason += ' [Volatile historical eCPM]'
        elif aggressiveness != 1.0:
            reason += ' [Historically Tuned]'
    elif aggressiveness != 1.0 and abs(chg_raw) > 0 and not is_volume_crash:
        reason += ' [Historically Tuned]'

    # Only one floor ever observed for this segment → no demand curve to optimize over.
    # Any change is an exploration probe; label it so the operator (and the learning loop)
    # treats it as a bandit step to be measured on the next upload, not a confident call.
    if len(real_perf) == 1 and not is_volume_crash:
        reason += ' [Exploratory — single floor observed; small probe to learn the demand curve]'

    return best_floor, direction, change_pct, confidence, reason


def _confidence(imps):
    if imps >= MIN_IMPS_SEGMENT * 3: return 'High'
    if imps >= MIN_IMPS_SEGMENT:     return 'Medium'
    return 'Low'


def _build_reason(direction, cur, sug, pct, conf, cur_p, sug_p, is_volume_crash=False, avg_imps=0):
    if is_volume_crash:
        return (f'Volume crash detected (current imps {cur_p.get("imps", 0):,.0f} vs historical average {avg_imps:,.0f}). '
                f'Reducing floor to ${sug:.2f} to recapture traffic.')
    if direction == 'Increase':
        rev_gain = sug_p.get('rev', 0) - cur_p.get('rev', 0)
        return (f'Higher revenue ${sug_p.get("rev", 0):.2f} vs ${cur_p.get("rev", 0):.2f} '
                f'({rev_gain:+.2f}) observed at ${sug:.2f}. ({conf} confidence)')
    else:
        imp_gain = sug_p.get('imps', 0) - cur_p.get('imps', 0)
        return (f'Lower floor ${sug:.2f} captures more volume ({imp_gain:+.0f} imps). '
                f'({conf} confidence)')


# ─── Segment Analysis Pipeline ─────────────────────────────────────────────────
def analyze_segment(seg_key, seg_df, df_full, has_requests, fallback_cols_list, fb_groups,
                    ml_knowledge=None, elasticity_map=None, bid_map=None, cut_bias_map=None,
                    anchor_map=None):
    if ml_knowledge is None: ml_knowledge = {}
    if elasticity_map is None: elasticity_map = {}
    if bid_map is None: bid_map = {}
    if cut_bias_map is None: cut_bias_map = {}
    if anchor_map is None: anchor_map = {}

    total_imps = float(seg_df['impressions'].sum())
    total_rev  = float(seg_df['revenue'].sum())
    total_reqs = float(seg_df['requests'].sum()) if ('requests' in seg_df.columns) else 0.0

    # Look up this segment's bid landscape (joined on normalized site+position key).
    seg_bid = None
    if bid_map:
        bkey = (bid_join_key(seg_key.get('ad_unit', '')), seg_key.get('country', ''), seg_key.get('device', ''))
        seg_bid = bid_map.get(bkey)
        # Use bid-report request/response volume when the main report has none (enables RPM).
        if total_reqs <= 0 and seg_bid:
            total_reqs = float(seg_bid.get('requests') or seg_bid.get('responses') or 0.0)

    effective_df   = seg_df
    used_fallback  = False
    fallback_label = ''

    if total_imps < MIN_IMPS_SEGMENT:
        for fb_cols in fallback_cols_list:
            single = len(fb_cols) == 1
            key_vals = tuple(seg_key.get(c, '') for c in fb_cols)
            lookup = key_vals[0] if single else key_vals
            grp = fb_groups.get(tuple(fb_cols))
            if grp is not None and lookup in grp.groups:
                fb_df = grp.get_group((lookup,) if single else lookup)
                if fb_df['impressions'].sum() >= MIN_IMPS_SEGMENT:
                    effective_df   = fb_df
                    used_fallback  = True
                    fallback_label = ' + '.join(fb_cols)
                    break

    current_floor = detect_current_floor(effective_df)
    perf = evaluate_floor_candidates(effective_df, has_requests)

    # Compute overall segment eCPM as fallback signal
    total_eff_imps = effective_df['impressions'].sum()
    total_eff_rev  = effective_df['revenue'].sum()
    seg_ecpm = (total_eff_rev / total_eff_imps * 1000) if total_eff_imps > 0 else 0.0

    # Extract ML volatility profile + learned elasticity for this precise Geo+Device
    seg_id = f"{seg_key.get('country', '')}_{seg_key.get('device', '')}"
    ml_profile = ml_knowledge.get(seg_id, {})
    elasticity = elasticity_map.get(seg_id, elasticity_map.get('__global__', DEFAULT_ELASTICITY))

    # Measured do-no-harm signal: how prior floor moves on this segment actually performed,
    # per direction (cut_bias_map is now a directional map: {key: {'Decrease':x,'Increase':y}}).
    _bkey = (db._normalize_ad_unit_key(seg_key.get('ad_unit', '')),
             str(seg_key.get('country', '')), str(seg_key.get('device', '')))
    _bias = cut_bias_map.get(_bkey, {})
    cut_bias = _bias.get('Decrease') if isinstance(_bias, dict) else _bias
    raise_bias = _bias.get('Increase') if isinstance(_bias, dict) else None
    sug_floor, direction, change_pct, confidence, reason = recommend_floor(
        current_floor, perf, seg_ecpm, has_requests, ml_profile, elasticity, seg_bid, cut_bias
    )

    # ── MANUAL-ANCHOR MODE (opt-in: only when an anchor map is supplied) ─────────
    # The parent/control tags carry the AdOps team's manual floors, which beat the
    # engine's from-scratch floors on eCPM. So when we have that policy, SEED the
    # variant floor to the proven manual floor and deviate ONLY where measured
    # outcomes justify it. This can't repeat the eCPM collapse (never far below
    # manual) and preserves the upside. Manual uses fine floors (down to $0.10),
    # so anchored floors snap to $0.05 — NOT the $0.25 min that was rejecting
    # cheap-inventory fill the human captures.
    anchor_key = (f"{bid_join_key(seg_key.get('ad_unit',''))}|"
                  f"{seg_key.get('country','')}|{seg_key.get('device','')}")
    anchor = anchor_map.get(anchor_key)
    if anchor is not None and anchor > 0:
        # Prefer the TRUE match rate carried from before the unfilled-row filter
        # (responses/requests, GAM's own definition). Falling back to imps/reqs here
        # would measure fill only over inventory that already filled — see load_and_clean.
        match_rate = None
        if 'true_match_rate' in seg_df.columns:
            _tm = pd.to_numeric(seg_df['true_match_rate'], errors='coerce').dropna()
            if len(_tm):
                match_rate = float(_tm.iloc[0])
        if match_rate is None and total_reqs > 0:
            match_rate = total_imps / total_reqs
        # total_rev drives the revenue-weighted exploration budget: big earners get
        # probed often, the long tail is held at the proven manual floor.
        _treq = None
        if 'true_requests' in seg_df.columns:
            _tr = pd.to_numeric(seg_df['true_requests'], errors='coerce').dropna()
            if len(_tr):
                _treq = float(_tr.iloc[0])
        target, why = _anchored_floor(anchor, anchor_key, seg_bid, cut_bias, raise_bias,
                                      match_rate, seg_rev=total_rev, seg_reqs=_treq)
        step = 0.05
        target = max(step, round(target / step) * step)
        # The $0.05 snap rounds to NEAREST, so it can round a *bounded* cut DOWN
        # past its limit — a −45% target on a $0.85 anchor snapped to $0.45 (−47%).
        # Re-assert the deepest allowed cut AFTER snapping, rounding up onto the grid.
        deepest = max(FLOOR_MIN_ABS, anchor * (1 - max(ANCHOR_BAND_DOWN, DEEP_CUT_MAX)))
        if target < deepest - 1e-9:
            target = max(step, math.ceil(deepest / step) * step)
        if current_floor > 0 and abs(target - current_floor) / current_floor < NO_CHANGE_BAND:
            sug_floor, direction, change_pct = current_floor, 'No change', 0.0
            reason = why + ' Already at target.'
        else:
            sug_floor = round(target, 2)
            direction = 'Increase' if target > current_floor else 'Decrease'
            change_pct = (abs(target - current_floor) / current_floor * 100) if current_floor > 0 else 100.0
            reason = why
        return _seg_result(seg_key, current_floor, sug_floor, direction, change_pct,
                           confidence, reason, total_imps, total_reqs, total_rev, used_fallback)

    # A pinned site with NO manual floor on record has nothing to pin to — and the
    # from-scratch pricer below is exactly what the pin exists to keep off this site.
    # So leave the segment alone rather than letting it fall through.
    if _site_policy(anchor_key) == 'manual':
        return _seg_result(seg_key, current_floor, current_floor, 'No change', 0.0,
                           confidence,
                           'SITE HOLD — site pinned to manual, and no manual floor on record '
                           'for this segment. Leaving the live floor untouched.',
                           total_imps, total_reqs, total_rev, used_fallback)

    # ── SELF-SUFFICIENT PROBE CONTROLLER (explore → measure → act) ──────────────
    # The engine proves itself from its own reports: an unproven change is capped to a
    # small PROBE the learning loop can score against the next performance upload.
    # Once outcomes exist: winners are released to full size, losers are already held
    # inside recommend_floor. This is the bandit loop — no external dashboard needed.
    if direction != 'No change' and current_floor > 0:
        has_proof = cut_bias is not None and abs(cut_bias) >= 1.0
        proven_win = has_proof and cut_bias >= CUT_WIN_PCT
        if not proven_win and change_pct > PROBE_PCT * 100:
            sign = 1 if direction == 'Increase' else -1
            sug_floor = max(FLOOR_STEP, round(current_floor * (1 + sign * PROBE_PCT) / FLOOR_STEP) * FLOOR_STEP)
            change_pct = abs(sug_floor - current_floor) / current_floor * 100
            if abs(sug_floor - current_floor) / current_floor < NO_CHANGE_BAND:
                sug_floor, direction, change_pct = current_floor, 'No change', 0.0
                reason = 'Probe rounds to current floor — holding.'
            else:
                reason += ' [Probe — capped move; next upload measures it, engine scales or reverts]'

    # Enforce a clean, importable floor: hard $0.25 minimum, snapped to $0.25 increments.
    # Reconcile direction/%-change after snapping so the UI and UPR export stay consistent.
    if direction != 'No change':
        snapped = max(FLOOR_STEP, round(sug_floor / FLOOR_STEP) * FLOOR_STEP)
        if current_floor > 0 and abs(snapped - current_floor) / current_floor < NO_CHANGE_BAND:
            sug_floor, direction, change_pct = current_floor, 'No change', 0.0
        else:
            sug_floor = snapped
            direction = 'Increase' if snapped > current_floor else 'Decrease'
            change_pct = (abs(snapped - current_floor) / current_floor * 100) if current_floor > 0 else 100.0

    # ── CONFIDENCE BOOSTER from historical DB ─────────────────────────────────
    # If this upload shows Low confidence but historical DB has seen this segment
    # with substantial volume, upgrade to Medium so the recommendation isn't suppressed.
    if confidence == 'Low' and ml_profile.get('avg_ecpm', 0) > 0:
        confidence = 'Medium'
        reason += ' [Confidence boosted by historical data]'

    # Suppress low-confidence weak signals
    if confidence == 'Low' and change_pct < 15 and direction != 'No change' and current_floor > 0:
        sug_floor  = current_floor
        direction  = 'No change'
        change_pct = 0.0
        reason     = 'Low confidence — maintaining current floor is safer.'

    if used_fallback and reason:
        reason += f' (Based on broader segment: {fallback_label})'

    return _seg_result(seg_key, current_floor, sug_floor, direction, change_pct,
                       confidence, reason, total_imps, total_reqs, total_rev, used_fallback)


def _explore_cadence(seg_rev):
    """
    How often this segment earns an exploration probe, by revenue contribution.
    Returns the 'every N weeks' divisor, or 0 to never explore.

    Uniform exploration wasted the budget: ~90% of segments earn under $0.25 per
    30-day window and together account for <2% of revenue, so a probe there can
    never be measured — it only burns blast-radius budget and adds churn.
    """
    if seg_rev is None:
        return EXPLORE_EVERY                 # unknown revenue -> current behaviour
    if seg_rev >= EXPLORE_REV_HIGH:
        return EXPLORE_EVERY_HIGH            # the segments that actually pay
    if seg_rev >= EXPLORE_REV_MID:
        return EXPLORE_EVERY
    if seg_rev >= EXPLORE_REV_LOW:
        return EXPLORE_EVERY_LOW
    return 0                                 # too small to ever measure


def _anchored_floor(anchor, anchor_key, seg_bid, cut_bias, raise_bias, match_rate=None,
                    seg_rev=None, seg_reqs=None):
    """
    Manual-anchor policy for one segment. Returns (target_floor, reason).

      • PROMOTE: if measured outcomes prove moving off manual in a direction pays
        (raise_bias / cut_bias >= CUT_WIN_PCT), deviate that way within a band.
      • EXPLORE: else, on a rotating ~1/EXPLORE_EVERY weekly slice, probe ±EXPLORE_PROBE
        around manual (direction hinted by the bid landscape) so the next cycle can
        measure it. This is what lets the engine BEAT manual, not just mirror it.
      • HOLD: otherwise sit exactly on the proven manual floor (safe default).
    """
    # RECOVERY MODE (set by the automation on a revenue anomaly): replicate the
    # proven manual floor exactly — no exploration, no promoted deviations. The
    # safe harbor is the human's known-good config, not whatever we last tried.
    if os.environ.get('FLOORS_RECOVERY') == '1':
        return anchor, f'RECOVERY — replicating manual floor ${anchor:.2f} (revenue anomaly; exploration paused).'

    # EXPLOIT MODE (automation sets FLOORS_EXPLOIT=1 when state/EXPLOIT_MODE exists).
    # Ship the best-KNOWN policy instead of experiments. Asymmetric on purpose:
    #   • no RAISES at all — measured outcomes show raising loses revenue
    #     (median −8.4%, bootstrap CI [−13.4,−2.5] excludes 0), and socialnieuws_nl
    #     is that failure live: engine match rate 0.32% vs manual 0.42%, −13% CPM.
    #   • no unproven DEEP probes — a −45% bet with no payoff inside the window.
    #   • cuts are KEPT, because they are what produces the current wins
    #     (paparazzi +24%, 1point3acres +21%, weetjewijzer +17% on ellipsis).
    exploit = os.environ.get('FLOORS_EXPLOIT') == '1'

    # SITE HOLD: on a site where an equal-traffic A/B measured the engine losing to the
    # desk, ship the human's floor and nothing else. Checked before every other rung —
    # including proven cuts — because the measurement that put the site here already
    # accounts for whatever those rungs were doing. Holding IS the revenue-maximising
    # move once the engine is demonstrably behind.
    if _site_policy(anchor_key) == 'manual':
        return anchor, (f'SITE HOLD — pinned to proven manual floor ${anchor:.2f}; measured A/B '
                        f'shows the engine behind the desk on this site, so no deviation ships.')

    # Per-site discount: where the variant tag is measured to under-fill at manual's
    # price, seed below the anchor instead of at it. Applied to the anchor itself so
    # every downstream path (hold, promote, explore, deep probe) inherits it.
    site = str(anchor_key).split('|')[0]
    for _site, _disc in SITE_ANCHOR_DISCOUNT.items():
        if site.startswith(_site):
            anchor = max(FLOOR_MIN_ABS, anchor * (1 - _disc))
            break
    proven_raise = raise_bias is not None and raise_bias >= CUT_WIN_PCT
    proven_cut = cut_bias is not None and cut_bias >= CUT_WIN_PCT
    if proven_raise and (not proven_cut or raise_bias >= cut_bias):
        if exploit:
            return anchor, (f'EXPLOIT — holding manual ${anchor:.2f}; raises are suppressed '
                            f'(measured to lose revenue) despite raise-bias {raise_bias:+.0f}%.')
        # Raises stay deliberately tight: measured outcomes show raising floors
        # loses revenue, so a proven raise earns the band edge and no more.
        target = anchor * (1 + ANCHOR_BAND_UP)
        return target, f'Manual ${anchor:.2f} + measured raise-win ({raise_bias:+.0f}%) → ${target:.2f}.'
    if proven_cut:
        # RAMPED: depth scales with the strength of the measured evidence, from
        # the probe size up to the full band. (Previously min()/max() pinned this
        # to exactly ±12%, making the bands dead code — learning had no teeth.)
        span = max(0.0, CUT_RAMP_FULL_PCT - CUT_WIN_PCT)
        strength = 1.0 if span <= 0 else min(1.0, max(0.0, (cut_bias - CUT_WIN_PCT) / span))
        depth = EXPLORE_PROBE + (ANCHOR_BAND_DOWN - EXPLORE_PROBE) * strength
        target = anchor * (1 - depth)
        return target, (f'Manual ${anchor:.2f} + measured cut-win ({cut_bias:+.0f}%) '
                        f'→ −{depth*100:.0f}% = ${target:.2f}.')
    # ── BID-LANDSCAPE OPTIMUM ─────────────────────────────────────────────────
    # Where the measured bid distribution says the revenue-maximising posted price is
    # materially away from the desk's floor, move toward it. Bounded on both sides —
    # this is a model, not a measurement, so it never gets more rope than a proven
    # outcome would. Sits above the under-fill heuristic because it uses the actual
    # demand curve rather than inferring from fill.
    if BID_OPT_ENABLED and not exploit and seg_bid:
        best = bid_optimal_floor(seg_bid)          # honours MIN_BIDS_LANDSCAPE
        if best:
            opt = float(best[0])
            if opt > 0 and abs(opt / anchor - 1.0) >= BID_OPT_MIN_MOVE:
                lo = anchor * (1 - ANCHOR_BAND_DOWN)
                hi = anchor * (1 + ANCHOR_BAND_UP)
                target = max(lo, min(opt, hi))
                target = max(target, FLOOR_MIN_ABS)
                if abs(target / anchor - 1.0) >= 0.02:
                    p_here = bid_prob_at_least(seg_bid, anchor)
                    return target, (
                        f'BID OPTIMUM — {seg_bid.get("bids", 0):,} measured bids put the '
                        f'revenue-maximising price at ${opt:.2f}'
                        + (f'; manual ${anchor:.2f} clears {p_here*100:.1f}% of them'
                           if p_here is not None else f'; manual ${anchor:.2f}')
                        + f'. Moving to ${target:.2f} ({(target/anchor-1)*100:+.0f}%, '
                          f'clamped to the measured bands).')

    # ── UNDER-FILL OVERRIDE (match-rate driven) ───────────────────────────────
    # Measured: revenue per request rises monotonically with fill, per site and within
    # ad unit x country. So a segment filling well under its site's target is leaving
    # money on the table now — correct it rather than waiting for its turn in the
    # explore rotation. CUT ONLY: there is no measured case for raising on high fill.
    # Sits BELOW measured outcomes (a real revenue result outranks a heuristic) and
    # ABOVE the probes (a known mispricing outranks a speculative one).
    if (MATCH_OVERRIDE and not exploit and match_rate is not None and match_rate > 0
            and (seg_reqs is None or seg_reqs >= MATCH_MIN_REQS)):
        site_t = MATCH_TARGET_DEFAULT
        for _s, _t in MATCH_TARGET.items():
            if site.startswith(_s):
                site_t = _t
                break
        if match_rate < site_t * MATCH_SEVERE_RATIO:
            sev = min(1.0, (site_t - match_rate) / site_t)
            depth = EXPLORE_PROBE + (MATCH_CUT_MAX - EXPLORE_PROBE) * sev
            target = max(anchor * (1 - depth), FLOOR_MIN_ABS)
            return target, (f'UNDER-FILL — matching {match_rate*100:.2f}% against a measured '
                            f'target of {site_t*100:.1f}% for this site; the floor is blocking '
                            f'demand that pays. Manual ${anchor:.2f} −{depth*100:.0f}% '
                            f'= ${target:.2f}.')

    # exploration budget — rotating weekly slice, deterministic per (segment, ISO week)
    wk = _dt.date.today().isocalendar()[1]
    digest = hashlib.md5(anchor_key.encode()).hexdigest()
    h = int(digest[:8], 16)              # selection hash
    hdir = int(digest[8:16], 16)         # INDEPENDENT direction hash (decorrelated from selection)
    hdeep = int(digest[16:24], 16)       # INDEPENDENT deep-probe selection hash

    # ── DEEP-CUT PROBE ────────────────────────────────────────────────────────
    # A small rotating slice steps toward the MEASURED posted-price optimum, but
    # only where the bid landscape proves the current floor is blocking almost all
    # demand (p < DEEP_CUT_MAX_P). Bounded to [DEEP_CUT_MIN, DEEP_CUT_MAX] so we
    # probe the direction without betting the network on a model whose revenue
    # assumption (winner pays our floor) does not hold in a live auction.
    # Deep probes are the most expensive experiment we run, so they are reserved
    # for segments big enough that the result can actually be measured.
    deep_eligible = (seg_rev is None) or (seg_rev >= EXPLORE_REV_MID)
    if (hdeep + wk) % DEEP_CUT_EVERY == 0 and seg_bid and not exploit and deep_eligible:
        p_here = bid_prob_at_least(seg_bid, anchor)
        best = bid_optimal_floor(seg_bid)          # honours MIN_BIDS_LANDSCAPE
        if p_here is not None and p_here < DEEP_CUT_MAX_P and best:
            opt = best[0]
            if opt < anchor:
                lo = anchor * (1 - DEEP_CUT_MAX)   # deepest allowed this step
                hi = anchor * (1 - DEEP_CUT_MIN)   # shallowest allowed this step
                target = max(lo, min(opt, hi))
                target = max(target, FLOOR_MIN_ABS)
                return target, (
                    f'DEEP PROBE — manual ${anchor:.2f} clears only {p_here*100:.1f}% of '
                    f'measured bids; optimum ${opt:.2f}. Stepping to ${target:.2f} '
                    f'(−{(1-target/anchor)*100:.0f}%) and measuring.')

    cadence = _explore_cadence(seg_rev)
    if cadence and (h + wk) % cadence == 0:
        p = bid_prob_at_least(seg_bid, anchor) if seg_bid else None
        # Direction priority per industry practice: MEASURED DEMAND (bid landscape)
        # first; the match-rate band is a useful desk heuristic but not a universal
        # objective, so it is only the fallback hint; then a decorrelated hash.
        if p is not None and p < 0.15:      sign = -1   # floor clears almost nothing → try lower
        elif p is not None and p > 0.40:    sign = 1    # lots of demand clears → headroom to raise
        elif match_rate is not None and 0 < match_rate < MATCH_LOW:
            sign = -1   # under-filling → floor likely too high → lower
        # NOTE: there is deliberately no "match rate high → raise" branch. It was here, and
        # the 2026-08-31 analysis refuted it: revenue per request rises monotonically with
        # fill on every site, and within ad unit x country too (median Spearman +0.535).
        # High fill is where we earn MOST, so it is not a reason to raise the floor.
        else:                                sign = 1 if (hdir & 1) else -1
        if exploit:
            # Suppress ALL unproven probes, not just raises. The floors that are
            # currently winning stay live via the no-op dedupe — they do not depend
            # on new probes — so exploration only adds variance here. That matters
            # doubly now that the budget is revenue-weighted: probes concentrate on
            # the top ~26 segments (34% of revenue) at 1-in-2 cadence, which is the
            # last thing you want churning on a day being measured.
            side = 'RAISE' if sign > 0 else 'cut'
            return anchor, (f'EXPLOIT — holding manual ${anchor:.2f}; unproven exploratory '
                            f'{side} suppressed (proven moves still apply).')
        target = max(anchor * (1 + sign * EXPLORE_PROBE), FLOOR_MIN_ABS)
        return target, (f'Exploring {sign*EXPLORE_PROBE*100:+.0f}% around manual ${anchor:.2f} '
                        f'— measured next cycle, kept only if it wins.')
    if cadence == 0:
        return anchor, (f'Seeded to proven manual floor ${anchor:.2f} (holding; segment earns '
                        f'${seg_rev:.2f} over the window — too small for a probe to be measurable).')
    return anchor, f'Seeded to proven manual floor ${anchor:.2f} (holding; not in this week\'s explore slice).'


def _seg_result(seg_key, current_floor, sug_floor, direction, change_pct,
                confidence, reason, total_imps, total_reqs, total_rev, used_fallback):
    ecpm = (total_rev / total_imps * 1000) if total_imps > 0 else 0.0
    rpm  = (total_rev / total_reqs * 1000) if total_reqs > 0 else 0.0
    r = dict(seg_key)
    r.update({
        'current_floor':    round(current_floor, 2),
        'suggested_floor':  round(sug_floor, 2),
        'change_direction': direction,
        'change_pct':       round(change_pct, 1),
        'confidence':       confidence,
        'reason':           reason or '',
        'ecpm':             round(ecpm, 2),
        'rpm':              round(rpm, 2),
        '_raw_imps':        int(total_imps),
        '_raw_reqs':        int(total_reqs),
        '_raw_rev':         round(total_rev, 4),
        '_used_fallback':   used_fallback,
    })
    return r


# ─── Output Compression ────────────────────────────────────────────────────────
def compress(df, max_rows, key_cols):
    """
    Keep every actionable row (Increase / Decrease), ranked by revenue impact.
    Scoring is fully vectorized for speed. When max_rows is None there is NO cap —
    all actionable recommendations are returned.
    """
    if df.empty:
        return df

    actionable = df[df['change_direction'] != 'No change'].copy()
    # Remove very low-confidence, small-change rows
    actionable = actionable[
        ~((actionable['confidence'] == 'Low') & (actionable['change_pct'] < 15))
    ]
    if actionable.empty:
        return df.head(0)  # Nothing to recommend → empty (not a pile of same floors)

    # ── Vectorized impact score (no per-row apply) ──────────────────────────────
    conf_w = actionable['confidence'].map({'High': 3.0, 'Medium': 1.5, 'Low': 0.3}).fillna(0.3)
    raw_rev = actionable.get('_raw_rev', pd.Series(0.01, index=actionable.index)).clip(lower=0.01)
    raw_imps = actionable.get('_raw_imps', pd.Series(1, index=actionable.index)).clip(lower=1)
    chg_pct = actionable.get('change_pct', pd.Series(0.0, index=actionable.index)).fillna(0.0)
    fb_pen = np.where(actionable.get('_used_fallback', False), 0.5, 1.0)
    actionable['_score'] = (raw_rev
                            * np.log1p(raw_imps)
                            * (1 + chg_pct / 50)
                            * conf_w
                            * 2.0
                            * fb_pen)

    actionable = actionable.sort_values('_score', ascending=False)
    if max_rows is not None:
        actionable = actionable.head(max_rows)

    drop = [c for c in actionable.columns if c.startswith('_')]
    return actionable.drop(columns=drop, errors='ignore')


_ANCHOR_CACHE = {'mtime': None, 'map': {}}
def load_manual_anchor(path=None):
    """
    Opt-in manual-floor anchor, cached by mtime. Keys are 'sitekey|country|device'
    -> manual floor. Absent file = empty map = engine behaves exactly as before.
    Never raises.

    PER-NETWORK: both networks carry the same sites, so their anchor keys collide.
    A single shared file let one network's floors overwrite the other's — measured
    2026-08-15, zero1 was anchored to ellipsis's ladder at 0.42-0.61x its own true
    manual floors, i.e. systematically under-priced. The automation sets
    FLOORS_ANCHOR to the network-specific file for the network being processed;
    we fall back to the legacy combined file only if that is absent.
    """
    try:
        if path is None:
            path = os.environ.get('FLOORS_ANCHOR') or 'manual_anchor.json'
            if not os.path.exists(path):
                path = 'manual_anchor.json'
        if not os.path.exists(path):
            return {}
        mt = os.path.getmtime(path)
        # Cache on (path, mtime): keying on mtime alone would serve one network's
        # anchor to the other if both were ever loaded in the same process.
        if _ANCHOR_CACHE.get('mtime') != mt or _ANCHOR_CACHE.get('path') != path:
            with open(path, encoding='utf-8') as f:
                _ANCHOR_CACHE['map'] = json.load(f)
            _ANCHOR_CACHE['mtime'] = mt
            _ANCHOR_CACHE['path'] = path
            logging.info(f"Loaded manual anchor: {len(_ANCHOR_CACHE['map']):,} segments "
                         f"from {os.path.basename(path)}.")
        return _ANCHOR_CACHE['map']
    except Exception as e:
        logging.warning(f"Manual anchor load failed: {e}")
        return {}


# ─── Full Pipeline ─────────────────────────────────────────────────────────────
def run_pipeline(df, has_requests, has_date, date_str="", bid_map=None, realized_uplift=None, anchor_map=None):
    bid_map = bid_map or {}
    anchor_map = anchor_map or load_manual_anchor()
    # Bid-landscape data also enables RPM reporting even without per-row request data
    has_requests = has_requests or bool(bid_map)
    # Load historical ML engine knowledge + learned price elasticity + cut-outcome memory
    ml_knowledge = db.extract_ml_knowledge()
    elasticity_map = db.extract_learned_elasticity()
    cut_bias_map = db.get_directional_bias()   # {key: {'Decrease':x,'Increase':y}} — promotion signal

    # ── Tab 1: Device – Country ──────────────────────────────────────────────
    b_group  = ['ad_unit', 'country', 'device', 'ssp']
    b_fb     = [['ad_unit', 'country', 'ssp'],
                ['ad_unit', 'device', 'ssp'],
                ['ad_unit', 'ssp'],
                ['ad_unit']]
    b_fbg    = {tuple(c): df.groupby(c, observed=True) for c in b_fb}
    b_res    = []
    for keys, seg in df.groupby(b_group, observed=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        seg_key = dict(zip(b_group, keys))
        b_res.append(analyze_segment(seg_key, seg, df, has_requests, b_fb, b_fbg, ml_knowledge, elasticity_map, bid_map, cut_bias_map, anchor_map))

    basic_full = pd.DataFrame(b_res)
    basic_out  = compress(basic_full, MAX_OUTPUT_ROWS, b_group)

    # ── Tab 2: Device – Country – Browser – OS ───────────────────────────────
    d_group  = ['ad_unit', 'country', 'device', 'browser', 'os', 'ssp']
    d_fb     = [['ad_unit', 'country', 'device', 'browser', 'ssp'],
                ['ad_unit', 'country', 'device', 'os', 'ssp'],
                ['ad_unit', 'country', 'device', 'ssp'],
                ['ad_unit', 'country', 'device'],
                ['ad_unit']]
    d_fbg    = {tuple(c): df.groupby(c, observed=True) for c in d_fb}
    d_res    = []
    for keys, seg in df.groupby(d_group, observed=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        seg_key = dict(zip(d_group, keys))
        d_res.append(analyze_segment(seg_key, seg, df, has_requests, d_fb, d_fbg, ml_knowledge, elasticity_map, bid_map, cut_bias_map, anchor_map))

    detailed_full = pd.DataFrame(d_res)
    detailed_out  = compress(detailed_full, MAX_OUTPUT_ROWS, d_group)

    total_basic    = len(basic_full)
    total_detailed = len(detailed_full)
    if realized_uplift is None:
        realized_uplift = db.get_realized_uplift()
    insights       = generate_insights(basic_full, basic_out, total_basic, total_detailed,
                                       date_str, has_requests, realized_uplift)

    return basic_out, detailed_out, basic_full, detailed_full, insights, total_basic, total_detailed


# ─── Insights ──────────────────────────────────────────────────────────────────
def generate_insights(basic_full, basic_out, total_basic, total_detailed, date_str="",
                      has_requests=False, realized_uplift=None):
    insights = []
    realized_uplift = realized_uplift or {}

    if date_str:
        insights.append({'title': 'Timezone & Report Window', 'icon': 'stable', 'items': [
            f'Timezone: Pacific Daylight Time (PDT, GMT-7:00)',
            f'Report Date Range: {date_str}',
            'All floor recommendations aligned with this reporting timezone.'
        ]})

    inc  = (basic_full['change_direction'] == 'Increase').sum()
    dec  = (basic_full['change_direction'] == 'Decrease').sum()
    nc   = (basic_full['change_direction'] == 'No change').sum()
    aus  = basic_full['ad_unit'].nunique() if 'ad_unit' in basic_full.columns else 0

    curr_rev  = float(sum(basic_full.get('_raw_rev', [0])))
    curr_imps = float(sum(basic_full.get('_raw_imps', [0])))
    curr_reqs = float(sum(basic_full.get('_raw_reqs', [0])))
    net_ecpm  = (curr_rev / curr_imps * 1000) if curr_imps > 0 else 0.0
    net_rpm   = (curr_rev / curr_reqs * 1000) if curr_reqs > 0 else 0.0

    summary_items = [
        f'Ad units analyzed: {aus}',
        f'Segments: {total_basic} basic / {total_detailed} detailed',
        f'Increase: {inc}  |  Decrease: {dec}  |  No change: {nc}',
        f'Actionable recommendations: {len(basic_out)}',
        f'Optimization target: {"RPM (revenue per request)" if has_requests else "eCPM (no request data in upload)"}',
    ]
    insights.append({'title': 'Analysis Summary', 'icon': 'chart', 'items': summary_items})

    # ── Current baseline (this upload) ─────────────────────────────────────────
    baseline_items = [f'Net revenue: ${curr_rev:,.2f}', f'Network eCPM: ${net_ecpm:.2f}']
    if has_requests:
        baseline_items.append(f'Network RPM: ${net_rpm:.2f}')
    insights.append({'title': 'Current Baseline', 'icon': 'chart', 'items': baseline_items})

    # ── MEASURED realized uplift (from applied recommendations, not modeled) ────
    applied = int(realized_uplift.get('applied_segments') or 0)
    if applied > 0:
        rev_up = realized_uplift.get('rev_uplift_pct')
        ecpm_up = realized_uplift.get('ecpm_uplift_pct')
        win = realized_uplift.get('win_rate')
        items = [
            f'Applied recommendations measured: {applied}',
            f'Revenue: ${realized_uplift.get("baseline_rev",0):,.0f} → ${realized_uplift.get("followup_rev",0):,.0f}'
            + (f' ({rev_up:+.1f}%)' if rev_up is not None else ''),
            f'eCPM: ${realized_uplift.get("baseline_ecpm",0):.2f} → ${realized_uplift.get("followup_ecpm",0):.2f}'
            + (f' ({ecpm_up:+.1f}%)' if ecpm_up is not None else ''),
        ]
        if win is not None:
            items.append(f'Recommendation win rate: {win:.0f}% of applied changes improved yield')
        insights.append({'title': 'Measured Realized Uplift', 'icon': 'up', 'items': items})
    else:
        insights.append({'title': 'Measured Realized Uplift', 'icon': 'ai', 'items': [
            'No applied recommendations measured yet.',
            'Apply suggested floors, then upload the next report — the engine will measure the '
            'actual revenue & RPM change instead of projecting it.'
        ]})

    if len(basic_out) > 0:
        top_inc = basic_out[basic_out['change_direction'] == 'Increase'].head(5)
        if len(top_inc) > 0:
            opp = []
            for _, r in top_inc.iterrows():
                ad = str(r.get('ad_unit', ''))[:50]
                opp.append(f"{ad} | {r['country']} | {r['device']} → ${r['suggested_floor']:.2f} ({r['change_pct']:.0f}%)")
            insights.append({'title': 'Top Revenue Opportunities', 'icon': 'up', 'items': opp})

    if nc > 0:
        insights.append({'title': 'Segments at Optimal Floor', 'icon': 'stable',
                         'items': [f'{nc} segments are performing well and were left unchanged.']})

    low_conf = basic_full[basic_full['confidence'] == 'Low']
    if len(low_conf) > 0:
        insights.append({'title': 'Low Data Warnings', 'icon': 'warning',
                         'items': [f'{len(low_conf)} segments had insufficient data for confident recommendations.']})
    return insights


def add_learning_insights(insights, learning_pass, learning_summary):
    items = []
    evaluated = int((learning_pass or {}).get('evaluated') or 0)
    applied = int((learning_pass or {}).get('applied') or 0)
    correct = int((learning_pass or {}).get('correct') or 0)
    accuracy = (learning_pass or {}).get('accuracy')

    if evaluated > 0:
        if applied > 0 and accuracy is not None:
            items.append(
                f'This upload evaluated {evaluated} prior recommendations; {applied} appear applied with {correct}/{applied} successful outcomes ({accuracy*100:.0f}%).'
            )
        else:
            items.append(
                f'This upload matched {evaluated} prior recommendations, but none appear applied closely enough to score yet.'
            )
    else:
        items.append('Learning loop ready. Upload the next post-change report to score prior recommendations.')

    total_applied = int((learning_summary or {}).get('applied_outcomes') or 0)
    total_correct = int((learning_summary or {}).get('correct_outcomes') or 0)
    total_accuracy = (learning_summary or {}).get('accuracy')
    if total_applied > 0 and total_accuracy is not None:
        items.append(
            f'Cumulative applied outcomes: {total_correct}/{total_applied} correct ({total_accuracy*100:.0f}% accuracy).'
        )

    insights.append({'title': 'Learning Feedback Loop', 'icon': 'ai', 'items': items})
    return insights



def generate_site_performance(df_full):
    import re
    import numpy as np
    
    def extract_site(au):
        au = str(au)
        m = re.search(r'(?:ellipsis|z1)_dfp_v_(.+?)_v\d+', au)
        if m: return m.group(1).replace('_', '.')
        m = re.search(r'(?:ellipsis|z1)_dfp_v_(.+?)_v_', au)
        if m: return m.group(1).replace('_', '.')
        return au
        
    df = df_full.copy()
    if df.empty:
        return {'total_revenue': 0, 'total_imps': 0, 'total_reqs': 0,
                'avg_ecpm': 0, 'avg_rpm': 0, 'analyzed_sites': 0, 'sites': []}

    df['site'] = df['ad_unit'].apply(extract_site)

    has_reqs = '_raw_reqs' in df.columns and df['_raw_reqs'].sum() > 0
    agg_spec = dict(
        ad_units=('ad_unit', 'nunique'),
        total_imps=('_raw_imps', 'sum'),
        total_rev=('_raw_rev', 'sum'),
        increases=('change_direction', lambda x: (x == 'Increase').sum()),
        decreases=('change_direction', lambda x: (x == 'Decrease').sum()),
        no_change=('change_direction', lambda x: (x == 'No change').sum()),
    )
    if has_reqs:
        agg_spec['total_reqs'] = ('_raw_reqs', 'sum')
    agg = df.groupby('site').agg(**agg_spec).reset_index()
    if not has_reqs:
        agg['total_reqs'] = 0

    agg['ecpm'] = np.where(agg['total_imps'] > 0, (agg['total_rev'] / agg['total_imps'] * 1000).round(2), 0)
    agg['rpm'] = np.where(agg['total_reqs'] > 0, (agg['total_rev'] / agg['total_reqs'] * 1000).round(2), 0)

    tot_rev = agg['total_rev'].sum()
    tot_reqs = agg['total_reqs'].sum()
    agg['rev_share'] = np.where(tot_rev > 0, (agg['total_rev'] / tot_rev * 100).round(1), 0)

    # Rank by revenue, no arbitrary letter grades. The frontend renders the numbers.
    agg = agg.sort_values('total_rev', ascending=False).reset_index(drop=True)

    sites_data = []
    for _, row in agg.iterrows():
        site_dict = row.to_dict()
        site_dict['total_rev'] = round(site_dict['total_rev'], 2)
        site_dict['total_reqs'] = int(site_dict.get('total_reqs', 0))
        site_dict['net_recommendation'] = (
            'Raise floors' if row['increases'] > row['decreases']
            else 'Lower floors' if row['decreases'] > row['increases']
            else 'Hold'
        )
        sites_data.append(site_dict)

    return {
        'total_revenue': round(tot_rev, 2),
        'total_imps': int(agg['total_imps'].sum()),
        'total_reqs': int(tot_reqs),
        'has_requests': bool(has_reqs),
        'avg_ecpm': round((tot_rev / agg['total_imps'].sum() * 1000), 2) if agg['total_imps'].sum() > 0 else 0,
        'avg_rpm': round((tot_rev / tot_reqs * 1000), 2) if tot_reqs > 0 else 0,
        'analyzed_sites': len(agg),
        'sites': sites_data
    }


def generate_day_of_week_trends(df):
    import re
    import numpy as np
    
    if 'day_of_week' not in df.columns or df['day_of_week'].isna().all():
        return {}

    # Extract site name helper
    def extract_site(au):
        au = str(au)
        m = re.search(r'(?:ellipsis|z1)_dfp_v_(.+?)_v\d+', au)
        if m: return m.group(1).replace('_', '.')
        m = re.search(r'(?:ellipsis|z1)_dfp_v_(.+?)_v_', au)
        if m: return m.group(1).replace('_', '.')
        return au

    df_copy = df.copy()
    df_copy['site'] = df_copy['ad_unit'].apply(extract_site)
    
    # Define day order
    day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    df_copy['day_of_week'] = pd.Categorical(df_copy['day_of_week'], categories=day_order, ordered=True)
    
    # Network wide trends
    net_agg = df_copy.groupby('day_of_week', observed=False).agg(
        total_imps=('impressions', 'sum'),
        total_rev=('revenue', 'sum')
    ).reset_index()
    net_agg['ecpm'] = np.where(net_agg['total_imps'] > 0, (net_agg['total_rev'] / net_agg['total_imps'] * 1000).round(2), 0.0)
    net_agg['day_of_week'] = net_agg['day_of_week'].astype(str)
    
    # Site specific trends
    site_agg = df_copy.groupby(['site', 'day_of_week'], observed=False).agg(
        total_imps=('impressions', 'sum'),
        total_rev=('revenue', 'sum')
    ).reset_index()
    site_agg['ecpm'] = np.where(site_agg['total_imps'] > 0, (site_agg['total_rev'] / site_agg['total_imps'] * 1000).round(2), 0.0)
    site_agg['day_of_week'] = site_agg['day_of_week'].astype(str)
    
    # Structure data for frontend
    if not net_agg.empty and not net_agg['ecpm'].isna().all() and len(net_agg) > 0:
        best_day_ecpm_row = net_agg.loc[net_agg['ecpm'].idxmax()]
        worst_day_ecpm_row = net_agg.loc[net_agg['ecpm'].idxmin()]
        best_day_rev_row = net_agg.loc[net_agg['total_rev'].idxmax()]
        
        insights = {
            'best_day_ecpm': str(best_day_ecpm_row['day_of_week']),
            'best_ecpm_val': float(best_day_ecpm_row['ecpm']),
            'worst_day_ecpm': str(worst_day_ecpm_row['day_of_week']),
            'worst_ecpm_val': float(worst_day_ecpm_row['ecpm']),
            'best_day_rev': str(best_day_rev_row['day_of_week']),
            'best_rev_val': float(best_day_rev_row['total_rev']),
        }
    else:
        insights = {}
        
    # Build site-by-site nested dict/list
    site_trends = {}
    for site, group in site_agg.groupby('site'):
        site_trends[site] = group.to_dict('records')
        
    # Calculate Weekday vs Weekend stats
    # Weekday: Mon-Fri, Weekend: Sat-Sun
    weekday_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    weekend_days = ['Saturday', 'Sunday']
    
    ww_data = []
    
    for site, group in df_copy.groupby('site'):
        # Weekday
        wd_df = group[group['day_of_week'].isin(weekday_days)]
        wd_imps = wd_df['impressions'].sum()
        wd_rev = wd_df['revenue'].sum()
        wd_ecpm = (wd_rev / wd_imps * 1000) if wd_imps > 0 else 0.0
        
        # Weekend
        we_df = group[group['day_of_week'].isin(weekend_days)]
        we_imps = we_df['impressions'].sum()
        we_rev = we_df['revenue'].sum()
        we_ecpm = (we_rev / we_imps * 1000) if we_imps > 0 else 0.0
        
        tot_site_rev = wd_rev + we_rev
        wd_share = (wd_rev / tot_site_rev * 100) if tot_site_rev > 0 else 0.0
        we_share = (we_rev / tot_site_rev * 100) if tot_site_rev > 0 else 0.0
        
        variance = ((we_ecpm - wd_ecpm) / wd_ecpm * 100) if wd_ecpm > 0 else 0.0
        
        # Determine strategy
        if variance < -10.0:
            strategy = "Weekend Discount Floors (-10% to -15% on Sat/Sun)"
        elif variance > 10.0:
            strategy = "Weekend Premium Floors (+10% to +15% on Sat/Sun)"
        else:
            strategy = "Steady Flat Floors (maintain uniform floors)"
            
        ww_data.append({
            'site': site,
            'weekday_imps': int(wd_imps),
            'weekday_rev': round(float(wd_rev), 2),
            'weekday_ecpm': round(float(wd_ecpm), 2),
            'weekend_imps': int(we_imps),
            'weekend_rev': round(float(we_rev), 2),
            'weekend_ecpm': round(float(we_ecpm), 2),
            'weekday_share': round(float(wd_share), 1),
            'weekend_share': round(float(we_share), 1),
            'variance': round(float(variance), 1),
            'strategy': strategy
        })
        
    # Sort ww_data by weekday revenue descending
    ww_data = sorted(ww_data, key=lambda x: x['weekday_rev'] + x['weekend_rev'], reverse=True)
        
    return {
        'network': net_agg.to_dict('records'),
        'sites': site_trends,
        'insights': insights,
        'weekday_weekend': ww_data
    }


# ─── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded.'}), 400
        file = request.files['file']
        if not file.filename.lower().endswith('.csv'):
            return jsonify({'error': 'Please upload a CSV file.'}), 400

        df, col_map, has_requests, has_date, date_str = load_and_clean(file)
        if len(df) < 10:
            return jsonify({'error': 'Not enough data rows after cleaning.'}), 400

        # Optional second file: bid-landscape report (enables RPM / true fill)
        bid_map, bid_info = {}, ''
        bid_file = request.files.get('bid_file')
        if bid_file and bid_file.filename:
            if not bid_file.filename.lower().endswith('.csv'):
                return jsonify({'error': 'Bid landscape report must be a CSV.'}), 400
            bid_map, bid_info = load_bid_landscape(bid_file)

        realized_uplift = db.get_realized_uplift()
        basic_out, detailed_out, basic_full, detailed_full, insights, total_basic, total_detailed = run_pipeline(
            df, has_requests, has_date, date_str, bid_map, realized_uplift
        )
        if bid_info:
            insights.insert(0, {'title': 'Bid Landscape', 'icon': 'chart', 'items': [bid_info]})

        # Log to local memory pool for ML training (skip learning pass on duplicate re-uploads)
        upload_id, is_duplicate = db.log_analysis(file.filename, len(df), basic_full, detailed_full)
        learning_pass = db.learn_from_followup_upload(upload_id, df) if (upload_id and not is_duplicate) else {}
        learning_summary = db.get_learning_summary()
        insights = add_learning_insights(insights, learning_pass, learning_summary)
        if is_duplicate:
            insights.insert(0, {'title': 'Duplicate Upload', 'icon': 'warning', 'items': [
                'This report matches a previous upload, so it was not re-logged to telemetry.',
                'Recommendations below are still computed live from the file.'
            ]})

        b_cols = ['ad_unit', 'country', 'device', 'ssp', 'ecpm', 'rpm',
                  'current_floor', 'suggested_floor', 'change_direction', 'change_pct', 'confidence', 'reason']
        d_cols = ['ad_unit', 'country', 'device', 'browser', 'os', 'ssp', 'ecpm', 'rpm',
                  'current_floor', 'suggested_floor', 'change_direction', 'change_pct', 'confidence', 'reason']

        basic_data    = basic_out[[c for c in b_cols if c in basic_out.columns]].to_dict('records')
        detailed_data = detailed_out[[c for c in d_cols if c in detailed_out.columns]].to_dict('records')
        
        site_perf_data = generate_site_performance(basic_full)
        day_of_week_trends = generate_day_of_week_trends(df)

        return jsonify({
            'success': True,
            'basic':    basic_data,
            'detailed': detailed_data,
            'insights': insights,
            'site_performance': site_perf_data,
            'realized_uplift': realized_uplift,
            'day_of_week_trends': day_of_week_trends,
            'rows_processed':       len(df),
            'total_segments_basic':    total_basic,
            'total_segments_detailed': total_detailed,
            'detected_columns': {k: v for k, v in col_map.items()},
            'learning_pass': learning_pass,
            'learning_summary': learning_summary,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def download():
    """
    Receive the exact same compressed detailed data shown in the UI
    and export it as UPR-formatted CSV.
    Columns: country, device, ad unit, os, browser, ssp, floor
    """
    try:
        data     = request.get_json()
        detailed = data.get('detailed', [])

        rows = []
        for r in detailed:
            ad_unit_str = str(r.get('ad_unit', ''))
            # In tag-level mode ad_unit holds the tag identity, which the dashboard
            # cannot resolve. Send a real ad-unit name belonging to that tag — the
            # dashboard maps it back to the same tag and writes the same rule.
            if TAG_LEVEL:
                ad_unit_str = _TAG_REP.get(ad_unit_str, ad_unit_str)
            # Prefer the SSP carried on the row; default to google_mcm_apac for APAC-truncated
            # ad-unit names, else google_mcm.
            # APAC seat for Ellipsis-network units (also covers truncated '…' display names)
            au_l = ad_unit_str.lower()
            default_ssp = 'google_mcm_apac' if (au_l.startswith('ellipsis') or '...' in ad_unit_str
                                                or '…' in ad_unit_str) else 'google_mcm'
            ssp = str(r.get('ssp') or default_ssp)
            if ssp == 'google_mcm' and au_l.startswith('ellipsis'):
                ssp = 'google_mcm_apac'  # correct rows stamped with the old generic default

            rows.append({
                'country':  r.get('country', 'all'),
                'device':   r.get('device', 'Other'),
                'ad unit':  ad_unit_str,
                'os':       r.get('os', 'Other'),
                'browser':  r.get('browser', 'Other'),
                'ssp':      ssp,
                'floor':    round(float(r.get('suggested_floor', 0.0)), 2),
            })

        df_upr = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=['country', 'device', 'ad unit', 'os', 'browser', 'ssp', 'floor'])

        # One row per deployable key. Two rows for the same rule would race, and the
        # loser would silently overwrite the winner — the exact failure this redesign
        # exists to remove. Keep the last (highest-ranked by the compressor).
        if TAG_LEVEL and not df_upr.empty:
            _k = ['country', 'device', 'ad unit', 'os', 'browser', 'ssp']
            _n = len(df_upr)
            df_upr = df_upr.drop_duplicates(subset=_k, keep='last').reset_index(drop=True)
            if _n != len(df_upr):
                logging.info('TAG_LEVEL export: %d rows -> %d unique rules', _n, len(df_upr))

        output = io.StringIO()
        df_upr.to_csv(output, index=False)
        output.seek(0)
        
        from flask import Response
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=floor_suggestions.csv"}
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/history', methods=['GET'])
def history():
    try:
        import sqlite3
        conn = sqlite3.connect('ml_telemetry.db')
        
        total = pd.read_sql('SELECT COUNT(*) as n FROM floor_recommendations', conn).iloc[0,0]
        
        uploads_df = pd.read_sql('''
            SELECT u.id, u.filename, u.timestamp, u.rows_processed,
                   COUNT(r.id) as rec_count
            FROM uploads u
            LEFT JOIN floor_recommendations r ON u.id = r.upload_id
            GROUP BY u.id
            ORDER BY u.id DESC
        ''', conn)
        
        conn.close()
        
        return jsonify({
            'total_rows': int(total),
            'uploads': uploads_df.to_dict('records'),
            'learning': db.get_learning_summary(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500



if __name__ == '__main__':
    print('\n' + '=' * 60)
    print('  Ad Floor Suggestions — http://localhost:5000')
    print('=' * 60 + '\n')
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(debug=True, host='127.0.0.1', port=5000, use_reloader=False)
