"""Orphan-container reconcile (startup): a stack restart can drop registry rows
while old agent containers keep running and squat on their host ports, so the
next generation fails with 'port already allocated'. `orphan_container_names`
decides which containers to reap."""

from app.backends.generator import orphan_container_names


def test_reaps_agent_container_with_no_live_slug():
    names = [
        "turncall-agent-my-hotel-receptionist-d0413823-...-1",  # row gone -> orphan
        "turncall-agent-support-bot-ab12cd34-support-bot-ab12cd34-1",  # live
    ]
    live = {"support-bot-ab12cd34"}
    assert orphan_container_names(names, live) == [names[0]]


def test_keeps_all_live_containers():
    names = ["turncall-agent-support-bot-ab12cd34-support-bot-ab12cd34-1"]
    assert orphan_container_names(names, {"support-bot-ab12cd34"}) == []


def test_ignores_non_agent_containers():
    names = ["localstack-postgres-1", "turncall-builder-api-api-1"]
    assert orphan_container_names(names, set()) == []


def test_no_live_slugs_reaps_every_agent_container():
    names = ["turncall-agent-x-1", "localstack-redis-1"]
    assert orphan_container_names(names, set()) == ["turncall-agent-x-1"]
