#!/usr/bin/env python3
"""Thin wrapper kept for backwards compatibility: same as `main.py food`."""

import sys

from rotator.cli import food_main

if __name__ == "__main__":
    sys.exit(food_main())
