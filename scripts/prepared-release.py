#!/usr/bin/env python3
"""Compatibility entry point for prepared release verification."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_search.prepared_release import main, validate_selection, verify

if __name__ == "__main__":
    main()
