"""Privacy-safe aggregation of live verification latency log records."""

from collections import Counter
import re

import numpy as np


PAIR = re.compile(r"([a-z_]+)=([^\s]+)")
LATENCY_KEYS = (
    "request_to_touch_ms", "touch_to_first_frame_ms", "touch_to_decision_ms",
    "capture_ms", "queue_ms", "matching_ms", "shadow_extraction_ms",
    "shadow_comparison_ms",
)


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
            record["shadow_frames"] = int(fields.get("shadow_frames", 0))
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
        "shadow_frames": distribution(
            [item["shadow_frames"] for item in sessions]),
    }
