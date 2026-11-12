#!/usr/bin/env python3
"""Entry point: `python3 -I hints.py live|backfill|atlas …` (see hintmap/build.py).

Adds this folder to the import path itself, so it also works in Python's isolated mode (-I).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hintmap import build  # noqa: E402

if __name__ == "__main__":
    sys.exit(build.main())
