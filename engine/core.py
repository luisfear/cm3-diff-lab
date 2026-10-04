"""core.py -- a generic Cortex-M3 machine on top of Unicorn.

This module knows nothing about any particular board. Every address it uses
comes from a `Profile` (memory regions, the flash base it resets from, the
optional bit-band aliases and the optional system-control registers). The
behaviour of peripheral reads and writes is delegated to a `device` object
supplied by the caller; for the synthetic self-test a trivial device is used,
and for real work the device comes from an encrypted plug-in.

What lives here (the reusable part):
  * mapping the memory regions and loading an image at the flash base;
  * reset from the initial stack pointer and reset vector;
  * running with an instruction budget, turning CPU faults into the firmware's
    own fault handler (as the silicon would), and detecting low-power wait;
  * injecting the periodic tick interrupt and device interrupts, honouring the
    interrupt-enable mask and the critical-section flag;
  * a native per-block instruction counter (see nativecount.py);
  * snapshots (CPU + memory + device state) so every test starts from the same
    point;
  * optional bit-band alias handling.

The device protocol (duck-typed) is:
  read(machine, addr, size) -> int | None   value to return, or None for RAM
  write(machine, addr, size, value) -> None  record/act on a write
  tick(machine, t_ms) -> None                 advance peripherals one tick
  snapshot() -> object / restore(state)       opaque device state
Any method may be absent; sensible defaults apply.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import unicorn as _unicorn
from unicorn import (UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
                     UC_MODE_MCLASS, UC_MODE_THUMB, Uc, UcError)
from unicorn.arm_const import (UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_PRIMASK,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP)

from . import nativecount

if int(str(getattr(_unicorn, "__version__", "2")).split(".")[0]) < 2:
    raise ImportError(f"this engine needs unicorn 2.x (found {_unicorn.__version__}); pip install -r requirements.txt")

# A free return address: code that returns here lands outside any mapped region,
# which stops the run cleanly. Chosen to sit just below the first flash byte.
_RETURN_MARKER = 0x00000001

# Context registers saved and restored around an injected interrupt.
_CONTEXT = None  # built lazily to keep the import list short


def _context_regs():
    global _CONTEXT
    if _CONTEXT is None:
        from unicorn.arm_const import (UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6,
                                       UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10,
                                       UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_XPSR)
        _CONTEXT = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
                    UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
                    UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR,
                    UC_ARM_REG_PC, UC_ARM_REG_XPSR)
    return _CONTEXT


@dataclass
class Profile:
    """Everything address-specific the engine needs, kept out of the code.

    regions:      list of (base, size) to map.
    flash_base:   where the image is written and where reset reads SP/PC.
    periph_ranges: list of (lo, hi) inclusive ranges the device handles.
    instr_per_ms: core instructions per millisecond (for the tick loop).
    bitband:      optional {"periph": (alias, base), "sram": (alias, base)}.
    system:       optional dict with keys systick_csr, systick_rvr, nvic_iser,
                  nvic_icer, scb_aircr, scb_vtor (absolute addresses).
    vtor_base:    vector-table base for reset and exceptions (default flash_base).
    """

    regions: list = field(default_factory=list)
    flash_base: int = 0
    periph_ranges: list = field(default_factory=list)
    instr_per_ms: int = 48000
    bitband: dict = field(default_factory=dict)
    system: dict = field(default_factory=dict)
    vtor_base: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {f: d[f] for f in ("regions", "flash_base", "periph_ranges", "instr_per_ms",
                                   "bitband", "system", "vtor_base") if f in d}
        return cls(**known)

    def vtor(self) -> int:
        return self.vtor_base if self.vtor_base is not None else self.flash_base


class Machine:
    def __init__(self, profile: Profile, image: bytes, label: str = "", device=None):
        self.profile = profile
        self.label = label
        self.device = device if device is not None else _NullDevice()
        self.t_ms = 0
        self.instr = 0
        self.restarted = False
        self.restart_cause = None
        self.low_power = False
        self.stalled = False            # fault handler looping: the core no longer advances
        self.faults: list[str] = []
        self.irq_queue: list[int] = []

        mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.mu = mu
        for base, size in profile.regions:
            mu.mem_map(base, size)
        mu.mem_write(profile.flash_base, image)

        for lo, hi in profile.periph_ranges:
            mu.hook_add(UC_HOOK_MEM_READ, self._on_read, begin=lo, end=hi)
            mu.hook_add(UC_HOOK_MEM_WRITE, self._on_write, begin=lo, end=hi)

        bb = profile.bitband
        if bb:
            for which, (alias, real) in bb.items():
                size = 0x02000000  # the Cortex-M bit-band alias spans 32x its region
                mu.mem_map(alias, size)
                mu.hook_add(UC_HOOK_MEM_READ, self._bb_read, begin=alias, end=alias + size - 1)
                mu.hook_add(UC_HOOK_MEM_WRITE, self._bb_write, begin=alias, end=alias + size - 1)
                self._bb_map = getattr(self, "_bb_map", {})
                self._bb_map[(alias, alias + size - 1)] = real

        self.counter = nativecount.install(mu)

    # ------------------------------------------------------------------ peripherals
    def _on_read(self, mu, access, addr, size, value, user):
        v = self.device.read(self, addr, size)
        if v is not None:
            mu.mem_write(addr & ~3, (v & 0xFFFFFFFF).to_bytes(4, "little"))

    def _on_write(self, mu, access, addr, size, value, user):
        value &= (1 << (8 * size)) - 1
        self.device.write(self, addr, size, value)

    def _bb_region(self, addr):
        for (lo, hi), real in getattr(self, "_bb_map", {}).items():
            if lo <= addr <= hi:
                off = addr - lo
                return real + ((off >> 5) & ~3), (off >> 2) & 31
        return None, None

    def _bb_read(self, mu, access, addr, size, value, user):
        real, bit = self._bb_region(addr)
        if real is None:
            return
        self._on_read(mu, access, real, 4, 0, user)
        word = int.from_bytes(bytes(mu.mem_read(real, 4)), "little")
        mu.mem_write(addr & ~3, ((word >> bit) & 1).to_bytes(4, "little"))

    def _bb_write(self, mu, access, addr, size, value, user):
        real, bit = self._bb_region(addr)
        if real is None:
            return
        word = int.from_bytes(bytes(mu.mem_read(real, 4)), "little")
        word = (word | (1 << bit)) if (value & 1) else (word & ~(1 << bit))
        mu.mem_write(real, (word & 0xFFFFFFFF).to_bytes(4, "little"))
        self._on_write(mu, access, real, 4, word, user)

    # ------------------------------------------------------------------ word helpers
    def rd(self, addr: int, n: int = 4) -> int:
        return int.from_bytes(bytes(self.mu.mem_read(addr, n)), "little")

    def wr(self, addr: int, value: int, n: int = 4) -> None:
        self.mu.mem_write(addr, int(value).to_bytes(n, "little"))

    def vector(self, index: int) -> int:
        return self.rd(self.profile.vtor() + 4 * index)

    # ------------------------------------------------------------------ reset / run
    def reset(self):
        head = bytes(self.mu.mem_read(self.profile.flash_base, 8))
        sp = int.from_bytes(head[0:4], "little")
        pc = int.from_bytes(head[4:8], "little")
        self.mu.reg_write(UC_ARM_REG_SP, sp)
        self._pc = pc
        return pc

    def _run_block(self, start: int, stop: int, count: int) -> str | None:
        """emu_start with the core's behaviour on exceptions: a wait instruction
        enters low-power; any other fault runs the firmware's own fault handler."""
        try:
            self.mu.emu_start(start, stop, count=count)
            return None
        except UcError as e:
            pc = self.mu.reg_read(UC_ARM_REG_PC)
            try:
                op = int.from_bytes(bytes(self.mu.mem_read(pc & ~1, 2)), "little")
            except UcError:
                op = 0
            if op in (0xBF20, 0xBF30):          # wfe / wfi
                self.low_power = True
                return "low_power"
            self.faults.append(f"0x{pc:08X}: {e}")
            handler = self.vector(3)            # HardFault
            self.mu.reg_write(UC_ARM_REG_LR, self.profile.flash_base | 1)
            try:
                self.mu.emu_start(handler | 1, self.profile.flash_base, count=2_000_000)
            except UcError:
                pass
            if not self.restarted:
                self.stalled = True
                return "stalled"
            self.restart_cause = self.restart_cause or "hardfault"
            return "restart"

    def run(self, stop: int, max_ins: int = 1_000_000) -> str | None:
        """Reset (once) and run to `stop` in a single pass, without the tick loop.
        Useful for short, time-independent runs and for the self-test."""
        pc = getattr(self, "_pc", None) or self.reset()
        r = self._run_block(pc | 1, stop, max_ins)
        self._pc = self.mu.reg_read(UC_ARM_REG_PC)
        return r

    def run_to(self, stop: int, max_ins: int = 60_000_000) -> int:
        pc = getattr(self, "_pc", None) or self.reset()
        done = 0
        step = max(1, self.profile.instr_per_ms * 10)
        while done < max_ins:
            take = min(step, max_ins - done)
            r = self._run_block(pc | 1, stop, take)
            done += take
            self.instr += take
            self.device.tick(self, self.t_ms)
            self.t_ms += step // max(1, self.profile.instr_per_ms)
            pc = self.mu.reg_read(UC_ARM_REG_PC)
            if r in ("low_power", "restart") or self.restarted or (stop and pc == stop):
                break
        self._pc = pc
        return pc

    # ------------------------------------------------------------------ interrupts
    def _enabled(self, irq: int) -> bool:
        iser = self.profile.system.get("nvic_iser")
        if iser is None:
            return True
        word = self.rd(iser + 4 * (irq // 32))
        return bool((word >> (irq % 32)) & 1)

    def _leave_critical(self):
        for _ in range(500):
            if not (self.mu.reg_read(UC_ARM_REG_PRIMASK) & 1) or self.stalled:
                return
            pc = self.mu.reg_read(UC_ARM_REG_PC)
            try:
                self.mu.emu_start(pc | 1, 0, count=100)
            except UcError:
                return

    def irq(self, irq: int, force: bool = False) -> bool:
        if self.stalled or (not force and not self._enabled(irq)):
            return False
        self._leave_critical()
        handler = self.vector(16 + irq)
        ctx = self.mu.context_save()
        self.mu.reg_write(UC_ARM_REG_LR, self.profile.flash_base | 1)
        r = self._run_block(handler | 1, self.profile.flash_base, 25_000_000)
        if not self.restarted and not self.low_power and not self.stalled:
            self.mu.context_restore(ctx)
        return True

    def systick(self) -> bool:
        """Deliver the periodic tick interrupt if the firmware has enabled it
        (enable + tick-interrupt bits of the control/status register)."""
        csr = self.profile.system.get("systick_csr")
        if csr is None or (self.rd(csr) & 3) != 3:
            return False
        self._leave_critical()
        ctx = self.mu.context_save()
        self.mu.reg_write(UC_ARM_REG_LR, self.profile.flash_base | 1)
        handler = self.vector(15)
        r = self._run_block(handler | 1, self.profile.flash_base, 2_000_000)
        if not self.restarted and not self.low_power and not self.stalled:
            self.mu.context_restore(ctx)
        return True

    def run_ms(self, ms: int, stop_when=None) -> str:
        """Run `ms` of simulated time: one tick each reload period, advancing the
        peripherals and delivering SysTick, as the silicon would."""
        rvr = self.profile.system.get("systick_rvr")
        reload = (self.rd(rvr) & 0xFFFFFF) if rvr is not None else (self.profile.instr_per_ms * 10 - 1)
        instr_per_tick = max(1000, reload + 1)
        ms_per_tick = max(1, round((reload + 1) / max(1, self.profile.instr_per_ms)))
        for _ in range(max(1, ms // ms_per_tick)):
            if self.low_power:
                return "low_power"
            if self.stalled:
                self.t_ms += ms_per_tick
                continue
            pc = self.mu.reg_read(UC_ARM_REG_PC)
            r = self._run_block(pc | 1, 0, instr_per_tick)
            self.instr += instr_per_tick
            if r == "low_power":
                return "low_power"
            if self.restarted:
                return "restart"
            while self.irq_queue and not self.stalled:
                self.irq(self.irq_queue.pop(0))
            self.systick()
            if self.restarted:
                return "restart"
            self.t_ms += ms_per_tick
            self.device.tick(self, self.t_ms)
            if self.restarted:
                return "restart"
            if stop_when and stop_when(self):
                return "stalled" if self.stalled else "ok"
        return "stalled" if self.stalled else "ok"

    def call(self, func: int, *args, max_ins: int = 2_000_000) -> int:
        """Call a Thumb function with up to three word arguments; return r0."""
        for reg, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2), args):
            self.mu.reg_write(reg, v & 0xFFFFFFFF)
        self.mu.reg_write(UC_ARM_REG_LR, self.profile.flash_base | 1)
        ctx = self.mu.context_save()
        try:
            self.mu.emu_start(func | 1, self.profile.flash_base, count=max_ins)
        except UcError:
            self.faults.append("call failed")
        r0 = self.mu.reg_read(UC_ARM_REG_R0)
        self.mu.context_restore(ctx)
        return r0

    def post_irq(self, irq: int):
        self.irq_queue.append(irq)

    # ------------------------------------------------------------------ snapshots
    def snapshot(self) -> dict:
        mem = {base: bytes(self.mu.mem_read(base, size)) for base, size in self.profile.regions}
        dev = self.device.snapshot() if hasattr(self.device, "snapshot") else None
        return {"ctx": self.mu.context_save(), "mem": mem, "t_ms": self.t_ms, "instr": self.instr,
                "device": copy.deepcopy(dev),
                "flags": (self.restarted, self.restart_cause, self.low_power, self.stalled)}

    def restore(self, snap: dict):
        self.mu.context_restore(snap["ctx"])
        for base, data in snap["mem"].items():
            self.mu.mem_write(base, data)
        self.t_ms, self.instr = snap["t_ms"], snap["instr"]
        self.restarted, self.restart_cause, self.low_power, self.stalled = snap["flags"]
        self.faults = []
        self.irq_queue = []
        if hasattr(self.device, "restore"):
            self.device.restore(copy.deepcopy(snap["device"]))


class _NullDevice:
    """A device that maps every peripheral word to zero and never interrupts.
    Enough for the self-test and for images that touch no peripherals."""

    def read(self, machine, addr, size):
        return 0

    def write(self, machine, addr, size, value):
        pass

    def tick(self, machine, t_ms):
        pass

    def snapshot(self):
        return None

    def restore(self, state):
        pass
