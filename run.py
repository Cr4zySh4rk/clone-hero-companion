"""Entry point used by PyInstaller and `python run.py`."""
import sys

from chcompanion.cli import main

if __name__ == "__main__":
    sys.exit(main())
