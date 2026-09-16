import unittest

from egis_driver.latency import derive_contact_budget_ms, summarize_latency_log


class LatencyReportTests(unittest.TestCase):
    def test_aggregates_sessions_without_identity_or_biometric_data(self):
        text = """
[LATENCY] outcome=match request_to_touch_ms=100 touch_to_first_frame_ms=20 touch_to_decision_ms=400 capture_ms=90 queue_ms=10 matching_ms=200 attempts=3 max_consecutive_accepts=3 accepted_attempt_ms=100,220,390 inter_frame_ms=30,35 deadline_expired=false
[LATENCY] outcome=retry request_to_touch_ms=200 touch_to_first_frame_ms=40 touch_to_decision_ms=800 capture_ms=180 queue_ms=20 matching_ms=400 attempts=6 max_consecutive_accepts=2 accepted_attempt_ms=500,700 inter_frame_ms=40,45 deadline_expired=true
[METRIC] component=dbus_verify_status result=verify-match done=True queue_ms=1.5
"""
        report = summarize_latency_log(text)
        self.assertEqual(report["sessions"], 2)
        self.assertEqual(report["outcomes"], {"match": 1, "retry": 1})
        self.assertEqual(report["latency_ms"]["touch_to_decision_ms"]["p50"], 600)
        self.assertEqual(report["dbus_queue_ms"]["p95"], 1.5)
        self.assertEqual(report["attempts_by_outcome"]["match"]["p50"], 3)
        self.assertEqual(report["third_accept_ms"]["p50"], 390)
        self.assertEqual(report["max_consecutive_accepts"]["max"], 3)
        self.assertEqual(report["deadline_expired"], 1)
        self.assertNotIn("identity", str(report))

    def test_empty_log_has_no_fabricated_measurements(self):
        report = summarize_latency_log("unrelated\n")
        self.assertEqual(report["sessions"], 0)
        self.assertIsNone(report["latency_ms"]["matching_ms"]["p95"])

    def test_contact_budget_is_derived_and_requires_enough_evidence(self):
        self.assertIsNone(derive_contact_budget_ms(
            [1000, 1200], [30, 35], minimum_confirmations=3))
        self.assertEqual(
            derive_contact_budget_ms(
                [1000, 1200, 1400], [30, 35, 40], minimum_confirmations=3),
            1500,
        )
