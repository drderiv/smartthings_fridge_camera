#!/usr/bin/env python3
"""Thin wrapper kept for backwards compatibility: same as `main.py pat`."""

import sys

from rotator.cli import pat_main

if __name__ == "__main__":
    sys.exit(pat_main())
