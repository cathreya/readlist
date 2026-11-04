"""Shared test configuration -- ensures project modules are importable."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")

for _path in (PROJECT_ROOT, APP_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
