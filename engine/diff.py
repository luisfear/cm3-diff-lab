"""diff.py -- generic comparison of two observations.

An "observation" is a plain dict of observable outputs collected after running a
test on one firmware image (bytes sent on each link, frames on the bus, output
pin transitions, memory-mapped state that changed, restart cause, and so on).
This module compares two observations field by field and returns a list of
differences, each tagged with a class. Every difference is "real" unless a
caller-supplied classifier downgrades it (for example, a plug-in that knows some
fields legitimately differ by a version string or by scheduling phase).

Nothing here is device-specific: the set of fields and the classifiers come from
the caller.
"""

from __future__ import annotations

import hashlib
import json


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def digest(obj) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def compare_observations(a: dict, b: dict, classifiers: dict | None = None) -> list[dict]:
    """Return the differing fields. `classifiers` maps a field name to a function
    (value_a, value_b) -> class_name | None; returning None (or no classifier)
    keeps the default class "real"."""
    classifiers = classifiers or {}
    diffs = []
    for field in sorted(set(a) | set(b)):
        va, vb = a.get(field), b.get(field)
        if va == vb:
            continue
        clazz = "real"
        fn = classifiers.get(field)
        if fn is not None:
            result = fn(va, vb)
            if result:
                clazz = result
        diffs.append({"field": field, "class": clazz})
    return diffs


def has_real(diffs: list[dict]) -> bool:
    return any(d.get("class", "real") == "real" for d in diffs)


def classes(diffs: list[dict]) -> set:
    return {d.get("class", "real") for d in diffs}
