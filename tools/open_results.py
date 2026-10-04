"""open_results.py -- read a sealed result file locally.

    python tools/open_results.py <result.bin> --key <passphrase-file-or-value> [--field detail]

Results produced by the runner (and the artifacts the workflow uploads) are
sealed with the same passphrase, so the public side reveals only counters and a
hash. This prints the decrypted JSON (or one top-level field).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import box  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("result")
    ap.add_argument("--key", required=True, help="passphrase file or value")
    ap.add_argument("--field", default=None, help="print only this top-level field")
    op = ap.parse_args()
    passphrase = box.read_passphrase(op.key)
    try:
        data = json.loads(box.unseal_bytes(Path(op.result).read_bytes(), passphrase))
    except box.BadKey as e:
        raise SystemExit(f"could not open: {e}")
    print(json.dumps(data[op.field] if op.field else data, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
