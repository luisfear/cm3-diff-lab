"""lab chunk runner -- verify a sealed package and run one chunk of it.

Self-contained on purpose (only `cryptography` is needed to open the package): the code that runs on the public
runner is exactly this file plus the workflow, both pinned by git blob and checked before every launch.

Inputs come from the environment (never interpolated into shell): ASSET, ASSET_SHA256, TREE, CHUNK, TIMEOUT_MIN,
PAYLOAD_KEY. Steps, each one fail-closed:
  1. the asset's sha256 must equal ASSET_SHA256 (before anything else is touched);
  2. unseal (AES-256-GCM, scrypt key) and unpack safely;
  3. every file of the package is checked against its manifest, and the manifest tree hash must equal TREE;
  4. the package data must be the anonymised kind;
  5. install the exact engine versions written in the manifest;
  6. run the chunk with a time limit;
  7. ALWAYS seal the chunk output (also on failure, with the reason) into out/t-<k>.bin.
Public log: fixed words, hashes and counters only. Exit code 0 only if the chunk is complete and verified.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

MAGIC, VERSION, SALT, NONCE = b"LABX", 1, 16, 12
N, R, P = 2 ** 15, 8, 1


def _key() -> bytes:
    k = os.environ.get("PAYLOAD_KEY", "").strip()
    if not k:
        raise SystemExit("no key")
    return k.encode()


def _derive(passphrase: bytes, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=N, r=R, p=P).derive(passphrase)


def seal(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(SALT), os.urandom(NONCE)
    return MAGIC + bytes([VERSION]) + salt + nonce + AESGCM(_derive(_key(), salt)).encrypt(nonce, data, None)


def unseal(blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:4] != MAGIC or blob[4] != VERSION:
        raise ValueError("not a sealed blob")
    salt, nonce, ct = blob[5:5 + SALT], blob[5 + SALT:5 + SALT + NONCE], blob[5 + SALT + NONCE:]
    return AESGCM(_derive(_key(), salt)).decrypt(nonce, ct, None)


def untar(data: bytes, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    base = dst.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for m in tar.getmembers():
            target = (dst / m.name).resolve()
            if not m.isfile() or not str(target).startswith(str(base) + os.sep):
                raise ValueError("unsafe member")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as fh:
                target.write_bytes(fh.read())


def tar_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(x for x in src.rglob("*") if x.is_file()):
            info = tarfile.TarInfo(p.relative_to(src).as_posix())
            data = p.read_bytes()
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    k = int(os.environ["CHUNK"])
    out = Path("out")
    out.mkdir(exist_ok=True)
    work = Path("work")
    salida = work / "salida"
    salida.mkdir(parents=True, exist_ok=True)
    log = []
    estado = "FAILED"
    try:
        blob = Path("in", os.environ["ASSET"]).read_bytes()
        if sha256(blob) != os.environ["ASSET_SHA256"].strip().lower():
            raise RuntimeError("asset sha256 mismatch")
        pkg = work / "paquete"
        untar(unseal(blob), pkg)
        man = json.loads((pkg / "MANIFIESTO.json").read_text(encoding="utf-8"))
        r = subprocess.run([sys.executable, str(pkg / "trabajador" / "verificar_paquete.py"), str(pkg),
                            "--hash-arbol", os.environ["TREE"]], capture_output=True, text=True)
        log.append(r.stdout[-4000:] + r.stderr[-2000:])
        if r.returncode != 0:
            raise RuntimeError("package verification failed")
        if man["datos"]["sensibilidad"] != "anonimizado":
            raise RuntimeError("package data is not anonymised")
        motor = man["motor"]
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", f"unicorn=={motor['unicorn']}",
                            f"capstone=={motor['capstone']}"], capture_output=True, text=True)
        log.append(r.stdout[-2000:] + r.stderr[-2000:])
        if r.returncode != 0:
            raise RuntimeError("engine install failed")
        nombres = sorted(man["trabajos"])
        if not 1 <= k <= len(nombres):
            raise RuntimeError("chunk index out of range")
        nombre = nombres[k - 1]
        limite = int(os.environ.get("TIMEOUT_MIN", "280")) * 60 - 300
        t0 = time.time()
        r = subprocess.run([sys.executable, "-u", str(pkg / "trabajador" / "ejecutar_trozo.py"),
                            str(pkg / "trabajos" / f"{nombre}.json"), "--paquete", str(pkg),
                            "--salida", str(salida / nombre)], capture_output=True, text=True,
                           timeout=max(600, limite))
        log.append(r.stdout[-6000:] + r.stderr[-4000:])
        res = json.loads((salida / nombre / "resultado_trozo.json").read_text(encoding="utf-8"))
        estado = res.get("estado", "FAILED")
        counts = res.get("recuento", {})
        print(f"chunk {k}: {estado} in {time.time() - t0:.0f}s instrument={man['huella'][:12]} "
              f"engine={motor['unicorn']}/{motor['capstone']} counts={json.dumps(counts)}")
    except Exception as e:                                    # noqa: BLE001 - every failure is sealed and reported
        print(f"chunk {k}: FAILED ({type(e).__name__})")
        (salida / "_runner").mkdir(parents=True, exist_ok=True)
        (salida / "_runner" / "error.json").write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}),
                                                       encoding="utf-8")
    (salida / "_runner").mkdir(parents=True, exist_ok=True)
    (salida / "_runner" / "log.txt").write_text("\n".join(log), encoding="utf-8")
    sealed = seal(tar_dir(salida))
    (out / f"t-{k}.bin").write_bytes(sealed)
    print(f"chunk {k}: output sealed sha256={sha256(sealed)[:16]}")
    return 0 if estado == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
