"""Privacy-safe aggregation of live verification latency log records."""

from collections import Counter
import math
import re

import numpy as np


PAIR = re.compile(r"([a-z_]+)=([^\s]+)")
LATENCY_KEYS = (
    "request_to_touch_ms", "touch_to_first_frame_ms", "touch_to_decision_ms",
    "capture_ms", "queue_ms", "matching_ms",
)


def derive_contact_budget_ms(third_accept_ms, inter_frame_ms,
                             minimum_confirmations):
    """Derive a 100ms-rounded budget from genuine confirmation evidence."""
    confirmations = [float(value) for value in third_accept_ms]
    intervals = [float(value) for value in inter_frame_ms]
    if len(confirmations) < minimum_confirmations or not intervals:
        return None
    if any(not math.isfinite(value) or value < 0
           for value in confirmations + intervals):
        raise ValueError("contact-budget evidence must be finite and non-negative")
    raw = np.percentile(confirmations, 95) + np.percentile(intervals, 95)
    return int(math.ceil(raw / 100.0) * 100)


def summarize_latency_log(text):
    sessions = []
    dbus_queue = []
    for line in text.splitlines():
        if "[LATENCY]" in line:
            fields = dict(PAIR.findall(line.split("[LATENCY]", 1)[1]))
            record = {"outcome": fields.get("outcome", "unknown")}
            for key in LATENCY_KEYS:
                value = fields.get(key)
                record[key] = (float(value) if value not in (None, "none") else None)
            record["attempts"] = int(fields.get("attempts", 0))
            record["max_consecutive_accepts"] = int(
                fields.get("max_consecutive_accepts", 0))
            record["deadline_expired"] = fields.get(
                "deadline_expired", "false") == "true"
            record["accepted_attempt_ms"] = [
                float(value) for value in
                fields.get("accepted_attempt_ms", "").split(",")
                if value and value != "none"
            ]
            record["inter_frame_ms"] = [
                float(value) for value in
                fields.get("inter_frame_ms", "").split(",")
                if value and value != "none"
            ]
            sessions.append(record)
        elif "component=dbus_verify_status" in line:
            fields = dict(PAIR.findall(line))
            if "queue_ms" in fields:
                dbus_queue.append(float(fields["queue_ms"]))

    def distribution(values):
        values = [value for value in values if value is not None]
        if not values:
            return {"count": 0, "p50": None, "p95": None, "max": None}
        return {
            "count": len(values),
            "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)),
            "max": float(max(values)),
        }

    outcomes = sorted({item["outcome"] for item in sessions})
    return {
        "schema_version": 1,
        "sessions": len(sessions),
        "outcomes": dict(sorted(Counter(
            item["outcome"] for item in sessions).items())),
        "latency_ms": {
            key: distribution([item[key] for item in sessions])
            for key in LATENCY_KEYS
        },
        "dbus_queue_ms": distribution(dbus_queue),
        "attempts": distribution([item["attempts"] for item in sessions]),
        "attempts_by_outcome": {
            outcome: distribution([
                item["attempts"] for item in sessions
                if item["outcome"] == outcome
            ])
            for outcome in outcomes
        },
        "max_consecutive_accepts": distribution([
            item["max_consecutive_accepts"] for item in sessions]),
        "third_accept_ms": distribution([
            item["accepted_attempt_ms"][2]
            for item in sessions
            if len(item["accepted_attempt_ms"]) >= 3
        ]),
        "inter_frame_ms": distribution([
            value for item in sessions for value in item["inter_frame_ms"]
        ]),
        "deadline_expired": sum(
            item["deadline_expired"] for item in sessions),
    }
