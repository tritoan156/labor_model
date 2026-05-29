"""Pytest configuration for the Labor Capacity Model test suite.

Ensures the repo root is importable so ``import core...`` and ``import app``
resolve regardless of where pytest is invoked from.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
