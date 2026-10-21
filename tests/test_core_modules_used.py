"""Every module under ``core/`` must earn its place: it has to be imported by
``app.py`` or by another core module.

``core/scheduler.py`` (a battery-cell scheduler from the first model) sat
unused for months -- nothing imported it, and the "LPT scheduler" the UI talks
about is ``_suggest_schedule_dates`` inside ``app.py``. These tests stop a dead
module creeping back, and keep the stale-module shim at the top of ``app.py``
(bare-import list + reload list; see MAINTAINING.md) consistent with the
modules that actually exist and are used.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"
CORE = ROOT / "core"

CORE_MODULES = sorted(p.stem for p in CORE.glob("*.py") if p.stem != "__init__")


def _core_modules_imported_by(path: Path) -> set:
    """Names of ``core`` submodules a file imports, in any of the spellings used
    in this repo: ``import core.X``, ``from core.X import ...``,
    ``from core import X``, and the relative ``from .X import ...``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "core" and len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:                       # from .X import ...  (inside core/)
                if mod:
                    found.add(mod.split(".")[0])
            elif mod == "core":                  # from core import X
                found.update(alias.name for alias in node.names)
            elif mod.startswith("core."):        # from core.X import ...
                found.add(mod.split(".")[1])
    return found


def _importers() -> dict:
    out = {"app.py": _core_modules_imported_by(APP)}
    for p in CORE.glob("*.py"):
        if p.stem != "__init__":
            out[f"core/{p.name}"] = _core_modules_imported_by(p) - {p.stem}
    return out


@pytest.mark.parametrize("module", CORE_MODULES)
def test_every_core_module_is_imported_by_app_or_another_core_module(module):
    importers = sorted(who for who, mods in _importers().items() if module in mods)
    assert importers, (
        f"core/{module}.py is imported by neither app.py nor any other core "
        f"module. If it is dead, delete it (and drop it from the bare-import and "
        f"reload lists at the top of app.py); if it is new, wire it in."
    )


# ---------------------------------------------------------------------------
# The stale-module shim at the top of app.py
# ---------------------------------------------------------------------------
SOURCE = APP.read_text(encoding="utf-8")


def _reload_list() -> list:
    m = re.search(r"for _modname in \((.*?)\):", SOURCE, re.DOTALL)
    assert m, "couldn't find the reload list (`for _modname in (...)`) in app.py"
    return re.findall(r'"core\.(\w+)"', m.group(1))


def _bare_import_list() -> list:
    return re.findall(r"^import core\.(\w+)\s*$", SOURCE, re.MULTILINE)


def test_reload_list_names_only_modules_that_exist():
    missing = [m for m in _reload_list() if m not in CORE_MODULES]
    assert not missing, (
        f"app.py's reload list names core modules that don't exist: {missing}. "
        f"Remove them from the reload list and the bare-import list."
    )


def test_bare_import_list_names_only_modules_that_exist():
    missing = [m for m in _bare_import_list() if m not in CORE_MODULES]
    assert not missing, f"app.py bare-imports core modules that don't exist: {missing}"


def test_every_module_app_pulls_names_from_is_in_both_shim_lists():
    """``from core.X import name`` only binds against a fresh module if X was
    bare-imported first and is in the reload list."""
    used = _core_modules_imported_by(APP)
    not_bare = sorted(used - set(_bare_import_list()))
    not_reloaded = sorted(used - set(_reload_list()))
    assert not not_bare, (
        f"app.py imports names from {not_bare} but never bare-imports them above "
        f"the `_stale` check -- add `import core.<name>` to the list."
    )
    assert not not_reloaded, (
        f"app.py imports names from {not_reloaded} but they are missing from the "
        f"reload list -- a stale warm process would keep the old module."
    )
