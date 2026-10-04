"""phase.py -- do the differences survive a change of scheduling phase?

On real silicon, where the periodic tick lands inside the main loop depends on
how many instructions each build takes; the relative order of two jobs due in the
same tick, or how many periodic writes fit in a window, varies with it -- even
for one image compared against itself. So a difference that disappears at some
phase cannot be blamed on the second build.

For each test the first pass flagged as real, this re-runs both images at the
phase variants the plug-in offers. If some pair of variants shows no real
difference, the test is "phase-sensitive"; otherwise it is "confirmed". The
verdict file is sealed; stdout shows only counts.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

from . import box
from .diff import compare_observations, has_real
from .plugin import load_plugin
from .progress import Progress

_WORKER = {}


def _init_worker(plugin_dir: str):
    plugin = load_plugin(plugin_dir)
    plugin.setup()
    _WORKER["plugin"] = plugin
    _WORKER["classifiers"] = plugin.classifiers()
    _WORKER["compare"] = getattr(plugin, "compare", None)


def _run_variant(variant: dict) -> dict:
    plugin = _WORKER["plugin"]
    obs_a, obs_b = plugin.run_pair(variant)
    custom = _WORKER["compare"]
    diffs = custom(obs_a, obs_b) if custom else compare_observations(obs_a, obs_b, _WORKER["classifiers"])
    return {"origin": variant.get("origin", variant["id"]), "id": variant["id"], "real": has_real(diffs)}


def confirm(plugin_dir: str, key, real_ids: list[str], fast: bool, procs: int,
            out_path: str) -> dict:
    passphrase = box.read_passphrase(key)
    plugin = load_plugin(plugin_dir)
    if hasattr(plugin, "confirm"):
        # The plug-in brings its own, fuller confirmation: use it so the verdict matches the trusted machine.
        plugin.setup()
        sealed = plugin.confirm(list(real_ids), fast, procs)
        Path(out_path).write_bytes(box.seal_bytes(json.dumps(sealed).encode(), passphrase))
        print(json.dumps({"confirmed": len(sealed["confirmed"]), "phase_sensitive": len(sealed["phase_sensitive"]),
                          "variants": sealed.get("variant_count", 0)}))
        return sealed
    by_id = {t["id"]: t for t in plugin.build_tests(fast)}
    variants = []
    for tid in real_ids:
        base = by_id.get(tid)
        if base is None:
            continue
        for v in plugin.phase_variants(base):
            v = dict(v)
            v.setdefault("origin", tid)
            variants.append(v)
    prog = Progress("phase", max(1, len(variants)), "phase rounds")

    survived = {tid: True for tid in real_ids}      # True = still real in every variant seen
    ctx = mp.get_context("spawn")
    done = 0
    with ctx.Pool(procs, initializer=_init_worker, initargs=(plugin_dir,)) as pool:
        for res in pool.imap_unordered(_run_variant, variants):
            if not res["real"]:
                survived[res["origin"]] = False     # a phase with no real diff explains it
            done += 1
            prog.step(done)
    prog.done()

    confirmed = sorted(t for t, still in survived.items() if still)
    phase_sensitive = sorted(t for t, still in survived.items() if not still)
    sealed = {"confirmed": confirmed, "phase_sensitive": phase_sensitive,
              "variant_count": len(variants)}
    Path(out_path).write_bytes(box.seal_bytes(json.dumps(sealed).encode(), passphrase))
    print(json.dumps({"confirmed": len(confirmed), "phase_sensitive": len(phase_sensitive),
                      "variants": len(variants)}))
    return sealed


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="phase confirmation")
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--result", required=True, help="sealed result file from the run step")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--procs", type=int, default=2)
    op = ap.parse_args(argv)
    passphrase = box.read_passphrase(op.key)
    result = json.loads(box.unseal_bytes(Path(op.result).read_bytes(), passphrase))
    confirm(op.plugin, op.key, result.get("real", []), op.fast, op.procs, op.out)


if __name__ == "__main__":
    _main(sys.argv[1:])
