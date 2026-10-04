#!/usr/bin/env bash
# ensayo_local.sh -- rehearse the sealed archive end to end on a trusted machine.
#
# It unseals payload.bin into a temporary directory, runs the reduced differential
# pass and the phase confirmation with the anonymised data, and compares the
# result fingerprint and the set of confirmed real differences against a stored
# reference. On the very first run (or with --record) it writes that reference
# instead of comparing.
#
# Usage:
#   tools/ensayo_local.sh <passphrase-file> [--record] [--procs N]
#
# The passphrase file is the one that seals payload.bin and the results; keep it
# outside the repository. Nothing here contacts the network.
set -euo pipefail

KEY=${1:?usage: ensayo_local.sh <passphrase-file> [--record] [--procs N]}
shift || true
RECORD=0
PROCS=2
while [ $# -gt 0 ]; do
  case "$1" in
    --record) RECORD=1 ;;
    --procs) shift; PROCS=$1 ;;
    --set) shift; export TEST_SET=$1 ;;   # test set number, mapped inside the sealed plug-in (0 = default)
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"                     # so `python -m engine...` and the inline imports resolve
PY=${PY:-python3}
WORK=$(mktemp -d)
mkdir -p "$ROOT/out"
# keep the sealed outputs for inspection (out/ is ignored by git), then drop the unsealed plug-in
trap 'cp -f "$WORK"/*.bin "$WORK"/verdict.json "$ROOT/out/" 2>/dev/null; rm -rf "$WORK"' EXIT

echo "unsealing archive..."
"$PY" "$ROOT/tools/unseal.py" "$ROOT/payload.bin" "$WORK/plugin" --key "$KEY"

echo "running the reduced pass..."
"$PY" -m engine.harness run --plugin "$WORK/plugin" --key "$KEY" \
  --out "$WORK/result.bin" --fast --procs "$PROCS"

echo "confirming across phases..."
"$PY" -m engine.phase --plugin "$WORK/plugin" --key "$KEY" \
  --result "$WORK/result.bin" --out "$WORK/phase.bin" --fast --procs "$PROCS"

# Build a compact verdict (fingerprint + confirmed real set) from the sealed files.
"$PY" - "$KEY" "$WORK/result.bin" "$WORK/phase.bin" "$WORK/verdict.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from engine import box
key, result, phase, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
k = box.read_passphrase(key)
r = json.loads(box.unseal_bytes(open(result, "rb").read(), k))
p = json.loads(box.unseal_bytes(open(phase, "rb").read(), k))
verdict = {"fingerprint": r["fingerprint"], "count": r["count"],
           "confirmed": sorted(p["confirmed"])}
open(out, "w").write(json.dumps(verdict, sort_keys=True))
print(json.dumps({"count": verdict["count"], "fingerprint": verdict["fingerprint"],
                  "confirmed": len(verdict["confirmed"])}))
PY

REF="$ROOT/reference_verdict.json"
if [ "$RECORD" = 1 ] || [ ! -f "$REF" ]; then
  cp "$WORK/verdict.json" "$REF"
  echo "reference recorded -> $REF"
else
  if diff -q "$REF" "$WORK/verdict.json" >/dev/null; then
    echo "PASS: verdict matches the reference"
  else
    echo "FAIL: verdict differs from the reference"
    exit 1
  fi
fi
