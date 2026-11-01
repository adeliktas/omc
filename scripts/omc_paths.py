"""Host paths for the fleet scripts.

Unset OMC_DATA keeps every path exactly where production has it (/var/lib, /var/log, /run, /etc).
An OMC instance sets OMC_DATA to its git-ignored data dir and everything moves under it.
"""
import json
import os
from pathlib import Path

OMC_HOME = Path(os.environ.get("OMC_HOME") or Path(__file__).resolve().parents[1])
OMC_DATA = Path(os.environ["OMC_DATA"]) if os.environ.get("OMC_DATA") else None
HOME = Path.home()  # /root for production, /home/omc-<name> for an instance


def state_dir(name: str) -> Path:
    return OMC_DATA / "state" / name if OMC_DATA else Path("/var/lib") / name


def log_path(name: str) -> Path:
    return OMC_DATA / "log" / f"{name}.log" if OMC_DATA else Path("/var/log") / f"{name}.log"


def lock_path(name: str) -> Path:
    return OMC_DATA / "run" / f"{name}.lock" if OMC_DATA else Path("/run") / f"{name}.lock"


def etc_path(rel: str) -> Path:
    return OMC_DATA / "etc" / rel if OMC_DATA else Path("/etc") / rel


def load_fleet_config() -> dict:
    """Instance-specific fleet values (private boards, forbidden paths, intake sources); {} if unset."""
    try:
        return json.loads(etc_path("mc-team/fleet.json").read_text())
    except (OSError, ValueError):
        return {}
