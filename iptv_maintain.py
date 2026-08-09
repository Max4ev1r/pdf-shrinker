#!/usr/bin/env python3
"""Hermes cron entrypoint for the private APTV maintainer."""

import subprocess
import sys


COMMAND = ["/Users/max/.hermes/iptv-maintainer/.venv/bin/python", "/Users/max/.hermes/iptv-maintainer/maintainer.py"]


def main():
    result = subprocess.run(COMMAND, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = result.stdout.strip()
    if output:
        print(output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
