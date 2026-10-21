"""Guards for the ``@st.cache_data`` loaders at the top of app.py.

``st.cache_data`` builds its cache key from the arguments ACTUALLY PASSED to the
call, skipping any parameter whose name starts with ``_``; a parameter's default
value is never part of the key. The data loaders used to be declared as
``def _load_x(_mtime, _schema_ver=_LOADER_SCHEMA_VERSION)``, which meant they
were cached on nothing: a changed file on disk or a bumped
``_LOADER_SCHEMA_VERSION`` never invalidated a warm process, and only
``st.cache_data.clear()`` (after an in-app save) or a Cloud reboot refreshed the
data. HANDOFF.md used to say the bump "may require a Cloud reboot" -- that was
this bug.

Two layers of protection here:

* a static scan of app.py so no ``@st.cache_data`` function can regress to
  underscore-only parameters (or sneak the schema version back in as a
  default, where it is equally invisible to the cache);
* a behavioural check that the loaders really re-run for a new ``mtime`` and
  for a bumped ``_LOADER_SCHEMA_VERSION`` -- and really don't for a repeat.
"""
import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app.py"

# (public name, cached inner name, core loader it calls) -- the CSV loaders,
# keyed on mtime + _LOADER_SCHEMA_VERSION.
CSV_LOADERS = [
    ("_load_machine_df", "_load_machine_df_cached", "load_machine_labor"),
    ("_load_acc_df", "_load_acc_df_cached", "load_acc_labor"),
    ("_load_item_master_df", "_load_item_master_df_cached", "load_item_master"),
    ("_load_item_packages_df", "_load_item_packages_df_cached", "load_item_packages"),
]
# (cached name, core loader it calls) -- JSON/JSONL loaders, keyed on mtime.
JSON_LOADERS = [
    ("_load_scenarios_dict", "load_all_scenarios"),
    ("_load_uploaded_schedules_dict", "load_all_uploaded_schedules"),
    ("_load_process_flow_edges", "load_process_flow"),
    ("_load_usage_log_cached", "load_usage_log"),
]


# ---------------------------------------------------------------------------
# Static scan
# ---------------------------------------------------------------------------
def _is_st_cache_data(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "cache_data"
        and isinstance(target.value, ast.Name)
        and target.value.id == "st"
    )


