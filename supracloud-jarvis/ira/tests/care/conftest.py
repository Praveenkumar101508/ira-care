"""The care unit tests never reach a model: the deterministic fake stands in."""
import os

os.environ.setdefault("IRA_CARE_MODEL", "fake")
