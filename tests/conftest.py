import os

# Telemetry must never send from tests (see chcompanion/telemetry.py); telemetry tests build their own instance.
os.environ["CHC_NO_TELEMETRY"] = "1"
os.environ["CHC_ACCEPT_TERMS"] = "1"       # tests that exercise the acceptance gate clear this themselves
