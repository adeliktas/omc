"""Jira admission, not disclosure approval or a general tool/network grant."""

from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path



def _private_lane() -> dict:
    """fleet.json `private_lane`: the one local model and router lane private boards may use."""
    # ponytail: repeats omc_paths.etc_path in 2 lines; backend modules do not import host scripts.
    data = os.environ.get("OMC_DATA")
    path = Path(data, "etc/mc-team/fleet.json") if data else Path("/etc/mc-team/fleet.json")
    try:
        return json.loads(path.read_text()).get("private_lane") or {}
    except (OSError, ValueError, AttributeError):
        return {}


_LANE = _private_lane()  # unset: no lane, every request is refused
LOCAL_PROVIDER = _LANE.get("local_provider", "")
LOCAL_MODEL = _LANE.get("local_model", "")
LOCAL_ENDPOINT = _LANE.get("local_endpoint", "")
ROUTER_PROVIDER = "omniroute-api"
ROUTER_MODEL = _LANE.get("router_model", "")
ROUTER_ENDPOINT = _LANE.get("router_endpoint", "")
# The router's self-authenticating private route and its headers (OMC OmniRoute patch defaults).
ROUTER_PATH = _LANE.get("router_path", "/v1/private/chat/completions")
MODEL_HEADER = _LANE.get("model_header", "X-OMC-Model")
METHOD_HEADER = _LANE.get("method_header", "X-OMC-Source-Method")
DEFAULT_OPENCLAW_HOME = "/home/host_openclaw"


def unique_json_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous_configuration")
        result[key] = value
    return result


def read_trusted_json(path: Path, *, private: bool = False):
    """Shared FD-pinned namespace admission; private credentials stay in place."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("configuration_refused")
    with contextlib.ExitStack() as files:
        directory = None
        # Retain every ancestor/no-follow/ownership check for both authority files.
        for component in ("/", *path.parts[1:-1]):
            directory = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            files.callback(os.close, directory)
            parent = os.fstat(directory)
            sticky_root = parent.st_uid == 0 and parent.st_mode & stat.S_ISVTX
            if parent.st_uid not in (0, os.getuid()) or (
                parent.st_mode & 0o022 and not sticky_root
            ):
                raise ValueError("configuration_refused")
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & (0o077 if private else 0o022)
                or not 0 < info.st_size <= 4 * 1024 * 1024
            ):
                raise ValueError("configuration_refused")
            return json.loads(
                file.read(4 * 1024 * 1024 + 1), object_pairs_hook=unique_json_members
            )


def require_jira_access(
    provider: str, model: str, endpoint: str, method: str = "GET"
) -> None:
    """Literal trust AND exact lane; refuse before protected input/auth/network.

    Callers are trusted adapters, not model-supplied provider/lane assertions.
    The existing MC sidecar is authoritative; never accept trust from a prompt
    or tool argument. Root/operator configuration remains a trust boundary.
    """
    try:
        if not endpoint or (provider, model, endpoint, method) not in (
            (LOCAL_PROVIDER, LOCAL_MODEL, LOCAL_ENDPOINT, "GET"),
            (ROUTER_PROVIDER, ROUTER_MODEL, ROUTER_ENDPOINT, "GET"),
        ):
            raise ValueError("lane_refused")
        path = (
            Path(os.environ.get("OPENCLAW_HOME", DEFAULT_OPENCLAW_HOME))
            / "openclaw.mc.json"
        )
        data = read_trusted_json(path)
        if data["models"][provider][model].get("fully_trusted") is not True:
            raise ValueError("trust_required")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise PermissionError("jira_local_trust_required") from None
