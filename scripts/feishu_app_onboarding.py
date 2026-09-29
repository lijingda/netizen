#!/usr/bin/env python3
"""Development entry for the packaged onboarding helper."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from netizen_cli.feishu_app_onboarding import main

if __name__ == "__main__":
    raise SystemExit(main())
