#!/usr/bin/env python3
"""Thin wrapper kept for backwards compatibility: same as `main.py kics`."""

import sys

from rotator.cli import kics_main

if __name__ == "__main__":
    sys.exit(kics_main())
