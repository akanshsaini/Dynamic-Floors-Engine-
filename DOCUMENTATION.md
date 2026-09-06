# Dynamic Floors — how the engine thinks

Sets GAM price floors per segment, daily and unattended, to **beat manual optimisation on revenue
per pageview**. It learns from measured outcomes; it does not guess and keep guessing.

Visual walkthrough (same content, for non-engineers):
<https://claude.ai/code/artifact/83e182e6-1f67-4fc2-bb3b-4a5bfd2fe819>

---

## 1. Inputs and output

| | |
|---|---|
| **Performance report** (GAM CSV, per network) | ad unit, country, device, OS, browser, pricing rule (= current floor), impressions, revenue, requests, date |
| **Bid-range report** (CSV, per network) | per-advertiser average bid CPM + bid counts — where demand actually sits |
| **Parent-tag control report** (CSV, per network) | the manually-priced tags; rebuilds the anchor and feeds the A/B scoreboard |
| **`manual_anchor_<net>.json`** | the proven human floors, keyed `sitekey\|country\|device`. **Per network** — a shared file once let Ellipsis overwrite Zero1's floors and left Zero1 38.4% under-priced |
| **Output** | `automation/outbox/YYYYMMDD_<net>_floors.csv` — UPR-ready: country, device, ad unit, os, browser, ssp, floor |

A **segment** is `tag × country × device × OS × browser` — the only key the dashboard can
store (see §3). Anchors are 3-dimensional (`tag|country|device`) and rules are 5-dimensional;
they are not interchangeable, and treating them as such broke the export quality filter once
(see §7).

---

## 2. The daily cycle — `automation/run_cycle.py`

```
15:30–16:10  GAM emails 6 reports (2 networks × perf/bid/control)
16:15 ├─ slot 1 ─┐
16:45 ├─ slot 2  │  first slot with all reports present wins;
17:15 ├─ slot 3  │  a clean run writes state/success_<date> and later slots exit instantly
17:45 └─ slot 4 ─┘  final slot processes whatever arrived
       │
       ├─ 0. staleness alarm ........ states live floor age; shouts past 2 days
       ├─ 1. anchor rebuild ......... BEFORE pricing, from today's control reports
       ├─ 1b. sanity check .......... report ≥60% of trailing norm, else SKIP network
       ├─ 2. anomaly check .......... revenue collapse ⇒ FLOORS_RECOVERY=1
       ├─ 3. engine ................. per-segment floor decision (§3), in a subprocess
       ├─ 4b. de-dupe ............... one row per deployable key, all sources
       ├─ 4. quality filter ......... drop rules on segments earning ≤ $0.01
       ├─ 5. shape_push ............. no-op dedupe → blast radius → auto-revert (§4)
       ├─ 6. upload ................. headless Playwright, self-authenticating
       └─ 7. digest ................. email + CSVs + desktop toast
```

Each network runs in a **fresh subprocess** — pandas balloons the 100–350 MB reports to several GB,
and the memory was not released between networks (caused `0x8007042B` aborts). A `runlock` file
prevents two slots colliding on the dashboard (observed HTTP 423 on 2026-08-04); the `--one` worker
is a child of an already-locked parent and deliberately does not lock.

Push-state is recorded **only after a verified upload**. Recording it at generation time caused
state drift on days when the upload silently didn't happen.

---

## 3. The brain — `app.py :: _anchored_floor()`

### Granularity: TAG LEVEL (2026-09-07)

The dashboard **cannot store a floor per ad unit**. Uploads go in as "Tag-Specific Rule":
it resolves the ad-unit name to its TAG and stores one rule per
`tag × country × device × os × browser`.

Measured 2026-08-31: the engine was deciding **6,466 floors that collapsed into 1,328
storable rules** — 4.9 decisions per rule, disagreeing in **74%** of cases (median spread
$2.50, worst $17.55). Whichever ad unit uploaded last silently set the price for the rest.
Roughly 80% of the engine's output never reached production, and most of the rest arrived
corrupted.

So `load_and_clean` now collapses `ad_unit` to the tag identity (`tag_key()`:
`weetjewijzer_nl_mid5` → `weetjewijzer_nl`) **before any aggregation**. Segmentation, the
bid join, the anchor lookup, the learning loop and the export all key off `ad_unit`, so
they follow automatically. `_TAG_REP` keeps one real ad-unit name per tag so the export
still sends something the dashboard can resolve.

This also **repairs the learning loop**: outcomes used to be attributed per ad unit while
the floor that produced them was a tag-level collision, so `cut_bias` was trained on
mislabelled experiments.

### The ladder

First matching rung returns; nothing below it is evaluated.

