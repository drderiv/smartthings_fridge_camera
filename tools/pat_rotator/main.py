#!/usr/bin/env python3
"""Run the token rotator service (or `main.py pat|food|kics` for a one-off run)."""

import sys

from rotator.cli import main

if __name__ == "__main__":
    sys.exit(main())
