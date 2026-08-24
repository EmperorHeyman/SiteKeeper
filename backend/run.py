"""Development launcher: python run.py [--port 8766]

The packaged sidecar calls app.main:main instead; this exists so the backend can
be driven on its own while working on the Svelte front end.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the sibling mysql_runner package importable when run from source.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Sitekeeper backend")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token", default="", help="require this token header")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    os.environ["MYSQLRUNNER_PORT"] = str(args.port)
    if args.token:
        os.environ["MYSQLRUNNER_TOKEN"] = args.token

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
