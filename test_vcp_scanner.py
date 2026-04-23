import unittest
import numpy as np
import pandas as pd

from vcp_scanner import AnalystEngine, VCPScanner


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
        self.assertGreater(plan["entry"], plan["stop_loss"])
        self.assertGreater(plan["take_profit"], plan["entry"])
        self.assertGreater(plan["risk_reward"], 0)
        self.assertTrue({"momentum_ok", "trend_ok", "volume_ok"} <= set(plan["analyst_flags"].keys()))


class TestScannerDefaults(unittest.TestCase):
    def test_default_config(self):
        scanner = VCPScanner()
        self.assertFalse(scanner.only_buy_recommendation)
        self.assertIn("not_buy_signal", scanner.rejections)


if __name__ == "__main__":
    unittest.main()
