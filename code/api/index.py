"""Fallback Vercel entry point when the project Root Directory is ``code``.

The canonical deployment adapter is at ``/api/index.py``. This copy lets the
same application deploy when an existing Vercel project is configured with
``code`` as its root directory.
"""
from pathlib import Path
import sys


APP_DIRECTORY = Path(__file__).resolve().parents[1]
if str(APP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APP_DIRECTORY))

from dashboard_v2 import Handler as handler  # noqa: E402
