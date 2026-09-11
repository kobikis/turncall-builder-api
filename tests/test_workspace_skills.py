"""Workspace-authored Builder skills (ADR-0015).

The failure mode worth testing is silence: a skill whose frontmatter breaks, or
whose name collides with a built-in, does not error — it just stops being there.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import deps, main, workspace_skills


# --- what may be written -----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Restaurant Reservations", "restaurant_reservations", "-leading", "trailing-", "x" * 65],
)
def test_names_must_be_the_shape_deepagents_expects(name):
    with pytest.raises(ValueError, match="lowercase words"):
        workspace_skills.validate(name, "when to use it", "body")


def test_built_in_names_are_reserved():
    """Sources are last-wins, so a workspace skill named after a built-in would
    silently replace it — indistinguishable from a bug."""
    for builtin in workspace_skills.builtin_names():
        with pytest.raises(ValueError, match="built-in"):
            workspace_skills.validate(builtin, "when to use it", "body")
    assert "agent-tools" in workspace_skills.builtin_names()


def test_a_skill_without_a_description_is_refused():
    """It is the only part always in front of the model. Without it the skill
    exists and is never opened, which is worse than not existing."""
    with pytest.raises(ValueError, match="never opened"):
        workspace_skills.validate("my-skill", "   ", "body")


def test_a_skill_without_instructions_is_refused():
    with pytest.raises(ValueError, match="instructions"):
        workspace_skills.validate("my-skill", "when to use it", "  ")


# --- what deepagents makes of it ---------------------------------------------


def test_a_rendered_skill_is_one_deepagents_can_actually_read(tmp_path):
    """Frontmatter is generated so nobody hand-writes YAML — but a description
    carrying a colon or a quote must not break it. deepagents does the parsing,
    so deepagents does the asserting."""
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.skills import _list_skills_with_errors

    awkward = 'Use when: the caller says "a table for four" — 50% of calls'
    skill_dir = tmp_path / "restaurant-reservations"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        workspace_skills.render("restaurant-reservations", awkward, "# How to\n\nAsk covers.")
    )

    found, error = _list_skills_with_errors(
        FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True), "/"
    )
    assert error is None
    assert [s["name"] for s in found] == ["restaurant-reservations"]
    assert found[0]["description"] == awkward


# --- the mount ---------------------------------------------------------------


@pytest.fixture
def staging(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_skills, "GENERATED_DIR", str(tmp_path))
    return tmp_path


def _rows(*names):
    return [
        {"id": "1", "name": n, "description": f"when to use {n}", "body": "do this",
         "updated_at": "now"}
        for n in names
    ]


@pytest.mark.asyncio
async def test_materialize_writes_the_workspace_skills(staging, monkeypatch):
    monkeypatch.setattr(workspace_skills, "list_skills", AsyncMock(return_value=_rows("takeaway-orders")))
    directory = await workspace_skills.materialize(None, "ws-1")
    assert directory is not None
    content = (staging / "workspace-skills" / "ws-1" / "takeaway-orders" / "SKILL.md").read_text()
    assert "name: takeaway-orders" in content


@pytest.mark.asyncio
async def test_a_deleted_skill_stops_applying(staging, monkeypatch):
    """The directory is rebuilt to match the database, so a removed skill does
    not linger on whichever host happened to write it."""
    monkeypatch.setattr(workspace_skills, "list_skills", AsyncMock(return_value=_rows("a", "b")))
    await workspace_skills.materialize(None, "ws-1")
    monkeypatch.setattr(workspace_skills, "list_skills", AsyncMock(return_value=_rows("a")))
    directory = await workspace_skills.materialize(None, "ws-1")
    assert sorted(p.name for p in (staging / "workspace-skills" / "ws-1").iterdir()) == ["a"]
    assert directory is not None


@pytest.mark.asyncio
async def test_a_workspace_with_no_skills_mounts_nothing(staging, monkeypatch):
    monkeypatch.setattr(workspace_skills, "list_skills", AsyncMock(return_value=[]))
    assert await workspace_skills.materialize(None, "ws-1") is None


def test_the_mount_is_added_only_when_it_has_something(staging, monkeypatch):
    from app import builder as builder_mod

    empty = str(staging / "nothing")
    assert builder_mod._skill_sources(empty) == ["/skills/"]
    assert builder_mod._skill_sources(str(staging)) == ["/skills/", "/workspace-skills/"]
    assert "/workspace-skills/" in builder_mod._backend(None, str(staging)).routes


# --- endpoints ---------------------------------------------------------------


def test_list_reports_the_reserved_names(client, mocks):
    """The console needs them to explain why a name is refused, so they travel
    with the list rather than being duplicated in the frontend."""
    mocks.workspace_skills.list_skills.return_value = []
    mocks.workspace_skills.builtin_names.return_value = {"agent-tools", "guardrails"}
    r = client.get("/skills")
    assert r.status_code == 200
    assert "agent-tools" in r.json()["data"]["reserved"]


def test_saving_over_a_built_in_name_is_refused(client, mocks):
    mocks.workspace_skills.builtin_names.return_value = {"agent-tools"}
    mocks.workspace_skills.validate.side_effect = ValueError(
        "'agent-tools' is a built-in skill — choose another name"
    )
    r = client.put("/skills/agent-tools", json={"description": "d", "body": "b"})
    assert r.status_code == 422
    assert "built-in" in r.json()["detail"]
    mocks.workspace_skills.save_skill.assert_not_called()


def test_saving_a_skill(client, mocks):
    mocks.workspace_skills.validate.side_effect = None
    mocks.workspace_skills.save_skill.return_value = {"name": "takeaway-orders"}
    r = client.put("/skills/takeaway-orders", json={"description": "d", "body": "b"})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "takeaway-orders"


def test_deleting_a_skill_that_is_not_there_is_404(client, mocks):
    mocks.workspace_skills.delete_skill.return_value = False
    assert client.delete("/skills/nope").status_code == 404


def test_writing_a_skill_is_admin_only(mocks):
    """A skill is prompt text in a builder holding the deployment's LLM key —
    a narrower trust boundary than editing one agent."""
    from fastapi.testclient import TestClient

    def forbidden():
        raise HTTPException(status_code=403, detail="admins only")

    ctx = deps.AuthContext(user={"id": "u"}, workspace_id="ws-1", role="editor")
    main.app.dependency_overrides[deps.require_member] = lambda: ctx
    main.app.dependency_overrides[deps.require_admin] = forbidden
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        mocks.workspace_skills.list_skills.return_value = []
        assert c.get("/skills").status_code == 200  # an editor may read
        assert c.put("/skills/x", json={"description": "d", "body": "b"}).status_code == 403
        assert c.delete("/skills/x").status_code == 403
    finally:
        main.app.dependency_overrides.clear()
