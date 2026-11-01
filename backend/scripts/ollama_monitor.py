#!/usr/bin/env python3
"""Periodic Ollama usage scraper — reads session/quotas and pushes to provider_capacity API."""

import os
import re
import sys
import json
import time
import subprocess
from datetime import datetime, timezone

API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "444b23307938cbebc42b30238c195f7e90983a2fc6eb0dbacca4ae02ec9f2441")
OLLAMA_SETTINGS_SCRIPT = os.environ.get("OLLAMA_SETTINGS_SCRIPT", "/home/host_openclaw/../Downloads/ollama_settings.sh")

def fetch_ollama_usage() -> tuple[float, float, str, str]:
    """Run ollama_settings.sh and parse usage values."""
    try:
        result = subprocess.run(
            ["bash", OLLAMA_SETTINGS_SCRIPT],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "HOME": "/root"}
        )
        output = result.stdout
    except Exception as e:
        print(f"Script error: {e}", file=sys.stderr)
        return 0, 0, "unknown", "unknown"

    session_match = re.search(r'(\d+\.?\d*)% used', output.split('\n')[0]) if output else None
    weekly_match = re.search(r'(\d+\.?\d*)% used', output.split('\n')[1]) if '\n' in output else None
    session_reset = re.search(r'Resets in ([^)]+)', output.split('\n')[0] if output else '') 
    weekly_reset = re.search(r'Resets in ([^)]+)', output.split('\n')[1] if '\n' in output else '')

    session_used = float(session_match.group(1)) if session_match else 0
    weekly_used = float(weekly_match.group(1)) if weekly_match else 0
    session_reset_str = session_reset.group(1).strip(')') if session_reset else "unknown"
    weekly_reset_str = weekly_reset.group(1).strip(')') if weekly_reset else "unknown"

    return session_used, weekly_used, session_reset_str, weekly_reset_str


def push_to_api(current_usage: int, session_used: float, weekly_used: float, session_reset: str):
    """POST current_usage to provider capacity endpoint via internal API."""
    import urllib.request

    url = f"{API_BASE}/api/v1/providers/ollama"
    # Use the patch endpoint to update usage
    payload = json.dumps({
        "current_usage": current_usage,
        "status": "online" if session_used < 95 else "overloaded",
    }).encode()

    req = urllib.request.Request(
        url, data=payload, method="PATCH",
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json",
        }
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[{datetime.now(timezone.utc).isoformat()}] Updated ollama: usage={current_usage} session={session_used}% weekly={weekly_used}%")
    except Exception as e:
        print(f"API push failed: {e}", file=sys.stderr)


def main():
    """Main loop — scrapes every 60s and pushes to API."""
    print("Ollama monitor started", file=sys.stderr)
    while True:
        try:
            session_used, weekly_used, session_reset, weekly_reset = fetch_ollama_usage()
            # Map to concurrent usage: ollama pro allows 3 concurrent
            # current_usage is the number of active sessions we think are running
            # Use session_used percentage to estimate: 0% = 0, 33%+ = 1, 66%+ = 2, 95%+ = 3
            if session_used >= 95:
                current_usage = 3
            elif session_used >= 66:
                current_usage = 2
            elif session_used >= 33:
                current_usage = 1
            else:
                current_usage = 0

            push_to_api(current_usage, session_used, weekly_used, session_reset)
        except Exception as e:
            print(f"Loop error: {e}", file=sys.stderr)

        time.sleep(60)


if __name__ == "__main__":
    main()
