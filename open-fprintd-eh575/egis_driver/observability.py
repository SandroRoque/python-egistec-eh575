import re
from collections import Counter


METRIC_PATTERN = re.compile(r"\[METRIC\] component=(\w+) outcome=([\w:-]+)")


def summarize_metric_lines(lines):
    """Summarize privacy-safe service metrics from iterable journal lines."""
    components = {}
    total = 0
    for line in lines:
        match = METRIC_PATTERN.search(line)
        if not match:
            continue
        component, outcome = match.groups()
        components.setdefault(component, Counter())[outcome] += 1
        total += 1
    return {
        "schema_version": 1,
        "events": total,
        "components": {
            component: dict(sorted(counts.items()))
            for component, counts in sorted(components.items())
        },
    }