def _cache_data_functions():
    """Every ``@st.cache_data``-decorated function in app.py, as
    ``(name, lineno, [parameter names], [names of parameters with defaults])``."""
    tree = ast.parse(APP.read_text(encoding="utf-8"), filename=str(APP))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_st_cache_data(d) for d in node.decorator_list):
            continue
        a = node.args
        positional = a.posonlyargs + a.args
        names = [p.arg for p in positional + a.kwonlyargs]
        if a.vararg:
            names.append(a.vararg.arg)
        if a.kwarg:
            names.append(a.kwarg.arg)
        # Defaults align with the LAST len(defaults) positional params.
        defaulted = [p.arg for p in positional[len(positional) - len(a.defaults):]] if a.defaults else []
        defaulted += [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        found.append((node.name, node.lineno, names, defaulted))
    return found


CACHED = _cache_data_functions()
CACHED_BY_NAME = {name: (lineno, names, defaulted) for name, lineno, names, defaulted in CACHED}


class TestStaticScan:
    def test_scanner_sees_the_loaders(self):
        # If the scanner silently found nothing, the other tests would pass
        # vacuously. Pin the loaders we know are cached.
        expected = {cached for _, cached, _ in CSV_LOADERS} | {name for name, _ in JSON_LOADERS}
        missing = expected - set(CACHED_BY_NAME)
        assert not missing, f"@st.cache_data loaders not found in app.py: {sorted(missing)}"

    @pytest.mark.parametrize("name,lineno,names,defaulted", CACHED, ids=[c[0] for c in CACHED])
    def test_no_cache_data_function_is_keyed_only_on_underscore_params(self, name, lineno, names, defaulted):
        # A function with parameters, ALL of which start with "_", is cached on
        # nothing: every call returns whatever the first call produced. (A
        # zero-parameter cached function is deliberately a constant -- allowed.)
        if not names:
            return
        hashed = [n for n in names if not n.startswith("_")]
        assert hashed, (
            f"app.py:{lineno} `{name}` takes only underscore-prefixed parameters "
            f"{names}; st.cache_data skips those when building the key, so the "
            f"cache never invalidates. Drop the underscore (e.g. `mtime`)."
        )

    @pytest.mark.parametrize("public,cached,_core", CSV_LOADERS, ids=[c[0] for c in CSV_LOADERS])
    def test_csv_loaders_key_on_mtime_and_schema_version_without_defaults(self, public, cached, _core):
        lineno, names, defaulted = CACHED_BY_NAME[cached]
        assert names == ["mtime", "schema_ver"], (
            f"app.py:{lineno} `{cached}` should take exactly (mtime, schema_ver), got {names}"
        )
        # A default is NOT part of the cache key when the caller omits the arg,
        # so `schema_ver=_LOADER_SCHEMA_VERSION` as a default would quietly
        # reintroduce the bug. The public wrapper must pass it explicitly.
        assert not defaulted, (
            f"app.py:{lineno} `{cached}` has defaulted parameters {defaulted}; "
            f"defaults are invisible to st.cache_data -- pass the value from `{public}` instead."
        )
        assert public not in CACHED_BY_NAME, (
            f"`{public}` must be the un-cached wrapper that supplies _LOADER_SCHEMA_VERSION"
        )

    @pytest.mark.parametrize("name,_core", JSON_LOADERS, ids=[c[0] for c in JSON_LOADERS])
    def test_json_loaders_key_on_mtime(self, name, _core):
        lineno, names, defaulted = CACHED_BY_NAME[name]
        assert names == ["mtime"], f"app.py:{lineno} `{name}` should take exactly (mtime,), got {names}"
        assert not defaulted

    def test_analysis_caches_hash_their_dataframes_by_content(self):
        # These take DataFrames on purpose; an "_df" rename would make the cache
        # serve the first schedule it ever saw to every later one.
        for name in ("_expand_schedule_cached", "_build_capacity_table_cached"):
            _, names, _ = CACHED_BY_NAME[name]
            assert names and not any(n.startswith("_") for n in names), (name, names)


# ---------------------------------------------------------------------------
# Behaviour: count how often the underlying core loader actually runs.
# ---------------------------------------------------------------------------
app = pytest.importorskip("app")

# Negative mtimes can never collide with a real file's mtime that another test
# (or the app import itself) may have warmed the cache with.
MTIME_A, MTIME_B = -1.0, -2.0


def _count_calls(monkeypatch, core_name):
    calls = []

    def fake_loader(*args, **kwargs):
        calls.append(1)
        return {"load": len(calls)}  # picklable stand-in for the real frame/dict

    monkeypatch.setattr(app, core_name, fake_loader)
    return calls


@pytest.fixture
def cleared():
    """Clear a cached function before AND after a test so neither a warm entry
    from the app import nor our stand-in payload leaks across tests."""
    cleared_fns = []

    def _clear(fn):
        fn.clear()
        cleared_fns.append(fn)
        return fn

    yield _clear
    for fn in cleared_fns:
        fn.clear()


class TestCsvLoadersInvalidate:
    @pytest.mark.parametrize("public,cached,core", CSV_LOADERS, ids=[c[0] for c in CSV_LOADERS])
    def test_repeat_mtime_hits_new_mtime_misses(self, monkeypatch, cleared, public, cached, core):
        cleared(getattr(app, cached))
        calls = _count_calls(monkeypatch, core)
        load = getattr(app, public)

        load(MTIME_A)
        load(MTIME_A)
        assert len(calls) == 1, "same mtime must be a cache hit"

        load(MTIME_B)
        assert len(calls) == 2, (
            f"`{public}` did not reload for a new mtime -- the file-change "
            f"cache-buster is not part of the key"
        )

    @pytest.mark.parametrize("public,cached,core", CSV_LOADERS, ids=[c[0] for c in CSV_LOADERS])
    def test_schema_version_bump_misses(self, monkeypatch, cleared, public, cached, core):
        cleared(getattr(app, cached))
        calls = _count_calls(monkeypatch, core)
        load = getattr(app, public)

        load(MTIME_A)
        assert len(calls) == 1

        monkeypatch.setattr(app, "_LOADER_SCHEMA_VERSION", app._LOADER_SCHEMA_VERSION + 1)
        load(MTIME_A)
        assert len(calls) == 2, (
            f"`{public}` served the old frame after a _LOADER_SCHEMA_VERSION bump "
            f"-- the bump is not part of the cache key"
        )
        load(MTIME_A)
        assert len(calls) == 2, "the bumped version must then be a cache hit again"


class TestJsonLoadersInvalidate:
    @pytest.mark.parametrize("name,core", JSON_LOADERS, ids=[c[0] for c in JSON_LOADERS])
    def test_repeat_mtime_hits_new_mtime_misses(self, monkeypatch, cleared, name, core):
        load = cleared(getattr(app, name))
        calls = _count_calls(monkeypatch, core)

        load(MTIME_A)
        load(MTIME_A)
        assert len(calls) == 1, "same mtime must be a cache hit"

        load(MTIME_B)
        assert len(calls) == 2, f"`{name}` did not reload for a new mtime"
