#!/usr/bin/env python3
"""Start the Faym Returns control panel.

    python3 serve.py

Puts ``src/`` on the import path so there is nothing to install and no
PYTHONPATH to remember, then serves the panel on http://127.0.0.1:8000.

Bound to localhost on purpose: the panel drives a real logged-in browser
session, so it must not be reachable from the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from faym_returns.webapp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
