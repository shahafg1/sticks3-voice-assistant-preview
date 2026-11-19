# Loaded automatically by Python children of the tests (this directory is on their PYTHONPATH): installs the isolation guard.
import os
import sys

if os.environ.get("STICK_TEST_GUARD") == "1":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import isolation_guard  # noqa: F401  (installs the hook on import)
    finally:
        sys.path.pop(0)
