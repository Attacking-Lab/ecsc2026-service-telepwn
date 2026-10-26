#!/usr/bin/env python3
"""Run a single command across the phone fleet via phonesh.py.

Each team's phone is reachable at 10.60.X.4. By default the command is run
against all 43 teams (X = 1..43); pass explicit numbers to target a subset.

Usage:
    mass_phonesh.py CMD [X ...]

Examples:
    mass_phonesh.py "uptime"          # all teams 1..43
    mass_phonesh.py "uptime" 11 7     # only 10.60.11.4 and 10.60.7.4
"""
import argparse
import os
import subprocess
import sys

PHONESH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phonesh.py")
TEAMS = range(1, 44)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("numbers", nargs="*", type=int)
    args = ap.parse_args()

    numbers = args.numbers if args.numbers else list(TEAMS)

    for x in numbers:
        host = f"10.60.{x}.4"
        print(f"########## team {x} ({host}) ##########")
        try:
            subprocess.run(
                [sys.executable, PHONESH, "--host", host, args.cmd],
                check=False,
            )
        except Exception as exc:
            print(f"!!! team {x} ({host}) failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
