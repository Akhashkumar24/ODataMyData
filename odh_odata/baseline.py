"""Known-issues baseline: tell new findings apart from accepted ones, so CI fails on regressions only.

A finding is identified by set | category | test name, with sampled values masked: the rows sampled change
between runs, so "Status eq 'Active'" and "Status eq 'Retired'" are the same test."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

RANK = {"PASS": 0, "INFO": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
_MASKS = [
    (re.compile(r"'(?:[^']|'')*'"), "'~'"),  # string literals
    (re.compile(r"\$search=\S+"), "$search=~"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "~guid"),
    (re.compile(r"\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:\d{2})?)?"), "~date"),
    (re.compile(r"(?<![A-Za-z_])-?\d+(\.\d+)?"), "#"),  # numbers, but not digits inside names like Address2
]


def _get(r, k):
    return r[k] if isinstance(r, dict) else getattr(r, k)


def test_id(r):
    name = _get(r, "name")
    for rx, sub in _MASKS:
        name = rx.sub(sub, name)
    return f"{_get(r, 'set')} | {_get(r, 'category')} | {name}"


def findings(results):
    """{test id: worst outcome} for every WARN/FAIL. Several masked-equal tests collapse to their worst outcome."""
    out = {}
    for r in results:
        o = _get(r, "outcome")
        if RANK.get(o, 0) and RANK[o] > RANK.get(out.get(test_id(r)), 0):
            out[test_id(r)] = o
    return out


def compare(results, known):
    """(new, fixed, still_open). new: a WARN/FAIL not in known, or WARN that became FAIL.
    fixed: a known finding whose test ran this time and no longer warns or fails."""
    cur, ran = findings(results), {test_id(r) for r in results}
    new = {k: o for k, o in cur.items() if RANK[o] > RANK.get(known.get(k), 0)}
    fixed = {k: o for k, o in known.items() if k in ran and k not in cur}
    still = {k: o for k, o in cur.items() if k not in new}
    return new, fixed, still


def load(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")).get("findings", {}) if p.exists() else {}


def save(path, results, service):
    """Accept the current findings. Entries for tests that did not run this time (e.g. --only) are kept."""
    ran = {test_id(r) for r in results}
    merged = {k: o for k, o in load(path).items() if k not in ran}
    merged.update(findings(results))
    Path(path).write_text(json.dumps({"service": service, "updated": datetime.now().isoformat(timespec="seconds"),
                                      "findings": dict(sorted(merged.items()))}, indent=1), encoding="utf-8")
    return len(merged)


def mark(results, new):
    for r in results:
        if test_id(r) in new and _get(r, "outcome") == new[test_id(r)]:
            r.baseline = "new"
