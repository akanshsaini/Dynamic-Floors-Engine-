import unittest
import pandas as pd
import numpy as np
import sys
import os

# Add the workspace directory to path to import app
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from app import generate_day_of_week_trends

class TestDayOfWeekTrends(unittest.TestCase):
    def setUp(self):
        # Create a mock dataframe resembling the cleaned report structure
        self.mock_data = pd.DataFrame([
            # paparazzi.ar (Weekday vs Weekend)
            {'ad_unit': 'ellipsis_dfp_v_paparazzi_ar_v9_v_pre_1', 'day_of_week': 'Monday', 'impressions': 1000, 'revenue': 1.0},
            {'ad_unit': 'ellipsis_dfp_v_paparazzi_ar_v9_v_mid1_1', 'day_of_week': 'Wednesday', 'impressions': 1000, 'revenue': 1.0},
            {'ad_unit': 'ellipsis_dfp_v_paparazzi_ar_v9_v_mid2_1', 'day_of_week': 'Saturday', 'impressions': 500, 'revenue': 0.25}, # lower weekend eCPM ($0.50 vs weekday $1.00)
            {'ad_unit': 'ellipsis_dfp_v_paparazzi_ar_v9_v_mid2_1', 'day_of_week': 'Sunday', 'impressions': 500, 'revenue': 0.25},
            
            # socialnieuws.nl
            {'ad_unit': 'ellipsis_dfp_v_socialnieuws_nl_v10_v_pre_1', 'day_of_week': 'Tuesday', 'impressions': 500, 'revenue': 2.50}, # Weekday: eCPM $5.00
            {'ad_unit': 'ellipsis_dfp_v_socialnieuws_nl_v10_v_mid1_1', 'day_of_week': 'Saturday', 'impressions': 200, 'revenue': 1.20}, # Weekend: eCPM $6.00 (+20% variance)
        ])
        
    def test_trends_returns_empty_dict_when_no_day_column(self):
        df = self.mock_data.drop(columns=['day_of_week'])
        trends = generate_day_of_week_trends(df)
        self.assertEqual(trends, {})

    def test_network_trends_calculation(self):
        trends = generate_day_of_week_trends(self.mock_data)
        
        self.assertIn('network', trends)
        net = trends['network']
        self.assertEqual(len(net), 7) # Should include all days of the week placeholders or categoricals
        
        # Monday: 1000 imps, $1.00 rev -> $1.00 eCPM
        mon_data = [d for d in net if d['day_of_week'] == 'Monday'][0]
        self.assertEqual(mon_data['total_imps'], 1000)
        self.assertEqual(mon_data['total_rev'], 1.0)
        self.assertEqual(mon_data['ecpm'], 1.0)
        
        # Tuesday: 500 imps, $2.50 rev -> $5.00 eCPM
        tue_data = [d for d in net if d['day_of_week'] == 'Tuesday'][0]
        self.assertEqual(tue_data['total_imps'], 500)
        self.assertEqual(tue_data['total_rev'], 2.50)
        self.assertEqual(tue_data['ecpm'], 5.0)

    def test_weekday_vs_weekend_split(self):
        trends = generate_day_of_week_trends(self.mock_data)
        self.assertIn('weekday_weekend', trends)
        
        ww = trends['weekday_weekend']
        self.assertEqual(len(ww), 2) # paparazzi.ar and socialnieuws.nl
        
        pap = [w for w in ww if w['site'] == 'paparazzi.ar'][0]
        # paparazzi.ar:
        # Weekday: Mon 1000 imps $1, Wed 1000 imps $1 -> 2000 imps, $2 rev -> $1.00 eCPM
        # Weekend: Sat 500 imps $0.25, Sun 500 imps $0.25 -> 1000 imps, $0.50 rev -> $0.50 eCPM
        # Variance: ($0.50 - $1.00) / $1.00 = -50%
        self.assertEqual(pap['weekday_imps'], 2000)
        self.assertEqual(pap['weekday_rev'], 2.0)
        self.assertEqual(pap['weekday_ecpm'], 1.0)
        self.assertEqual(pap['weekend_imps'], 1000)
        self.assertEqual(pap['weekend_rev'], 0.5)
        self.assertEqual(pap['weekend_ecpm'], 0.5)
        self.assertEqual(pap['variance'], -50.0)
        self.assertIn("Weekend Discount Floors", pap['strategy'])
        
        soc = [w for w in ww if w['site'] == 'socialnieuws.nl'][0]
        # socialnieuws.nl:
        # Weekday: Tue 500 imps $2.5 -> $5.00 eCPM
        # Weekend: Sat 200 imps $1.2 -> $6.00 eCPM
        # Variance: ($6.00 - $5.00) / $5.00 = +20%
        self.assertEqual(soc['weekday_imps'], 500)
        self.assertEqual(soc['weekday_rev'], 2.5)
        self.assertEqual(soc['weekday_ecpm'], 5.0)
        self.assertEqual(soc['weekend_imps'], 200)
        self.assertEqual(soc['weekend_rev'], 1.2)
        self.assertEqual(soc['weekend_ecpm'], 6.0)
        self.assertEqual(soc['variance'], 20.0)
        self.assertIn("Weekend Premium Floors", soc['strategy'])

if __name__ == '__main__':
    unittest.main()