| # | Condition | Result |
|---|---|---|
| **0** | `FLOORS_RECOVERY=1` | Ship the manual floor **exactly**. |
| **—** | `SITE_POLICY[site] == 'manual'` | Pinned site: ship manual, no deviation, ever. |
| **—** | *anchor lookup* | Manual floor becomes the base (`SITE_ANCHOR_DISCOUNT` may seed below it). |
| **1** | `FLOORS_EXPLOIT=1` | Proven cuts only. |
| **2** | `raise_bias ≥ 2%` | `anchor × 1.12`. Band edge, no further. |
| **3** | `cut_bias ≥ 2%` | `anchor × (1 − depth)`, depth ramped 12%→45% as bias goes 2%→10%. |
| **4** | bid landscape has ≥200 bids and optimum is >10% off anchor | Move toward `argmax f·P(bid≥f)`, clamped to [−45%, +12%]. |
| **5** | match rate < ⅓ of the site's fill target | Under-fill cut, depth scaled by severity, max −35%. |
| **6** | deep-probe slice ∧ `P(bid≥anchor)<5%` ∧ `seg_rev≥$1` | Step to the optimum, clamped −30%…−45%. |
| **7** | explore slice (revenue-weighted cadence) | `anchor × (1 ± 0.12)`. |
| **else** | — | Hold at manual. |

Then: snap to `$0.05`, **re-clamp** against −45%, enforce `FLOOR_MIN_ABS`.

### Why the bid landscape moved up (rung 4)

It used to be consulted only at rung 6, gated behind a 1-in-16 weekly slice AND `p<5%`
AND `seg_rev≥$1`. Measured 2026-08-21: **4 of 107 floor decisions (3.7%)** came from it,
while we fetch ~85 MB of bid report per network per day. Coverage was never the problem —
**99.6% of all bids already sit in a key with enough volume** to compute an optimum. The
data was simply never asked. It stays below measured outcomes because the posted-price
optimum is a *model* whose revenue assumption (the winner pays our floor) is false in a
live auction.

### There is no match-rate "band" to steer into

The desk's 1%–1.5% heuristic does not survive our own data. Measured 2026-08-31, revenue
per 1,000 ad requests rises **monotonically** with match rate on every site — no site
peaks in the band:

| match rate | 1point3acres | filmpje | paparazzi | socialnieuws | weetjewijzer |
|---|---|---|---|---|---|
| 1.0–1.5% *(the band)* | 0.133 | 0.063 | 0.018 | 0.095 | 0.039 |
| 2.0–3.0% | **0.237** | **0.182** | 0.021 | **0.165** | 0.160 |
| 5–10% | · | · | **0.026** | · | **0.686** |

Spearman +0.22…+0.65 per site, and it holds **within** ad unit × country groups (median
+0.535, positive in 81% of 429 groups) — so it is not merely "good inventory fills more".
Consequence: **high fill is never a reason to raise a floor.** The signal is used in one
direction only, to size and prioritise cuts, and only on *severe* under-fill
(`MATCH_SEVERE_RATIO = 0.33`) — ungated it hit 98.6% of segments and 95% of revenue,
which is a blanket repricing, not an override.

