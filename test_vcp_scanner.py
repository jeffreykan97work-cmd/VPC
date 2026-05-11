import unittest
import numpy as np
import pandas as pd
import os
from unittest.mock import patch

from vcp_scanner import AnalystEngine, VCPScanner, main, score_to_grade


def make_sample_df(rows: int = 260, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = np.linspace(80, 120, rows) + rng.normal(0, 1.2, rows)
    close = pd.Series(np.abs(base) + 1)
    high = close * (1 + rng.uniform(0.001, 0.02, rows))
    low = close * (1 - rng.uniform(0.001, 0.02, rows))
    volume = pd.Series(rng.integers(1_000_000, 3_000_000, rows))
    return pd.DataFrame({"Close": close, "High": high, "Low": low, "Volume": volume})


class TestAnalystEngine(unittest.TestCase):
    def test_calc_rsi_uptrend_is_high(self):
        close = pd.Series(np.linspace(100, 140, 50))
        rsi = AnalystEngine.calc_rsi(close)
        self.assertGreaterEqual(rsi, 90.0)

    def test_build_plan_has_valid_levels(self):
        df = make_sample_df()
        pivot = float(df["High"].iloc[-10:].max())
        plan = AnalystEngine.build_plan(df, pivot)

        self.assertIn(plan["recommendation"], {"BUY", "WATCH"})
        self.assertIn(plan["analyst_rating"], {"Strong Buy", "Buy", "Outperform Watchlist", "Neutral"})
        self.assertIn(plan["confidence"], {"High", "Medium", "Medium-Low", "Low"})
        self.assertGreater(plan["entry"], plan["stop_loss"])
        self.assertGreater(plan["take_profit"], plan["entry"])
        self.assertGreater(plan["risk_reward"], 0)
        self.assertTrue({"momentum_ok", "trend_ok", "volume_ok", "risk_ok", "setup_points"} <= set(plan["analyst_flags"].keys()))


class TestScannerDefaults(unittest.TestCase):
    def test_default_config(self):
        scanner = VCPScanner()
        self.assertEqual(scanner.min_score, 40)
        self.assertEqual(scanner.trend_min_passed, 6)
        self.assertEqual(scanner.min_contractions, 0)
        self.assertEqual(scanner.min_results, 10)
        self.assertFalse(scanner.only_buy_recommendation)
        self.assertIn("not_buy_signal", scanner.rejections)

    def test_score_to_grade_uses_abcd_buckets(self):
        self.assertEqual(score_to_grade(80), "A")
        self.assertEqual(score_to_grade(60), "B")
        self.assertEqual(score_to_grade(45), "C")
        self.assertEqual(score_to_grade(20), "D")

    @patch("vcp_scanner.VCPScanner.run", return_value=[])
    def test_main_tolerates_invalid_env_values(self, _mock_run):
        old_env = os.environ.copy()
        try:
            os.environ["MIN_SCORE"] = "abc"
            os.environ["TREND_MIN_PASSED"] = "x9"
            os.environ["MIN_CONTRACTIONS"] = "none"
            os.environ["ONLY_BUY_RECOMMENDATION"] = "maybe"
            main()  # should fallback to defaults instead of raising ValueError
        finally:
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
