"""Root execution runner for the Bookable Payable assignment.
"""
import sys
import subprocess
from pathlib import Path

candidate_kit_dir = Path(__file__).resolve().parent / "candidate_kit"
if candidate_kit_dir.exists():
    cmd = [sys.executable, "pipeline/run.py"] + sys.argv[1:]
    sys.exit(subprocess.call(cmd, cwd=str(candidate_kit_dir)))
else:
    from pipeline.run import main
    main()