The true match rate must be computed **before** the `impressions > 0` filter (it is
`responses / requests`, GAM's own definition). Computing it after measures fill only over
inventory that already filled — 91% instead of the true 1.12% — which made every segment
read "over-filling → raise", permanently pushing the direction measured to lose money.

## 4. Guardrails — `automation/guardrails.py`

| Rail | Behaviour | Why it is calibrated that way |
|---|---|---|
| **Sanity** | Report < 60% of trailing norm ⇒ skip the network | A truncated export mimics a demand collapse and would drag every floor down |
| **Anomaly halt** | 2σ below the **weekday** baseline, floored 20% / capped 50%, needs 10 days history and **2 consecutive** qualifying days ⇒ RECOVERY | A fixed percentage sat under the noise floor and fired on 40–66% of days |
| **No-op dedupe** | Floors identical to last push are dropped | Stops the blast-radius budget being spent on changes that change nothing |
| **Blast radius** | ≤30% of segments/day, sorted by relative delta | It is a **prioritiser**, not just a limit — it guarantees highest-impact-first. Waived only under RECOVERY or `FLOORS_FULL_PUSH=1` |
| **Auto-revert** | Restore previous floors where an ad unit underperformed **its own site** by 1.5σ below the median day-over-day move (40% floor / 80% ceiling, ≥$1.00 prior-day base, ≥12 units to trust calibration) | Site-relative on purpose: when *every* position of a site falls together (all six paparazzi breaks −80% on 2026-08-04) that is traffic, not pricing. Network-wide events are the anomaly rail's job — the two stay complementary |

Additional: statistical-significance gate, learned elasticity, eCPM winsorisation, duplicate-upload
guard, `HOLD_UPLOAD` manual-review latch.

---

## 5. Modes

| Env / marker | Effect |
|---|---|
| `FLOORS_RECOVERY=1` | Set automatically by the anomaly rail. Every anchored segment ships the exact manual floor. |
| `state/EXPLOIT_MODE` → `FLOORS_EXPLOIT=1` | Proven cuts only; raises and probes suppressed. For days being measured or presented. |
| `FLOORS_FULL_PUSH=1` | Waives the blast-radius cap. **Rebuild-from-empty only** — throttling a restore to 30%/day leaves most inventory unpriced for days. |
| `FLOORS_ANCHOR=<path>` | Points the engine at one network's anchor file. Set per network by `run_cycle`. |
| `state/HOLD_UPLOAD` | Generate and email floors but ship nothing and queue nothing. |

---

## 6. Scoring — is it working?

50/50 split (was 75/25 until 2026-08-12): engine-priced variant tags vs manually-priced parent tags,
same sites, same days.

**RPM (revenue per 1000 pageviews) is the metric.** Pageviews are the fixed quantity, so RPM is
split-invariant.

**eCPM is a vanity metric here** — the engine lowers floors to buy more impressions at a lower unit
price, so eCPM falls *by design* even as revenue rises. Judging on eCPM would reject the behaviour
that makes the system work.

Revenue must be normalised **day × site** before comparison: the arms spread traffic slightly
differently across sites whose RPMs span $0.11–$1.42, and pooling understates the engine
(+11.1% pooled vs +17.9% mix-adjusted). `automation/build_ab_workbook.py` does this.

**19 Jul – 16 Aug 2026, 29 full days:** +17.9% RPM, +$806.21 like-for-like, ahead on 25 of 29 days.
Per tag: paparazzi +17.6% (29/29), socialnieuws +38.8% (24/29), 1point3acres +9.8% (24/29),
filmpjevandedag +8.5% (19/29), **weetjewijzer −4.6% (4/29 — a standing loss, not noise)**.

---

## 7. Mistakes worth not repeating

- **Shared anchor file** let one network overwrite the other's manual floors → per-network anchors.
- **`usecols` returns file order, not listed order** — adding a `Date` column silently relabelled
  every column in `build_anchor.py` and produced an empty anchor. Rename **by name**.
- **`EXPORT_KEEP_ANCHORED = True`** neutered the quality filter: the anchor is 3-dim and rules are
  5-dim, so nearly every rule matched. It must stay `False`.
- **Waiving the blast-radius cap** to "ship everything" force-pushed ~4,000 rules worth $12/month.
  The cap prioritises; waive it only on a genuine restore.
- **Streak keyed on `date.today()-1`** never accumulated, so a broken tag would go undetected
  forever. Derive streaks from the data.
- **Comparing a modal statistic to a weighted mean** across differently-shaped populations produced
  two wrong socialnieuws diagnoses. Compare per-segment, same statistic, joined on key.

---

- **Optimising at a granularity you cannot deploy.** The engine priced per ad unit for months
  while the dashboard stored per tag. Verify the delivery path before refining the decision.
- **Aggregating with the wrong weight.** Collapsing per-position anchors with a *request*-weighted
  mode returned the CHEAPEST position's floor every time (request volume anti-correlates with
  price) — a 12% estate-wide price cut disguised as a refactor. Revenue-weighted median fixed it.
- **Sequencing.** The anchor was rebuilt *after* pricing, so every run used yesterday's desk
  prices. Invisible while the gap was one day; obvious at five.
- **Silent input failure.** A revoked Gmail app password killed the run before the digest, so
  five days of no floors produced no email and no toast. Inputs need alarms as much as outputs.

## 8. Files

| Path | Role |
|---|---|
| `app.py` | Engine. `_anchored_floor()` = §3; `analyze_segment()` wraps it, snaps and clamps |
| `db.py` | Telemetry + learning loop (`cut_bias` / `raise_bias` come from here) |
| `automation/run_cycle.py` | Orchestrator — §2 |
| `automation/guardrails.py` | All five rails — §4 |
| `automation/settings.py` | Every threshold |
| `automation/build_anchor.py` | Rebuilds `manual_anchor_<net>.json` from control reports |
| `automation/uploader.py` | Headless dashboard upload, self-authenticating |
| `automation/build_ab_workbook.py` | Manual-vs-engine comparison workbook |
| `automation/runlock.py`, `retention.py`, `pending.py` | Single-instance lock, disk pruning, failed-upload queue |
| `automation/.env` | `GMAIL_*`, `DASH_*`. **Gitignored. Never commit.** |

---

## 9. Ongoing

1. Judge uplift on the **multi-week trend**, never a single day.
2. Rebuild the manual anchor whenever AdOps re-prices (it refreshes daily from the control report).
3. **Open:** weetjewijzer should come off the engine or be anchored far tighter.
4. **Open:** two tags turned negative exactly when the split moved to 50/50 — an edge measured on a
   quarter of the traffic may not survive carrying all of it. Re-measure before scaling.
5. **Open:** the socialnieuws −20% discount is a seeded hypothesis awaiting a clean read.
