"""Pytest configuration for the Labor Capacity Model test suite.

Ensures the repo root is importable so ``import core...`` and ``import app``
resolve regardless of where pytest is invoked from.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _catalog_mirror_in_tmp(tmp_path, monkeypatch):
    """Every catalog CSV save mirrors the pushed text under
    ``core.catalog_storage.REPO_ROOT / "data"`` so the running app uses it at
    once. Point that root at a temp folder for every test, so a save path
    driven with a stubbed GitHub can never rewrite the tracked ``data/*.csv``.
    (AppTest runs share the imported ``core`` modules, so the patch reaches
    them too.)"""
    import core.catalog_storage as _cs
    monkeypatch.setattr(_cs, "REPO_ROOT", tmp_path / "repo")
