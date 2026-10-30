"""Tests run against the example rubrics, so they pass on a fresh clone and don't depend on your own."""

import os
from pathlib import Path

os.environ["RUBRICS_DIR"] = str(Path(__file__).parent.parent / "rubrics" / "examples")
