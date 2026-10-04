"""chunking.py -- deterministic, balanced work splitting and joining.

A long test list can be run on several machines at once. `split` partitions the
tests into N chunks that, taken together, are exactly the originals (no test
repeated or lost), balanced by an estimated cost so the slowest chunk -- which
fixes the wall-clock time -- is as small as possible. `join` merges the N result
files back into one, checks that all chunks are present and non-overlapping, and
computes a hash of the merged result so independent runs can be shown identical.

The cost estimate and the test identity come from small callables the caller
supplies, so nothing here is specific to any firmware.
"""

from __future__ import annotations

import hashlib
import json


def split(tests: list, index: int, n: int, cost, key, group_cost=0, group_of=None):
    """Return chunk `index` (1..n) of `tests`.

    cost(test) -> int       relative cost of running a test.
    key(test) -> str        a stable identity (for a deterministic tie-break).
    group_cost              extra cost the first test of a new group adds to a
                            chunk (e.g. building a fresh snapshot).
    group_of(test) -> hash  group a test belongs to (None: all in one group).
    """
    if not 1 <= index <= n:
        raise ValueError(f"chunk {index}/{n}: expected 1 <= index <= n")
    group_of = group_of or (lambda t: "")
    load = [0] * n
    groups = [set() for _ in range(n)]
    assigned = {}
    for t in sorted(tests, key=lambda x: (-cost(x), key(x))):
        c = cost(t)

        def total(k):
            return load[k] + c + (0 if group_of(t) in groups[k] else group_cost)
        k = min(range(n), key=lambda j: (total(j), j))
        load[k] = total(k)
        groups[k].add(group_of(t))
        assigned[key(t)] = k
    return [t for t in tests if assigned[key(t)] == index - 1]


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def join(parts: list[dict]) -> dict:
    """Merge chunk result dicts of the shape produced by the harness:
    {"n": N, "index": I, "results": {test_id: observation_hash}, "real": [ids]}.
    Returns the merged result plus a reproducible fingerprint."""
    if not parts:
        raise ValueError("no chunks to join")
    ns = {p["n"] for p in parts}
    if len(ns) != 1:
        raise ValueError(f"chunks from different splits: {sorted(ns)}")
    n = ns.pop()
    present = {p["index"] for p in parts}
    missing = [i for i in range(1, n + 1) if i not in present]
    if missing:
        raise ValueError(f"missing chunks {missing} of {n}")
    results, real = {}, set()
    for p in parts:
        overlap = results.keys() & p["results"].keys()
        if overlap:
            raise ValueError(f"tests repeated across chunks: {len(overlap)}")
        results.update(p["results"])
        real.update(p.get("real", []))
    fingerprint = hashlib.sha256("\n".join(f"{k}:{results[k]}" for k in sorted(results)).encode()).hexdigest()
    return {"n": n, "count": len(results), "real_count": len(real),
            "results": results, "real": sorted(real), "fingerprint": fingerprint}
