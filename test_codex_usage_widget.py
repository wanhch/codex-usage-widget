import unittest
from unittest.mock import Mock, patch

import codex_usage_widget as widget


class ParseCodexRateLimitsTests(unittest.TestCase):
    @patch("codex_usage_widget.time.time", return_value=1_000_000)
    def test_maps_windows_by_duration_not_primary_order(self, _mock_time):
        response = {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 31,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_604_800,
                    },
                    "secondary": {
                        "usedPercent": 12,
                        "windowDurationMins": 300,
                        "resetsAt": 1_018_000,
                    },
                }
            }
        }

        five, week = widget.parse_codex_rate_limits(response)

        self.assertEqual(five["percent"], 88)
        self.assertEqual(week["percent"], 69)
        self.assertEqual(five["reset_fraction"], 1.0)
        self.assertEqual(week["reset_fraction"], 1.0)

    def test_supports_account_with_only_weekly_window(self):
        response = {
            "rateLimits": {
                "primary": {
                    "usedPercent": 44,
                    "windowDurationMins": 10_080,
                    "resetsAt": None,
                },
                "secondary": None,
            }
        }

        five, week = widget.parse_codex_rate_limits(response)

        self.assertIsNone(five["percent"])
        self.assertEqual(week["percent"], 56)

    def test_web_percentage_marked_remaining_is_not_inverted(self):
        result = widget.extract_usage(
            ["Weekly usage limit", "8%", "remaining", "Resets tomorrow"],
            "Week",
        )

        self.assertEqual(result["percent"], 8)
        self.assertEqual(result["value"], "8% remaining")

    def test_web_percentage_marked_used_is_converted_to_remaining(self):
        result = widget.extract_usage(
            ["Weekly usage limit", "32% used", "Resets tomorrow"],
            "Week",
        )

        self.assertEqual(result["percent"], 68)
        self.assertEqual(result["value"], "68% remaining")


class KimiPollingTests(unittest.TestCase):
    @patch("codex_usage_widget.find_kimi_cli", return_value="kimi.exe")
    @patch("codex_usage_widget.find_free_port", return_value=58649)
    @patch("codex_usage_widget.port_is_open", return_value=True)
    @patch("codex_usage_widget.subprocess.Popen")
    def test_one_quota_request_per_poll_even_after_timeout(self, popen, *_):
        client = widget.KimiUsageClient({})
        popen.return_value.poll.return_value = None
        with patch.object(client, "_request_usage", side_effect=[1, TimeoutError(), 3]) as request:
            self.assertEqual(client.query(), 1)
            with self.assertRaises(TimeoutError):
                client.query()
            self.assertEqual(client.query(), 3)
        self.assertEqual(request.call_count, 3)
        popen.assert_called_once()

    @patch("codex_usage_widget.log")
    def test_timeout_marks_recent_reading_then_expires_and_recovers(self, _log):
        app = widget.UsageWidget.__new__(widget.UsageWidget)
        app.show_kimi_usage = True
        app.last_kimi_reading = {}
        app.last_kimi_read_at = 0
        data = {"quota": {"usages": {"limit7d": {"usedRatio": 0.25}}}}
        app.kimi_client = Mock()
        app.kimi_client.query.side_effect = [data, TimeoutError(), TimeoutError(), data]
        def poll(now):
            with patch("codex_usage_widget.time.monotonic", return_value=now):
                return app._add_kimi_usage({})

        fresh = poll(100)
        stale = poll(130)
        self.assertEqual(stale["kimi_week"]["percent"], 75)
        self.assertEqual(stale["kimi_week"]["reset_text"], "stale")
        self.assertIsNone(stale["kimi_five"]["percent"])
        self.assertIsNone(poll(221)["kimi_week"]["percent"])
        self.assertEqual(poll(250), fresh)


class ParseKimiUsageTests(unittest.TestCase):
    @patch("codex_usage_widget.time.time", return_value=0)
    def test_v2_ratios_and_reset_countdowns(self, _mock_time):
        data = {"kind": "ok", "quota": {"usages": {
            "limit5h": {"usedRatio": 0, "resetAt": "1970-01-01T02:30:00Z"},
            "limit7d": {"usedRatio": 0.022621, "resetAt": "1970-01-04T12:00:00Z"},
        }, "extraUsage": None}}

        five, week = widget.parse_kimi_usage(data)

        self.assertEqual(five["percent"], 100)
        self.assertAlmostEqual(week["percent"], 97.7379)
        self.assertEqual(five["reset_fraction"], 0.5)
        self.assertEqual(week["reset_fraction"], 0.5)
        self.assertTrue(five["reset_text"])
        self.assertTrue(week["reset_text"])

    def test_v2_missing_ratio_is_unknown_not_full(self):
        for missing in (None, {}, {"usedRatio": None}):
            with self.subTest(missing=missing):
                five, week = widget.parse_kimi_usage({"quota": {"usages": {
                    "limit5h": missing,
                    "limit7d": {"usedRatio": 1.2},
                }}})
                self.assertIsNone(five["percent"])
                self.assertEqual(week["percent"], 0)

    def test_maps_five_hour_and_weekly_remaining_percent(self):
        data = {
            "summary": {
                "window": {"duration": 1, "unit": "week"},
                "used": 91,
                "limit": 100,
                "reset_at": None,
            },
            "limits": [
                {
                    "window": {"duration": 5, "unit": "hour"},
                    "used": 0,
                    "limit": 100,
                    "reset_at": None,
                }
            ],
        }

        five, week = widget.parse_kimi_usage(data)

        self.assertEqual(five["percent"], 100)
        self.assertEqual(week["percent"], 9)


if __name__ == "__main__":
    unittest.main()
