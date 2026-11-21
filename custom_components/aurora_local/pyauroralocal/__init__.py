"""Local, read-only access to an Adlar Castra Aurora heat pump.

The heat pump's Hi-Flying HF-LPT270 Wi-Fi module streams JSON on TCP port
8899. This package parses that stream, keeps a merged view of the values,
explains what the fields mean, corrects the water temperature sensors and
archives the raw data. It has no dependencies outside the standard library
and never sends anything to the heat pump.

Modules:
    framing      byte stream -> JSON messages
    state        merged values and tables (AdlarState)
    client       reconnecting TCP client (AdlarClient)
    discovery    probe a host, search a subnet for the module
    fields       meaning of the fields (temperatures, power, faults, ...)
    calibration  temperature sensor calibration, energy integration
    monitoring   low-flow and fault tracking
    processor    one burst -> corrected values (BurstProcessor)
    archive      raw data archive (RawLogger)
    history      hourly history rebuilt from the archive
"""

from __future__ import annotations

from . import fields, history
from .archive import RawLogger
from .calibration import CAL_AUTO, CAL_MANUAL, CAL_MIN_SAMPLES, Calibration, EnergyIntegrator
from .client import STALE_AFTER, STATUSES, AdlarClient
from .discovery import find_module, probe, subnet_candidates
from .framing import StreamParser
from .monitoring import FaultChange, FaultTracker, LowFlowTracker
from .processor import BurstProcessor
from .state import AdlarState, to_signed16

__version__ = "0.8.0"

__all__ = [
    "CAL_AUTO",
    "CAL_MANUAL",
    "CAL_MIN_SAMPLES",
    "STALE_AFTER",
    "STATUSES",
    "AdlarClient",
    "AdlarState",
    "BurstProcessor",
    "Calibration",
    "EnergyIntegrator",
    "FaultChange",
    "FaultTracker",
    "LowFlowTracker",
    "RawLogger",
    "StreamParser",
    "__version__",
    "fields",
    "find_module",
    "history",
    "probe",
    "subnet_candidates",
    "to_signed16",
]
