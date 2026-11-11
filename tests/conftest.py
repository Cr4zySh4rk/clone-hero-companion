import os

# Telemetry must never send from tests (see chcompanion/telemetry.py); telemetry tests build their own instance.
os.environ["CHC_NO_TELEMETRY"] = "1"
