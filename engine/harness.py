"""harness.py -- the differential test runner.

Loads a plug-in, builds its tests (optionally just one chunk of them), runs each
test on both firmware images in parallel, compares the two observations, and
writes a single *sealed* result file. The only thing printed to stdout is a set
of counters and a fingerprint -- never a test identifier, an address or a symbol.

The sealed result (readable locally with tools/open_results.py) holds the full
detail: per-test observation digests, which tests differ for real, and the diff
of each. Join several chunk results with `join_sealed`.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
import traceback
from pathlib import Path

from . import box, chunking
from .diff import compare_observations, digest, has_real, classes
from .plugin import load_plugin
from .progress import Progress

_WORKER = {}     # per-process cache: plug-in, classifiers


def _init_worker(plugin_dir: str):
    plugin = load_plugin(plugin_dir)
    plugin.setup()
    _WORKER["plugin"] = plugin
    _WORKER["classifiers"] = plugin.classifiers()
    # A plug-in may provide its own comparison: the rules that decide which
    # differences are benign can themselves be device-specific knowledge, so they
    # belong in the (encrypted) plug-in. Otherwise the generic field comparison
    # with the plug-in's classifiers is used.
    _WORKER["compare"] = getattr(plugin, "compare", None)


def _run_batch(tests: list[dict]) -> list[dict]:
    plugin = _WORKER["plugin"]
    classifiers = _WORKER["classifiers"]
    custom = _WORKER["compare"]
    out = []
    for test in tests:
        tid = test["id"]
        try:
            obs_a, obs_b = plugin.run_pair(test)
            diffs = custom(obs_a, obs_b) if custom else compare_observations(obs_a, obs_b, classifiers)
            out.append({"id": tid, "group": test.get("group", ""),
                        "digest": digest([obs_a, obs_b]),
                        "real": has_real(diffs), "classes": sorted(classes(diffs)),
                        "detail": diffs})
        except Exception:
            # Keep the public world clean: the traceback (paths, symbols) goes in
            # the sealed detail only; the digest marks the failure.
            out.append({"id": tid, "group": test.get("group", ""),
                        "digest": "error", "real": True, "classes": ["harness-error"],
                        "detail": {"error": traceback.format_exc()}})
    return out


def _cost(test: dict) -> int:
    return int(test.get("cost", 100))


def run_chunk(plugin_dir: str, key, fast: bool, index: int, n: int, procs: int,
              out_path: str, batch: int = 40) -> dict:
    passphrase = box.read_passphrase(key)
    plugin = load_plugin(plugin_dir)
    tests = plugin.build_tests(fast)
    if n > 1:
        tests = chunking.split(tests, index, n, cost=_cost, key=lambda t: t["id"],
                               group_cost=3000, group_of=plugin.group_of)
    batches = [tests[i:i + batch] for i in range(0, len(tests), batch)]
    prog = Progress(f"chunk-{index}-of-{n}", len(tests), "differential run")

    results = {}
    ctx = mp.get_context("spawn")
    done = 0
    with ctx.Pool(procs, initializer=_init_worker, initargs=(plugin_dir,)) as pool:
        for batch_result in pool.imap_unordered(_run_batch, batches):
            for r in batch_result:
                results[r["id"]] = r
            done += len(batch_result)
            real_so_far = sum(1 for r in results.values() if r["real"])
            prog.step(done, f"real so far: {real_so_far}")
    prog.done()

    real = sorted(i for i, r in results.items() if r["real"])
    fingerprint = _fingerprint({i: r["digest"] for i, r in results.items()})
    sealed = {"n": n, "index": index, "count": len(results), "real_count": len(real), "fingerprint": fingerprint,
              "results": {i: r["digest"] for i, r in results.items()},
              "real": real, "detail": {i: r["detail"] for i, r in results.items()},
              "groups": {i: r["group"] for i, r in results.items()}}
    Path(out_path).write_bytes(box.seal_bytes(json.dumps(sealed).encode(), passphrase))

    summary = {"count": len(results), "real_count": len(real), "fingerprint": fingerprint}
    print(json.dumps(summary))   # public: counters and a hash only
    return summary


def _fingerprint(results: dict) -> str:
    import hashlib
    return hashlib.sha256("\n".join(f"{k}:{results[k]}" for k in sorted(results)).encode()).hexdigest()


def join_sealed(part_paths: list[str], key, out_path: str | None = None) -> dict:
    passphrase = box.read_passphrase(key)
    parts = [json.loads(box.unseal_bytes(Path(p).read_bytes(), passphrase)) for p in part_paths]
    merged = chunking.join(parts)
    # Carry the detail across for local inspection, still sealed.
    detail = {}
    for p in parts:
        detail.update(p.get("detail", {}))
    out = {**merged, "detail": detail}
    if out_path:
        Path(out_path).write_bytes(box.seal_bytes(json.dumps(out).encode(), passphrase))
    print(json.dumps({"count": merged["count"], "real_count": merged["real_count"],
                      "fingerprint": merged["fingerprint"]}))
    return out


def _main(argv: list[str]):
    import argparse
    ap = argparse.ArgumentParser(description="differential test runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--plugin", required=True, help="unsealed plug-in directory")
    r.add_argument("--key", required=True, help="passphrase file or value")
    r.add_argument("--out", required=True, help="sealed result file to write")
    r.add_argument("--fast", action="store_true")
    r.add_argument("--index", type=int, default=1)
    r.add_argument("--n", type=int, default=1)
    r.add_argument("--procs", type=int, default=2)
    j = sub.add_parser("join")
    j.add_argument("parts", nargs="+")
    j.add_argument("--key", required=True)
    j.add_argument("--out", default=None)
    op = ap.parse_args(argv)
    if op.cmd == "run":
        run_chunk(op.plugin, op.key, op.fast, op.index, op.n, op.procs, op.out)
    else:
        join_sealed(op.parts, op.key, op.out)


if __name__ == "__main__":
    _main(sys.argv[1:])
