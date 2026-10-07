"""Run the IVP checks for one Mist site, guided step by step.

    python ivp.py

It asks for the site ID (and the API token if it isn't exported), shows site
insights, then a menu of the MOP's IVP Test Plan sections.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))  # works without pip install

from ivp_runner.interactive import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
