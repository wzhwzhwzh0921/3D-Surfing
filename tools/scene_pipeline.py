"""Compatibility entry point. Implementation: scene_factory.screening.intake."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_factory.screening.intake import main


if __name__ == "__main__":
    raise SystemExit(main())
