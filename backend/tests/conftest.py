"""让 tests 无论从哪里调用都能 import app 包。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
