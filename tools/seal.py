"""seal.py -- bundle and encrypt a directory into a single archive.

    python tools/seal.py <source-dir> <output.bin> --key <passphrase-file-or-value>

The source directory is bundled into a reproducible, metadata-stripped tar and
sealed with AES-256-GCM (see engine/box.py). The passphrase is read from a file
(one line) or taken literally. Keep the passphrase outside this repository.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import box  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("output")
    ap.add_argument("--key", required=True, help="passphrase file or value")
    op = ap.parse_args()
    src = Path(op.source)
    if not src.is_dir():
        raise SystemExit(f"not a directory: {src}")
    passphrase = box.read_passphrase(op.key)
    blob = box.seal_dir(src, passphrase)
    Path(op.output).write_bytes(blob)
    n = sum(1 for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    print(f"sealed {n} files -> {op.output} ({len(blob)} bytes)")


if __name__ == "__main__":
    main()
