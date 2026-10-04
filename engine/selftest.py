"""selftest.py -- exercise the engine on a tiny synthetic image, with no plug-in.

Two almost-identical Thumb images are built in memory: each stores one byte to a
peripheral word; the only difference is the byte. A trivial device records what
is written. The two images are run through the engine, their observations are
compared, and the difference is expected to show up. The addresses here are
arbitrary scratch values, not any real device's.

Run:  python -m engine.selftest
"""

from __future__ import annotations

from .core import Machine, Profile
from .diff import compare_observations, has_real
from .nativecount import selftest as counter_selftest

# Scratch memory map -- deliberately not a real device layout.
CODE = 0x00010000
RAM = 0x00020000
PERIPH = 0x50000000

PROFILE = Profile(
    regions=[(CODE, 0x10000), (RAM, 0x10000), (PERIPH, 0x1000)],
    flash_base=CODE,
    periph_ranges=[(PERIPH, PERIPH + 0xFFF)],
)


def build_image(value: int) -> bytes:
    """Vector table + a reset handler that stores `value` to the peripheral then
    spins. Returns the raw image written at the flash base."""
    sp = RAM + 0x8000
    entry = CODE + 8
    u16 = lambda h: int(h).to_bytes(2, "little")      # noqa: E731
    u32 = lambda w: int(w).to_bytes(4, "little")      # noqa: E731
    img = bytearray()
    img += u32(sp) + u32(entry | 1)                   # initial SP, reset vector
    # reset handler at CODE+8:
    #   ldr r1,[pc,#4] ; movs r0,#value ; str r0,[r1] ; b .
    img += u16(0x4901)                                # ldr r1,[pc,#4]
    img += u16(0x2000 | (value & 0xFF))              # movs r0,#value
    img += u16(0x6008)                                # str r0,[r1]
    img += u16(0xE7FE)                                # b . (self)
    img += u32(PERIPH)                                # literal: peripheral address
    return bytes(img)


class RecordingDevice:
    """A peripheral that just records the words written to it."""

    def __init__(self):
        self.writes = []

    def read(self, machine, addr, size):
        return 0

    def write(self, machine, addr, size, value):
        self.writes.append((addr, value))

    def tick(self, machine, t_ms):
        pass

    def snapshot(self):
        return list(self.writes)

    def restore(self, state):
        self.writes = list(state)


STOP = CODE + 8 + 6   # the 'b .' instruction


def run_image(value: int) -> dict:
    dev = RecordingDevice()
    m = Machine(PROFILE, build_image(value), label=f"v{value}", device=dev)
    m.run(STOP, max_ins=1000)
    return {"peripheral_writes": dev.writes}


def main() -> int:
    ok_counter = counter_selftest()
    obs_same_a = run_image(0xAA)
    obs_same_b = run_image(0xAA)
    obs_diff = run_image(0xAB)

    same = compare_observations(obs_same_a, obs_same_b)
    different = compare_observations(obs_same_a, obs_diff)

    checks = {
        "native_counter": ok_counter,
        "identical_images_match": not has_real(same),
        "one_byte_difference_detected": has_real(different),
        "write_was_observed": obs_same_a["peripheral_writes"] == [(PERIPH, 0xAA)],
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    all_ok = all(checks.values())
    print("SELFTEST", "OK" if all_ok else "FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
