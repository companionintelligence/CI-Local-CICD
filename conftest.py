"""Put the repo root on sys.path so `local_ci` imports without installation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
