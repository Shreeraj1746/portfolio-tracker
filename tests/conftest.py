from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root is importable in pytest runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
