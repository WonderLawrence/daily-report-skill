"""Configuration validation and collection window resolution."""
import unittest
from datetime import date
from pathlib import Path

from helpers import config, run, timewindow, TZ


class TestResolvePeriod(unittest.TestCase):
    THU = date(2026, 10, 15)

    def bounds(self, *args, **kwargs):
        period = timewindow.resolve_period(*args, **kwargs)
        return period.start.date(), period.last_day.date()

    def test_this_week_runs_monday_to_today(self):
        self.assertEqual(self.bounds(None, 1, TZ, week="this", today=self.THU),
                         (date(2026, 10, 12), date(2026, 10, 15)))

    def test_this_week_on_sunday_is_full_week(self):
        self.assertEqual(self.bounds(None, 1, TZ, week="this", today=date(2026, 10, 18)),
                         (date(2026, 10, 12), date(2026, 10, 18)))

    def test_last_week_is_previous_monday_to_sunday(self):
        self.assertEqual(self.bounds(None, 1, TZ, week="last", today=self.THU),
                         (date(2026, 10, 5), date(2026, 10, 11)))

    def test_yesterday_and_today_keywords(self):
        self.assertEqual(self.bounds("yesterday", 1, TZ, today=self.THU),
                         (date(2026, 10, 14), date(2026, 10, 14)))
        self.assertEqual(self.bounds("today", 2, TZ, today=self.THU),
                         (date(2026, 10, 14), date(2026, 10, 15)))

    def test_explicit_date_and_days(self):
        self.assertEqual(self.bounds("2026-10-16", 3, TZ),
                         (date(2026, 10, 14), date(2026, 10, 16)))

    def test_bad_date_mentions_format(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            timewindow.resolve_period("2026-13-01", 1, TZ)

class TestPeriodLimits(unittest.TestCase):
    def test_a_window_outside_the_representable_range_is_a_usable_error(self):
        """Date math used to raise OverflowError, which reached the user as a traceback."""
        for args in ((None, 3000000), ("9999-12-31", 1)):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ValueError, "超出可表示范围"):
                    timewindow.resolve_period(*args, TZ)

    def test_the_cli_reports_it_without_a_traceback(self):
        result = run(["--days", "3000000", "--no-git", "--no-sessions", "--print"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("超出可表示范围", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class TestValidateConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(config.validate_config(config.load_config(Path("/nonexistent"))), [])

    def test_rejects_bad_values(self):
        cfg = config.load_config(Path("/nonexistent"))
        cfg.update(author_emails="a@b.c", utc_offset_hours=99, max_sessions=0,
                   dirty="yes", reports_dir="")
        cfg["sources"] = {"git": "yes"}
        joined = "；".join(config.validate_config(cfg))
        for key in ("author_emails", "utc_offset_hours", "max_sessions", "dirty",
                    "reports_dir", "sources"):
            self.assertIn(key, joined)


if __name__ == "__main__":
    unittest.main()
