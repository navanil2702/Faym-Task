"""Locating the sample workbook.

Prefer the copy committed in ``data/``, so a fresh clone can run the whole suite
with nothing to download and no path to configure. Fall back to ``~/Downloads``
for anyone still working from the originally supplied file.
"""

from __future__ import annotations

from pathlib import Path

NAME = "Faym Status Test Orders.xlsx"
IN_REPO = Path(__file__).resolve().parent.parent / "data" / NAME
IN_DOWNLOADS = Path.home() / "Downloads" / NAME

SOURCE = IN_REPO if IN_REPO.exists() else IN_DOWNLOADS
