"""Gateway 2026.9 roster/channel schema compatibility.

The gateway renamed `agents.list` -> `agents.entries` (array -> map) and
`channels.defaults.heartbeat` -> `channels.defaults.heartbeatVisibility`, and
rejects the old keys outright. A rejected config.patch failed the whole agent
provision, including the AUTH_TOKEN write agents need to call back into MC.
"""
from app.services.openclaw.provisioning import (
    _agent_entries_as_list,
    _agent_roster_patch,
    _channel_heartbeat_key,
)


def test_entries_map_is_normalized_to_a_list_with_ids():
    section = {"entries": {"mc-1": {"name": "QA", "workspace": "/w/1"}}}
    assert _agent_entries_as_list(section) == [
        {"name": "QA", "workspace": "/w/1", "id": "mc-1"}
    ]


def test_legacy_list_is_passed_through():
    section = {"list": [{"id": "mc-1", "workspace": "/w/1"}]}
    assert _agent_entries_as_list(section) == [{"id": "mc-1", "workspace": "/w/1"}]


def test_roster_patch_renders_entries_for_a_new_gateway():
    section = {"entries": {"mc-1": {"name": "QA"}}}
    patch = _agent_roster_patch(section, [{"id": "mc-1", "name": "QA", "workspace": "/w"}])
    assert patch == {"entries": {"mc-1": {"name": "QA", "workspace": "/w"}}}
    assert "list" not in patch


def test_roster_patch_renders_list_for_a_legacy_gateway():
    section = {"list": []}
    patch = _agent_roster_patch(section, [{"id": "mc-1"}])
    assert patch == {"list": [{"id": "mc-1"}]}


def test_round_trip_preserves_entries():
    section = {"entries": {"a": {"name": "A"}, "b": {"name": "B"}}}
    assert _agent_roster_patch(section, _agent_entries_as_list(section)) == section


def test_channel_heartbeat_key_follows_the_gateway():
    assert _channel_heartbeat_key(
        {"channels": {"defaults": {"heartbeatVisibility": {}}}}
    ) == "heartbeatVisibility"
    assert _channel_heartbeat_key({"channels": {"defaults": {"heartbeat": {}}}}) == "heartbeat"
    # Unknown/empty gateway: prefer the current key, never the rejected one.
    assert _channel_heartbeat_key({}) == "heartbeatVisibility"


def test_unrecognized_key_is_extracted_from_a_gateway_complaint():
    from app.services.openclaw.provisioning import _unrecognized_config_key as f

    assert f('invalid config: agents.entries.mc-1.heartbeat: Unrecognized key: "includeReasoning"') == "includeReasoning"
    assert f('channels.defaults: Unrecognized key: "heartbeat"') == "heartbeat"
    assert f("something else entirely") is None


def test_stripping_a_key_walks_the_whole_patch():
    from app.services.openclaw.provisioning import _strip_config_key as f

    patch = {"agents": {"entries": {"a": {"heartbeat": {"every": "5m", "bad": 1}}}}}
    assert f(patch, "bad") is True
    assert patch == {"agents": {"entries": {"a": {"heartbeat": {"every": "5m"}}}}}
    assert f(patch, "missing") is False
