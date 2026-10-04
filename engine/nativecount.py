"""nativecount.py -- an exact instruction clock for Unicorn, with no compiler.

Unicorn does not report how many instructions it has executed partway through a
run, and a Python hook added while emulation is in flight is not called for the
basic blocks already translated. So a deadline measured "in instructions" from
inside a memory hook would never advance.

The fix is a permanent per-block hook whose body is a few native instructions
that add each block's size to a 64-bit counter, without ever entering Python.
The counter holds bytes of executed Thumb code; bytes / 2 approximates the
instruction count (32-bit Thumb-2 encodings count double).

    void cb(uc_engine *uc, uint64_t address, uint32_t size, void *user)
    { *(uint64_t *)user += size; }

If the host architecture is not one of the few encoded below, install() returns
None and the caller falls back to a coarser, block-boundary estimate.
"""

from __future__ import annotations

import ctypes
import mmap
import platform
import sys

_page = None        # the executable page, kept alive for the life of the process
_addr = None


def _code() -> bytes | None:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        # ldr x8,[x3] ; add x8,x8,w2,uxtw ; str x8,[x3] ; ret
        return bytes.fromhex("680040f9" "0841228b" "680000f9" "c0035fd6")
    if machine in ("x86_64", "amd64"):
        if sys.platform == "win32":
            return bytes([0x44, 0x89, 0xC0, 0x49, 0x01, 0x01, 0xC3])   # mov eax,r8d; add [r9],rax; ret
        return bytes([0x89, 0xD0, 0x48, 0x01, 0x01, 0xC3])             # mov eax,edx; add [rcx],rax; ret
    return None


def _clear_cache(addr: int, n: int):
    for lib in ("libgcc_s.so.1", None):
        try:
            fn = ctypes.CDLL(lib).__clear_cache
            fn.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
            fn(addr, addr + n)
            return
        except (OSError, AttributeError):
            continue


def _function() -> int | None:
    global _page, _addr
    if _addr is not None:
        return _addr
    code = _code()
    if code is None:
        return None
    try:
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            k32.VirtualAlloc.restype = ctypes.c_void_p
            k32.VirtualAlloc.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong)
            addr = k32.VirtualAlloc(None, 4096, 0x3000, 0x40)   # MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE
            if not addr:
                return None
            ctypes.memmove(addr, code, len(code))
            _page = addr
        else:
            page = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
            page.write(code)
            addr = ctypes.addressof(ctypes.c_char.from_buffer(page))
            _page = page
            if platform.machine().lower() in ("aarch64", "arm64"):
                _clear_cache(addr, len(code))
    except (OSError, ValueError, AttributeError):
        return None
    _addr = addr
    return addr


def _uclib():
    """The loaded Unicorn C library, across 2.0.x and 2.1.x layouts."""
    try:
        from unicorn.unicorn_py3.unicorn import uclib
        return uclib
    except Exception:
        pass
    try:
        from unicorn.unicorn import _uc
        return _uc
    except Exception:
        return None


def install(mu) -> "ctypes.c_uint64 | None":
    """Attach the permanent block hook and return the counter (bytes of code),
    or None if the host architecture or Unicorn build is unsupported."""
    from unicorn import UC_HOOK_BLOCK
    uclib = _uclib()
    fn = _function()
    if fn is None or uclib is None:
        return None
    counter = ctypes.c_uint64(0)
    handle = ctypes.c_size_t(0)
    try:
        uclib.uc_hook_add.restype = ctypes.c_int
        # Pass explicit ctypes objects so pointers and 64-bit ranges are not
        # truncated when the prototype is not pre-declared (older builds).
        err = uclib.uc_hook_add(mu._uch, ctypes.byref(handle), ctypes.c_int(UC_HOOK_BLOCK),
                                ctypes.c_void_p(fn), ctypes.c_void_p(ctypes.addressof(counter)),
                                ctypes.c_uint64(1), ctypes.c_uint64(0))
    except Exception:
        return None
    if err != 0:
        return None
    mu._native_counter = (counter, handle)   # keep both from the garbage collector
    return counter


def selftest() -> bool:
    """Run a 3-instruction Thumb loop 1000 times and check the counter moved."""
    from unicorn import UC_ARCH_ARM, UC_MODE_THUMB, Uc
    from unicorn.arm_const import UC_ARM_REG_R0
    mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    mu.mem_map(0x1000, 0x1000)
    # movw r0,#1000 ; 1: subs r0,#1 ; bne 1b ; nop
    mu.mem_write(0x1000, bytes.fromhex("40f2e830" "0138" "fdd1" "00bf"))
    c = install(mu)
    if c is None:
        return False
    mu.emu_start(0x1001, 0x100A)
    return mu.reg_read(UC_ARM_REG_R0) == 0 and c.value >= 1000 * 4


if __name__ == "__main__":
    print(platform.machine(), sys.platform, "selftest:", selftest())
