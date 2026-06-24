import csv
import io
import os
import tempfile
import unittest

import pandas as pd

import db
from app import app, recommend_floor


class RecommendationLogicTests(unittest.TestCase):
    def test_higher_floor_with_worse_rpm_does_not_win(self):
        perf = {
            1.0: {'imps': 100.0, 'rev': 1.0, 'ecpm': 10.0, 'rpm': 10.0, 'fill_rate': 1.0},
            1.5: {'imps': 1000.0, 'rev': 5.0, 'ecpm': 5.0, 'rpm': 5.0, 'fill_rate': 1.0},
        }

        suggested, direction, change_pct, _, _ = recommend_floor(
            1.0, perf, seg_ecpm=5.45, has_requests=True
        )

        self.assertEqual(suggested, 1.0)
        self.assertEqual(direction, 'No change')
        self.assertEqual(change_pct, 0.0)

    def test_higher_floor_with_better_rpm_can_win(self):
        perf = {
            1.0: {'imps': 100.0, 'rev': 1.0, 'ecpm': 10.0, 'rpm': 10.0, 'fill_rate': 1.0},
            1.25: {'imps': 100.0, 'rev': 1.6, 'ecpm': 16.0, 'rpm': 16.0, 'fill_rate': 1.0},
        }

        suggested, direction, change_pct, _, _ = recommend_floor(
            1.0, perf, seg_ecpm=13.0, has_requests=True
        )

        self.assertEqual(suggested, 1.25)
        self.assertEqual(direction, 'Increase')
        self.assertEqual(change_pct, 25.0)

    def test_lower_floor_with_better_ecpm_wins_despite_less_raw_volume(self):
        """
        Reproduces the Argentinian segment scenario: floor $0.90 ran for most
        of the period and accumulated 1500 impressions, while $0.85 only ran
        briefly (951 impressions). But $0.85 has a higher eCPM (0.61 vs 0.49).
        With equal-volume projection the lower floor should win.
        """
        perf = {
            0.85: {'imps': 951.0, 'rev': 0.58, 'ecpm': 0.6099},
            0.90: {'imps': 1500.0, 'rev': 0.73, 'ecpm': 0.4867},
            3.0:  {'imps': 29.0, 'rev': 0.08, 'ecpm': 2.7586},
        }

        suggested, direction, change_pct, _, _ = recommend_floor(
            0.90, perf, seg_ecpm=0.49, has_requests=False
        )

        self.assertEqual(suggested, 0.85)
        self.assertEqual(direction, 'Decrease')
        self.assertGreater(change_pct, 0.0)

    def test_volume_biased_floor_does_not_win_on_raw_sums(self):
        """
        Current floor $5.00 has 10x more raw impressions simply because it
        was active longer, but candidate $4.50 has a substantially higher
        eCPM. The new projection logic should recommend the decrease.
        """
        perf = {
            5.0: {'imps': 5000.0, 'rev': 15.0, 'ecpm': 3.0},
            4.5: {'imps': 500.0, 'rev': 2.25, 'ecpm': 4.5},
        }

        suggested, direction, change_pct, _, _ = recommend_floor(
            5.0, perf, seg_ecpm=3.0, has_requests=False
        )

        self.assertEqual(suggested, 4.5)
        self.assertEqual(direction, 'Decrease')
        self.assertGreater(change_pct, 0.0)

    def test_higher_floor_with_better_ecpm_wins_with_projection(self):
        """
        Candidate $1.25 has substantially higher eCPM (16.0 vs 10.0) but
        fewer raw impressions (50 vs 1000). Equal-volume projection should
        still recommend the increase because projected revenue is higher.
        """
        perf = {
            1.0:  {'imps': 1000.0, 'rev': 10.0, 'ecpm': 10.0},
            1.25: {'imps': 50.0, 'rev': 0.8, 'ecpm': 16.0},
        }

        suggested, direction, change_pct, _, _ = recommend_floor(
            1.0, perf, seg_ecpm=10.0, has_requests=False
        )

        self.assertEqual(suggested, 1.25)
        self.assertEqual(direction, 'Increase')
        self.assertGreater(change_pct, 0.0)

class DownloadExportTests(unittest.TestCase):
    def test_download_preserves_row_ssp(self):
        client = app.test_client()
        resp = client.post('/api/download', json={
            'detailed': [{
                'country': 'US',
                'device': 'Desktop',
                'ad_unit': 'home_top',
                'os': 'Windows',
                'browser': 'Google Chrome',
                'ssp': 'custom_exchange',
                'suggested_floor': 1.25,
            }]
        })

        self.assertEqual(resp.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
        self.assertEqual(rows[0]['ssp'], 'custom_exchange')


class LearningLoopTests(unittest.TestCase):
    def test_followup_upload_scores_prior_applied_recommendation(self):
        old_db_path = db.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db.DB_PATH = os.path.join(tmp, 'learning.db')
                db.init_db()

                prior_basic = pd.DataFrame([{
                    'ad_unit': 'z1_dfp_v_socialnieuws_nl_v5_v_pre_1',
                    'country': 'US',
                    'device': 'Desktop',
                    'ssp': 'custom_exchange',
                    'current_floor': 1.0,
                    'suggested_floor': 1.25,
                    'change_direction': 'Increase',
                    'change_pct': 25.0,
                    'confidence': 'High',
                    'reason': 'Test recommendation',
                    '_raw_imps': 100,
                    '_raw_rev': 1.0,
                }])
                source_id = db.log_analysis('before.csv', 100, prior_basic, pd.DataFrame())

                followup_basic = prior_basic.copy()
                followup_basic['current_floor'] = 1.25
                followup_id = db.log_analysis('after.csv', 100, followup_basic, pd.DataFrame())

                followup_report = pd.DataFrame([{
                    'ad_unit': 'z1_dfp_v_socialnieuws_nl_v5_v_pre_1',
                    'country': 'US',
                    'device': 'Desktop',
                    'ssp': 'custom_exchange',
                    'floor': 1.25,
                    'impressions': 100,
                    'revenue': 1.4,
                }])

                learning = db.learn_from_followup_upload(followup_id, followup_report)
                summary = db.get_learning_summary()

                self.assertEqual(source_id, learning['source_upload_id'])
                self.assertEqual(learning['evaluated'], 1)
                self.assertEqual(learning['applied'], 1)
                self.assertEqual(learning['correct'], 1)
                self.assertEqual(summary['applied_outcomes'], 1)
                self.assertEqual(summary['confusion_matrix'][0]['predicted_direction'], 'Increase')
                self.assertEqual(summary['confusion_matrix'][0]['actual_direction'], 'Increase')
        finally:
            db.DB_PATH = old_db_path


if __name__ == '__main__':
    unittest.main()
