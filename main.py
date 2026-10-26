"""Run without an editable installation: python main.py --help."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from llm_test_tool.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
