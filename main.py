"""Entry point for Sitekeeper."""

import sys

from mysql_runner.app import run

if __name__ == "__main__":
    sys.exit(run())
