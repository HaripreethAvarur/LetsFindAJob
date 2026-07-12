import sys
from pathlib import Path

# Tests run from anywhere; make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
