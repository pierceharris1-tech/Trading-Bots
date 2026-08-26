from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from bots.choch_live.s1_structure import pending_limit_filled, retracement_price
from bots.choch_live.s2_continuation import original_exec_ohlc, price_to_original_r


REFERENCE_DIR = Path(__file__).resolve().parents[1] / "research" / "reference"


class TestChochReferenceRegression(unittest.TestCase):
    def test_required_notebook_function_sources_are_present(self):
        notebooks = [
            REFERENCE_DIR / "choch_v10_two_cell_quantconnect_FIXED.ipynb",
            REFERENCE_DIR / "choch_strategy2_evolutionary_execution_search.ipynb",
        ]
        required = {
            "def add_atr(",
            "def detect_structure_events(",
            "def build_supersession_maps(",
            "def retracement_price(",
            "def simulate_dynamic_trade(",
            "def collect_frozen_entries(",
        }
        s2_required = {
            "def original_exec_ohlc(",
            "def price_to_original_r(",
            "def simulate_reversal_from_feature(",
        }
        combined = ""
        for notebook in notebooks:
            data = json.loads(notebook.read_text())
            combined += "\n".join(
                "".join(cell.get("source", []))
                for cell in data.get("cells", [])
                if cell.get("cell_type") == "code"
            )
        for needle in sorted(required | s2_required):
            with self.subTest(function=needle):
                self.assertIn(needle, combined)

    def test_formula_level_reference_behaviour_matches_production(self):
        self.assertAlmostEqual(retracement_price(100.0, 104.0, "bullish"), 101.528)
        self.assertAlmostEqual(retracement_price(104.0, 100.0, "bearish"), 102.472)
        self.assertTrue(pending_limit_filled(pd.Series({"asklow": 101.52}), "bullish", 101.528))
        self.assertTrue(pending_limit_filled(pd.Series({"bidhigh": 102.48}), "bearish", 102.472))

        bar = pd.Series(
            {
                "bidopen": 1.0,
                "bidhigh": 1.2,
                "bidlow": 0.9,
                "bidclose": 1.1,
                "askopen": 1.01,
                "askhigh": 1.21,
                "asklow": 0.91,
                "askclose": 1.11,
            }
        )
        self.assertEqual(original_exec_ohlc(bar, "bullish"), (1.0, 1.2, 0.9, 1.1))
        self.assertEqual(original_exec_ohlc(bar, "bearish"), (1.01, 1.21, 0.91, 1.11))
        self.assertAlmostEqual(price_to_original_r(115.0, 100.0, 10.0, "bullish"), 1.5)
        self.assertAlmostEqual(price_to_original_r(85.0, 100.0, 10.0, "bearish"), 1.5)


if __name__ == "__main__":
    unittest.main()
