"""Ambience console endpoints (turncall ADR-0021): per-agent proxies onto the
agent's project's ambience sounds, and the choice saved as ordinary config."""

import httpx

from tests.conftest import backend_row

SOUND = {"id": "s1", "name": "room.mp3", "duration_ms": 30000, "rates": [8000, 16000]}


def _turncall_error(status: int, error: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://tc/v1/ambience-sounds")
    response = httpx.Response(status, json={"error": error}, request=request)
    return httpx.HTTPStatusError("x", request=request, response=response)


def test_lists_the_projects_sounds(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.list_ambience_sounds.return_value = [SOUND]

    data = client.get("/agents/a1/ambience/sounds").json()["data"]

    assert data == {"sounds": [SOUND]}
    mocks.client.list_ambience_sounds.assert_awaited_once_with("tc_key")


def test_another_workspaces_agent_is_404(client, mocks):
    mocks.registry.get_backend.return_value = None
    assert client.get("/agents/a1/ambience/sounds").status_code == 404
    mocks.client.list_ambience_sounds.assert_not_awaited()


def test_upload_forwards_the_file_and_turncalls_warnings(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    warned = {**SOUND, "warnings": [{"code": "inaudible_on_phone", "message": "m"}]}
    mocks.client.upload_ambience_sound.return_value = warned

    r = client.post(
        "/agents/a1/ambience/sounds",
        files={"file": ("room.mp3", b"ID3", "audio/mpeg")},
    )

    assert r.status_code == 200
    assert r.json()["data"]["warnings"][0]["code"] == "inaudible_on_phone"
    mocks.client.upload_ambience_sound.assert_awaited_once_with(
        "room.mp3", b"ID3", "audio/mpeg", "tc_key"
    )


def test_an_unreadable_upload_keeps_turncalls_status_and_fix(client, mocks):
    """The .m4a hint is the whole point of the 415 — it must reach the console."""
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.upload_ambience_sound.side_effect = _turncall_error(
        415, "Could not decode the file. ... `ffmpeg -i memo.m4a memo.wav`."
    )

    r = client.post(
        "/agents/a1/ambience/sounds",
        files={"file": ("memo.m4a", b"x", "audio/mp4")},
    )

    assert r.status_code == 415
    assert "ffmpeg" in r.json()["detail"]


def test_deleting_a_sound_in_use_is_turncalls_409(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.delete_ambience_sound.side_effect = _turncall_error(
        409, "Ambience sound is used by Agent v1"
    )

    r = client.delete("/agents/a1/ambience/sounds/s1")

    assert r.status_code == 409
    assert "Agent v1" in r.json()["detail"]


def test_audio_streams_the_phone_rendition_by_default(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_ambience_audio.return_value = b"0123456789"

    r = client.get("/agents/a1/ambience/sounds/s1/audio")

    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    mocks.client.get_ambience_audio.assert_awaited_once_with("s1", 8000, "tc_key")


def test_audio_honors_range_for_safari(client, mocks):
    mocks.registry.get_backend.return_value = backend_row()
    mocks.client.get_ambience_audio.return_value = b"0123456789"

    r = client.get(
        "/agents/a1/ambience/sounds/s1/audio?rate=16000", headers={"Range": "bytes=2-5"}
    )

    assert r.status_code == 206
    assert r.content == b"2345"
    assert mocks.client.get_ambience_audio.await_args.args[1] == 16000


def test_a_form_save_carries_the_choice_to_turncall_and_the_registry(client, mocks):
    """Choosing a sound is an ordinary config edit. Both destinations, because
    call-init builds its inline agent from the registry config (ADR-0020)."""
    mocks.registry.get_backend.return_value = backend_row()
    ambience = {"sound": "s1", "volume": 0.2}

    r = client.put(
        "/agents/a1",
        json={"config": {"name": "Agent", "system_prompt": "hi", "ambience": ambience}},
    )

    assert r.status_code == 200
    assert mocks.client.update_agent.await_args.args[1]["config"]["ambience"] == ambience
    assert mocks.registry.update_config.await_args.args[2]["ambience"] == ambience
