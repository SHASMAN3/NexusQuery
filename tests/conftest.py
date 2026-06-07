"""
conftest.py
-----------
Adds the project root to sys.path so pytest and uvicorn can resolve
all internal imports (api.*, rag.*, db.*, etc.) regardless of the
working directory or how the runner is invoked.

This file must live at the project root (same level as api/, rag/, db/).
"""
import sys
from pathlib import Path

# Insert project root as the first entry on sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))