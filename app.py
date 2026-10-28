"""Vercel's entrypoint: the platform imports `app` from here and serves it.

The code lives in src/insight (a src layout), so put src on the path first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from insight.main import app

__all__ = ["app"]
