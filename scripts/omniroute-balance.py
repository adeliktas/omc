"""Balance eligible members and enable prompt-aware omniroute-auto routing.

Existing intent, complexity and model-fitness scoring chooses the primary;
round-robin over the same eligible accounts remains the fallback. Dry-run by
default; --apply writes even when only the routing policy changed. Explicit
--round-robin restores the old strategy. Each member runs the newest model its
account's live catalog serves; account pins and other config are preserved.

Run inside the backend container: python scripts/omniroute-balance.py [--apply].
"""

import asyncio
import re
import sys

import httpx

from app.services import omniroute_sync as o
from app.services.omniroute_combo_sync import prompt_routing_config

# Owner 2026-10-06: always run the newest top model each account serves.
# Codex follows the account's live catalog; the others name their newest line.
CODEX_SOL = re.compile(r"gpt-(\d+)(?:\.(\d+))?-sol")  # gpt-6-sol == 6.0
NEURALWATT_MODEL = "glm-5.3"
CLAUDE_CODE_P_MODEL = "opus"
CONTEXTS = {"Codex account": 272000, "Claude Code P": 200000, "neuralwatt": 131072}
# Older-to-newer lines without a version number in the name.
LADDER = {"haiku": (0,), "sonnet": (1,), "opus": (2,), "glm-5.3-flash": (0,), "glm-5.3": (1,)}


CODEX_ID = re.compile(
    r"gpt-(\d+)(?:\.(\d+))?(?:-(luna|terra|sol))?(?:-(?:low|medium|high|xhigh|max|ultra))?"
)
TIERS = {None: 0, "luna": 0, "terra": 1, "sol": 2}


def version(model):
    """Comparable age of a member model, or None when unknown (never replaced)."""
    name = model.split("/", 1)[-1]
    codex = CODEX_ID.fullmatch(name)
    if codex:  # gpt-5.5 < gpt-5.6-terra < gpt-5.6-sol(-max) < gpt-6-sol < gpt-6.1-sol
        return (int(codex[1]), int(codex[2] or 0), TIERS[codex[3]])
    return LADDER.get(name)


def latest_model(connection, catalog):
    """(model, label) for the newest model this account serves, or None if unknown."""
    provider = connection["provider"]
    if provider == "codex":
        sol = max(
            (m for m in catalog if isinstance(m, str) and CODEX_SOL.fullmatch(m)),
            key=lambda m: tuple(int(v or 0) for v in CODEX_SOL.fullmatch(m).groups()),
            default=None,
        )
        return (f"codex/{sol}", "Codex account") if sol else None
    if (
        (connection.get("providerSpecificData") or {})
        .get("baseUrl", "")
        .endswith(":20130/v1")
    ):
        name, label = CLAUDE_CODE_P_MODEL, "Claude Code P"
    elif connection["name"] == "neuralwatt":
        name, label = NEURALWATT_MODEL, "neuralwatt"
    else:
        return None
    return (f"{provider}/{name}", label) if name in catalog else None


def balanced_combo(combo, connections, catalogs=None, *, round_robin=False):
    """Upgrade members to their account's newest model and add missing accounts.

    Every existing member is kept (weight 0 included). A member changes only when
    its catalog serves a newer model than it runs: no catalog, an unknown model
    or an older catalog leaves it as it is.
    """
    latest = {}
    for connection in connections:
        if connection.get("isActive"):
            pick = latest_model(connection, (catalogs or {}).get(connection["id"], ()))
            if pick:
                latest[connection["id"]] = (connection["provider"], *pick)
    have = {m.get("connectionId") for m in combo["models"]}
    members = [
        (
            {**m, "model": latest[m["connectionId"]][1]}
            if m.get("connectionId") in latest
            and version(m.get("model") or "") is not None
            and version(m.get("model") or "") < version(latest[m["connectionId"]][1])
            else m
        )
        for m in combo["models"]
    ] + [
        {
            "id": "member-" + connection_id,
            "kind": "model",
            "model": model,
            "providerId": provider,
            "connectionId": connection_id,
            "weight": 1,
            "label": label,
        }
        for connection_id, (provider, model, label) in latest.items()
        if connection_id not in have
    ]
    config = dict(combo.get("config") or {})
    config["concurrencyPerModel"] = max(2, int(config.get("concurrencyPerModel") or 1))
    contexts = dict(config.get("memberContexts") or {})
    for member in members:
        pick = latest.get(member.get("connectionId"))
        if pick:
            contexts.setdefault(pick[1], CONTEXTS[pick[2]])
    config["memberContexts"] = contexts
    if not round_robin:
        config = prompt_routing_config(config)
    return {
        "name": combo["name"],
        "strategy": "round-robin" if round_robin else "auto",
        "models": members,
        "config": config,
    }


async def catalog(client, headers, connection_id):
    response = await client.get(
        o._MGMT_URL + f"/api/providers/{connection_id}/models", headers=headers
    )
    if response.status_code != 200:
        return []
    data = response.json()
    models = (data.get("models") if isinstance(data, dict) else data) or []
    ids = [m.get("id") if isinstance(m, dict) else m for m in models]
    return [i for i in ids if isinstance(i, str)]


async def main():
    async with httpx.AsyncClient(timeout=20) as client:
        headers = await o._mgmt_headers(write=False)
        providers = await client.get(o._MGMT_URL + "/api/providers", headers=headers)
        providers.raise_for_status()
        response = await client.get(o._MGMT_URL + "/api/combos", headers=headers)
        response.raise_for_status()
        combo = next(
            c for c in response.json()["combos"] if c["name"] == "omniroute-auto"
        )
        connections = providers.json()["connections"]
        catalogs = {
            c["id"]: await catalog(client, headers, c["id"])
            for c in connections
            if c.get("isActive")
        }
        body = balanced_combo(
            combo,
            connections,
            catalogs,
            round_robin="--round-robin" in sys.argv,
        )
        before = {m.get("id"): m.get("model") for m in combo["models"]}
        for member in body["models"]:
            if before.get(member["id"]) != member["model"]:
                print("member:", before.get(member["id"]), "->", member["model"])
        changed = any(combo.get(key) != value for key, value in body.items())
        print(
            "strategy:",
            body["strategy"],
            "members:",
            len(body["models"]),
            "changed:",
            changed,
        )
        if "--apply" in sys.argv and changed:
            result = await client.put(
                o._MGMT_URL + f"/api/combos/{combo['id']}",
                headers=await o._mgmt_headers(write=True),
                json=body,
            )
            result.raise_for_status()
            print("PUT", result.status_code)


if __name__ == "__main__":
    asyncio.run(main())
