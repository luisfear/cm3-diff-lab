"""Generic differential testing harness for Cortex-M3 firmware images.

The public, cleartext part of this project: a reusable Cortex-M3 emulation
engine (CPU, memory, interrupts/SysTick, hooks, snapshots), a differential
comparison of two firmware images under the same inputs, deterministic work
splitting and joining, phase rounds, and a parallel test runner.

Device-specific peripheral and chip models are NOT part of this project. They
are supplied at run time by a plug-in loaded from an encrypted archive (see
engine.plugin and tools/unseal.py). Without a plug-in the engine still runs:
engine.selftest exercises it on a tiny synthetic image.
"""

from .core import Machine, Profile
from .diff import compare_observations
from .plugin import Plugin, load_plugin

__all__ = ["Machine", "Profile", "compare_observations", "Plugin", "load_plugin"]
