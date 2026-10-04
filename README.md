# Differential testing harness for Cortex-M3 firmware images

A small, reusable engine that runs two firmware images of the same program on an
emulated Cortex-M3 core (built on [Unicorn](https://www.unicorn-engine.org/)),
feeds them the same inputs, and reports where their observable behaviour differs.
It is meant for checking that a rebuilt image behaves like a reference image.

The public part of this repository is **generic**: it knows nothing about any
particular board. It provides

* `engine/core.py` — a configurable Cortex-M3 machine (memory, reset,
  interrupts and the periodic tick, fault handling, snapshots, a native
  instruction counter, optional bit-band aliasing);
* `engine/diff.py` — a field-by-field comparison of two observations;
* `engine/chunking.py` — deterministic, balanced work splitting and joining;
* `engine/phase.py` — re-running differences across scheduling phases;
* `engine/harness.py` — a parallel runner that produces counters and a
  fingerprint, and seals the full detail;
* `tools/` — sealing, unsealing and reading encrypted archives and results.

All addresses and all device behaviour come from a **plug-in** that is loaded at
run time from an **encrypted archive** (`payload.bin`). The plug-in holds the
peripheral and chip models, the test suites, the extracted tables, the symbols
and the firmware images. None of that is in this repository.

## Try the engine without an archive

```
python -m engine.selftest
```

This builds two tiny synthetic Thumb images that differ by one byte, runs them
through the engine, and confirms the difference is detected. No archive, no
secret, no network.

## Running with an archive

The encrypted archive is unsealed into a temporary directory and the engine
loads the plug-in from there:

```
python tools/unseal.py payload.bin /tmp/plugin --key <passphrase-file>
python -m engine.harness run --plugin /tmp/plugin --key <passphrase-file> \
       --out result.bin --fast --procs 2
python tools/open_results.py result.bin --key <passphrase-file>
```

The runner prints only counters and a hash; `result.bin` is encrypted with the
same passphrase and holds the detail.

## Continuous integration

Two manual workflows live in `.github/workflows/`:

* `quick.yml` — one reduced pass;
* `pipeline.yml` — the full run split across parallel jobs and joined.

Both are **`workflow_dispatch` only** (they must never run from forks or
untrusted events, because they use a secret), they unseal the archive, run the
engine, and upload **encrypted** results. Public logs contain only counters and
hashes.

### Secret

Create one repository secret:

* `PAYLOAD_KEY` — the passphrase that unseals `payload.bin` and the results.

### Repository settings

* Settings → Actions → General → Workflow permissions: **Read repository
  contents** (the default `GITHUB_TOKEN` read-only is enough).
* Settings → Actions → General → Fork pull request workflows: **do not** allow
  workflows from forks to run, and do not expose secrets to them.

## Requirements

Python 3.12, and the libraries in `requirements.txt`
(`unicorn`, `capstone`, `cryptography`).
