"""Guards for app.py's stale-module reload shim.

Streamlit Cloud can serve a warm process whose ``core.*`` modules were imported
from an earlier deploy. app.py detects that by probing for names/params added in
recent pushes and force-reloads when one is missing.

The shim is only as good as its sentinels. Add a parameter to ``load_schedule``,
pass it from app.py, forget to update the sentinel, and the stale check still
passes — so the reload is skipped and the NEW app.py calls the OLD module,
dying with a bare ``TypeError: unexpected keyword argument`` on the first real
upload. That shipped once; these tests exist so it can't ship again.
"""
import inspect
import re
from pathlib import Path

import pytest

from core.data_loader import load_schedule

APP = Path(__file__).resolve().parent.parent / "app.py"
SOURCE = APP.read_text(encoding="utf-8")

# The `_stale = (...)` expression at the top of app.py.
STALE_BLOCK = re.search(r"_stale = \((.*?)\n\)", SOURCE, re.DOTALL)


def _sentinel_param() -> str:
    """The load_schedule parameter app.py probes to detect a stale module."""
    assert STALE_BLOCK, "couldn't find the `_stale = (...)` block in app.py"
    m = re.search(
        r'"(\w+)"\s+not in _inspect\.signature\(\s*'
        r'core\.data_loader\.load_schedule\s*\)\.parameters',
        STALE_BLOCK.group(1),
    )
    assert m, "app.py's stale check no longer probes load_schedule's signature"
    return m.group(1)


class TestSentinelTracksTheNewestParam:
    def test_sentinel_is_a_real_parameter(self):
        assert _sentinel_param() in inspect.signature(load_schedule).parameters

    def test_sentinel_is_the_most_recently_added_parameter(self):
        # New params are appended, so the last one is the newest — and the
        # newest is the only sentinel that detects every stale version (it
        # implies all the older ones). If this fails you added a parameter and
        # forgot to move the sentinel in app.py's `_stale` block.
        params = list(inspect.signature(load_schedule).parameters)
        assert _sentinel_param() == params[-1], (
            f"app.py probes for `{_sentinel_param()}` but load_schedule's newest "
            f"parameter is `{params[-1]}` — update the sentinel in the `_stale` "
            f"block or a warm Streamlit process will pair new app.py with the "
            f"old module."
        )


class TestAppOnlyPassesRealKeywords:
    def test_every_load_schedule_kwarg_exists(self):
        valid = set(inspect.signature(load_schedule).parameters)
        calls = re.findall(r"load_schedule\((.*?)\n\s*\)", SOURCE, re.DOTALL)
        assert calls, "no load_schedule call sites found in app.py"
        for call in calls:
            for kw in re.findall(r"(\w+)\s*=", call):
                assert kw in valid, (
                    f"app.py passes `{kw}=` to load_schedule, which has no such "
                    f"parameter — this is the TypeError the reload shim exists "
                    f"to prevent."
                )


class TestShimStillWired:
    def test_reload_runs_when_stale(self):
        assert "importlib.reload" in SOURCE or "_importlib.reload(" in SOURCE

    def test_data_loader_is_in_the_reload_list(self):
        # Reloading in dependency order matters; data_loader must be in it or a
        # stale loader survives the reload that was meant to replace it.
        m = re.search(r"for _modname in \((.*?)\):", SOURCE, re.DOTALL)
        assert m and '"core.data_loader"' in m.group(1)
