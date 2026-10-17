"""Integrity checks cover malformed output, units, work mismatches and distinct HAL sensors."""

import json, pathlib, tempfile, unittest
from run_device import sensors, validate


class ValidationTests(unittest.TestCase):
    def check_rows(self, rows, mode="fixed"):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            (path / "results.jsonl").write_text("\n".join(json.dumps(p) for p in rows))
            return validate(path, mode, 128, 64)

    def valid(self):
        return {
            "status": "ok",
            "actual_prefill_tokens": 128,
            "actual_decode_tokens": 64,
            "native_prefill_tokens_per_s": 1280.0,
            "native_decode_tokens_per_s": 64.0,
            "native_ttft_s": 0.115625,
            "app_call_elapsed_ns": 2_000_000_000,
            "phase": "measure",
        }

    def test_valid(self):
        self.assertTrue(self.check_rows([self.valid()])["passed"])

    def test_wrong_seconds_units(self):
        row = self.valid()
        row["native_ttft_s"] *= 1000
        self.assertIn("native_ttft_formula_mismatch", self.check_rows([row])["issues"])

    def test_false_success_with_zero_work(self):
        row = self.valid()
        row["actual_decode_tokens"] = 0
        self.assertFalse(self.check_rows([row])["passed"])

    def test_wrong_budget(self):
        row = self.valid()
        row["actual_decode_tokens"] = 63
        self.assertIn("fixed_work_mismatch", self.check_rows([row])["issues"])

    def test_missing_nonfinite(self):
        for value in [None, float("nan"), float("inf")]:
            row = self.valid()
            row["native_decode_tokens_per_s"] = value
            self.assertFalse(self.check_rows([row])["passed"])

    def test_wall_time_unit_error(self):
        row = self.valid()
        row["app_call_elapsed_ns"] = 2_000_000
        self.assertIn(
            "native_work_duration_exceeds_app_wall", self.check_rows([row])["issues"]
        )

    def test_clock_factor_1000(self):
        self.assertFalse(
            self.check_rows(
                [{"status": "ok", "observed_elapsed_ns": 100_000}], mode="selftest"
            )["passed"]
        )

    def test_empty(self):
        self.assertFalse(self.check_rows([])["passed"])

    def test_native_failure_retained(self):
        self.assertIn(
            "non_ok_rows",
            self.check_rows([{"status": "error", "error": "engine creation failed"}])[
                "issues"
            ],
        )

    def test_hal_cache_does_not_override_current(self):
        raw = "Cached temperatures:\nTemperature{mValue=99.0, mType=3, mName=skin, mStatus=0}\nCurrent temperatures from HAL:\nTemperature{mValue=33.0, mType=3, mName=skin, mStatus=0}\nTemperature{mValue=79.0, mType=9, mName=nsp3, mStatus=1}\nCurrent cooling devices from HAL:"
        values = sensors(raw)
        self.assertEqual(
            [(p["name"], p["type"], p["value"]) for p in values],
            [("skin", 3, 33.0), ("nsp3", 9, 79.0)],
        )


if __name__ == "__main__":
    unittest.main()
