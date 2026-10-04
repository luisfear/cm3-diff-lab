"""run_wrapped.py -- run a command so failures never leak to a public log.

    python tools/run_wrapped.py --key <pass> --detail <sealed.bin> -- <command> [args...]

The command runs with its stdout/stderr captured. On success nothing of the
output is printed except a short status line. On failure the captured output
(which may contain paths, symbols or tracebacks) is sealed to the --detail file;
stdout gets only an exit code and a hash of the captured text. This keeps
Continuous-Integration logs free of anything identifying.

Optional --progress <file>: while the command runs, lines of the form
"[name]  NN% (k/n) ..." are reduced to "(k/n)" and appended to that file, so a
side process can report numeric progress. Nothing else is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import box  # noqa: E402

_PROG = re.compile(r"\((\d+)/(\d+)\)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--key", required=True)
    ap.add_argument("--detail", required=True, help="sealed file for captured output")
    ap.add_argument("--tail", type=int, default=0,
                    help="print this many trailing lines on success (should be identifier-free)")
    ap.add_argument("--progress", default=None,
                    help="file that receives numeric '(k/n)' markers while running")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    op = ap.parse_args()
    cmd = op.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise SystemExit("no command given")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    err_chunks: list[str] = []
    t = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    t.start()
    out_lines: list[str] = []
    pf = open(op.progress, "a", buffering=1) if op.progress else None
    for line in proc.stdout:
        out_lines.append(line)
        if pf:
            m = _PROG.search(line)
            if m:
                pf.write(f"({m.group(1)}/{m.group(2)})\n")
    proc.wait()
    t.join()
    if pf:
        pf.close()
    stdout = "".join(out_lines)
    stderr = "".join(err_chunks)
    captured = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
    passphrase = box.read_passphrase(op.key)
    Path(op.detail).write_bytes(box.seal_bytes(captured.encode("utf-8", "replace"), passphrase))
    digest = hashlib.sha256(captured.encode("utf-8", "replace")).hexdigest()[:16]

    if proc.returncode == 0:
        if op.tail > 0:
            for line in stdout.splitlines()[-op.tail:]:
                print(line)
        print(f"OK code=0 output-hash={digest}")
    else:
        # Public log: code and hash only. Detail is sealed in --detail.
        print(f"FAILED code={proc.returncode} output-hash={digest} (detail sealed)")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
