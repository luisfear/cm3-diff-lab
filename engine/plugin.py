"""plugin.py -- how the engine loads device behaviour at run time.

The engine ships no peripheral or chip models. A plug-in supplies them, loaded
from a directory that was unsealed from the encrypted archive. The directory
must contain a `plugin.py` that defines a module-level `PLUGIN` implementing the
interface below. The engine never imports anything device-specific directly; it
only calls these methods.

Interface (all methods but build_tests and run_pair are optional):

    build_tests(fast: bool) -> list[dict]
        The tests. Each is a dict with at least "id" (str), "group" (str) and
        "cost" (int, relative run cost). Any other keys describe the stimulus
        and are the plug-in's own business.

    setup() -> None
        Called once per worker process before the first run_pair. The plug-in
        builds and caches its machines here (engine.Machine with its device).

    run_pair(test: dict) -> (observation_a: dict, observation_b: dict)
        Run the test on both images and return their observations (see
        engine.diff). The plug-in owns snapshots and machine reuse.

    classifiers() -> dict
        Field-name -> (value_a, value_b) -> class_name|None, for engine.diff.

    group_of(test: dict) -> hashable
        Grouping used to balance chunk splitting (default: test["group"]).

    phase_variants(test: dict) -> list[dict]
        Variants of a test at different scheduling phases, for phase rounds.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


class Plugin:
    """Reference base class. A plug-in may subclass this or just duck-type it."""

    def build_tests(self, fast: bool) -> list[dict]:
        raise NotImplementedError

    def setup(self) -> None:
        pass

    def run_pair(self, test: dict):
        raise NotImplementedError

    def classifiers(self) -> dict:
        return {}

    def group_of(self, test: dict):
        return test.get("group", "")

    def phase_variants(self, test: dict) -> list[dict]:
        return []


def load_plugin(directory) -> Plugin:
    """Import `<directory>/plugin.py` and return its PLUGIN object. The directory
    is put on sys.path so the plug-in can import its own sibling modules."""
    directory = Path(directory).resolve()
    entry = directory / "plugin.py"
    if not entry.exists():
        raise FileNotFoundError(f"no plugin.py in {directory}")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("device_plugin", entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules["device_plugin"] = module
    spec.loader.exec_module(module)
    plugin = getattr(module, "PLUGIN", None)
    if plugin is None:
        raise AttributeError(f"{entry} does not define PLUGIN")
    return plugin
