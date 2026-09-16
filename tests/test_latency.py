import unittest

from egis_driver.latency import summarize_latency_log


class LatencyReportTests(unittest.TestCase):
    def test_aggregates_sessions_without_identity_or_biometric_data(self):
        text = """
[LATENCY] outcome=match request_to_touch_ms=100 touch_to_first_frame_ms=20 touch_to_decision_ms=400 capture_ms=90 queue_ms=10 matching_ms=200 shadow_extraction_ms=30 shadow_comparison_ms=40 shadow_frames=3 attempts=3
[LATENCY] outcome=retry request_to_touch_ms=200 touch_to_first_frame_ms=40 touch_to_decision_ms=800 capture_ms=180 queue_ms=20 matching_ms=400 shadow_extraction_ms=60 shadow_comparison_ms=80 shadow_frames=6 attempts=6
[METRIC] component=dbus_verify_status result=verify-match done=True queue_ms=1.5
"""
        report = summarize_latency_log(text)
        self.assertEqual(report["sessions"], 2)
        self.assertEqual(report["outcomes"], {"match": 1, "retry": 1})
        self.assertEqual(report["latency_ms"]["touch_to_decision_ms"]["p50"], 600)
        self.assertEqual(report["dbus_queue_ms"]["p95"], 1.5)
        self.assertNotIn("identity", str(report))

    def test_empty_log_has_no_fabricated_measurements(self):
        report = summarize_latency_log("unrelated\n")
        self.assertEqual(report["sessions"], 0)
        self.assertIsNone(report["latency_ms"]["matching_ms"]["p95"])
