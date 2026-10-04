"""unseal.py -- decrypt and unbundle an archive into a directory.

    python tools/unseal.py <archive.bin> <dest-dir> --key <passphrase-file-or-value>

A wrong passphrase, or any tampering, fails loudly before any file is written.
The workflow uses this to unbundle the plug-in into a temporary directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import box  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("dest")
    ap.add_argument("--key", required=True, help="passphrase file or value")
    op = ap.parse_args()
    passphrase = box.read_passphrase(op.key)
    blob = Path(op.archive).read_bytes()
    try:
        box.unseal_dir(blob, Path(op.dest), passphrase)
    except box.BadKey as e:
        raise SystemExit(f"could not unseal: {e}")
    n = sum(1 for p in Path(op.dest).rglob("*") if p.is_file())
    print(f"unsealed {n} files -> {op.dest}")


if __name__ == "__main__":
    main()
