"""Vercel serverless entry point for the BuyCycle Python application.

The local app remains in ``code/dashboard_v2.py`` for local development.
Vercel invokes the exported handler class for every rewritten request rather
than starting a persistent HTTP server.
"""
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIRECTORY = PROJECT_ROOT / "code"
if str(APP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APP_DIRECTORY))

from dashboard_v2 import Handler as handler  # noqa: E402
