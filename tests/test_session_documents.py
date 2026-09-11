"""Documents staged on a Session before an Agent exists.

The interesting cases are the ones the browser-held version could not do:
telling the Builder what is really attached, and retrying a failed forward.
"""

import io

import pytest

from app import session_docs
from app.builder import ComposeResult
from app.routers import sessions

from conftest import FakeStream

VALID_CONFIG = {
    "name": "A",
    "system_prompt": "hi",
    "llm": {"provider": "openai", "model": "gpt-4o-mini"},
    "tts": {"provider": "deepgram", "voice": "aura-2-helena-en"},
}


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Point the staging volume at a temp dir for the whole module."""
    monkeypatch.setattr(session_docs, "GENERATED_DIR", str(tmp_path))
    return tmp_path


# --- the trust boundary ------------------------------------------------------


def test_safe_name_cannot_escape_the_session_directory():
    assert session_docs.safe_name("../../etc/passwd") == "passwd"
    assert session_docs.safe_name("men u.pdf") == "men_u.pdf"
    assert session_docs.safe_name(".hidden.txt") == "hidden.txt"
    for bad in ("", "..", "/", "   "):
        with pytest.raises(ValueError):
            session_docs.safe_name(bad)


def test_save_rejects_type_size_and_empty(staging):
    with pytest.raises(ValueError, match="unsupported file type"):
        session_docs.save("s1", "notes.exe", b"x")
    with pytest.raises(ValueError, match="empty"):
        session_docs.save("s1", "notes.txt", b"")
    with pytest.raises(ValueError, match="larger than"):
        session_docs.save("s1", "big.txt", b"x" * (session_docs.MAX_BYTES + 1))


# --- staging -----------------------------------------------------------------


def test_save_extracts_text_and_lists_it_as_readable(staging):
    doc = session_docs.save("s1", "menu.txt", b"Tuna roll 12")
    assert doc == {"name": "menu.txt", "size": 12, "readable": True}
    assert session_docs.list_documents("s1") == [doc]
    assert session_docs.names("s1") == ["menu.txt"]
    assert (staging / "session-s1" / "text" / "menu.txt.txt").read_text() == "Tuna roll 12"


def _one_page_pdf(text: str) -> bytes:
    """Smallest PDF that really carries text, so the flagship case (someone's
    menu) is proven rather than assumed."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref,
    )
    return bytes(out)


def test_pdf_text_is_extracted(staging):
    doc = session_docs.save("s1", "menu.pdf", _one_page_pdf("Tuna roll 12"))
    assert doc["readable"] is True
    assert "Tuna roll 12" in (staging / "session-s1" / "text" / "menu.pdf.txt").read_text()


def test_docx_text_is_extracted(staging):
    import docx

    document = docx.Document()
    document.add_paragraph("Tuna roll 12")
    buf = io.BytesIO()
    document.save(buf)

    doc = session_docs.save("s1", "menu.docx", buf.getvalue())
    assert doc["readable"] is True
    assert "Tuna roll 12" in (staging / "session-s1" / "text" / "menu.docx.txt").read_text()


def test_unextractable_file_still_stages_but_is_not_readable(staging):
    """A PDF the parser chokes on still reaches Agent Knowledge — only the
    Builder's ability to read it during the interview is lost."""
    doc = session_docs.save("s1", "menu.pdf", b"not really a pdf")
    assert doc["readable"] is False
    assert session_docs.read_raw("s1", "menu.pdf") == b"not really a pdf"
    assert not (staging / "session-s1" / "text" / "menu.pdf.txt").exists()


def test_delete_removes_both_copies(staging):
    session_docs.save("s1", "menu.txt", b"hi")
    assert session_docs.delete("s1", "menu.txt") is True
    assert session_docs.list_documents("s1") == []
    assert not (staging / "session-s1" / "text" / "menu.txt.txt").exists()
    assert session_docs.delete("s1", "menu.txt") is False


def test_discard_drops_everything(staging):
    session_docs.save("s1", "menu.txt", b"hi")
    session_docs.discard("s1")
    assert session_docs.list_documents("s1") == []


# --- endpoints ---------------------------------------------------------------


def test_upload_list_and_delete_a_staged_document(client, mocks, staging):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}

    r = client.post(
        "/sessions/s1/documents", files={"file": ("menu.txt", b"Tuna roll 12", "text/plain")}
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"name": "menu.txt", "size": 12, "readable": True}

    listed = client.get("/sessions/s1/documents").json()["data"]["documents"]
    assert [d["name"] for d in listed] == ["menu.txt"]

    assert client.delete("/sessions/s1/documents/menu.txt").status_code == 200
    assert client.get("/sessions/s1/documents").json()["data"]["documents"] == []
    assert client.delete("/sessions/s1/documents/menu.txt").status_code == 404


def test_upload_unsupported_type_is_422(client, mocks, staging):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    r = client.post("/sessions/s1/documents", files={"file": ("x.exe", b"x", "application/octet-stream")})
    assert r.status_code == 422


def test_upload_to_unknown_session_404(client, mocks, staging):
    mocks.store.get_session.return_value = None
    r = client.post("/sessions/nope/documents", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 404


# --- what the Builder is told ------------------------------------------------


def test_builder_is_told_about_staged_documents(client, mocks, monkeypatch, staging):
    """The whole point: the server knows what is attached, so this no longer
    depends on the browser remembering to send the names."""
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    session_docs.save("s1", "menu.txt", b"Tuna roll 12")
    fake = FakeStream(ComposeResult(action="ask", question="?"))
    monkeypatch.setattr(sessions, "stream", fake)

    client.post("/sessions/s1/messages", json={"message": "add room service"})
    assert fake.kwargs["knowledge_docs"] == ["menu.txt"]


def test_staged_and_legacy_names_are_merged_without_duplicates(client, mocks, monkeypatch, staging):
    mocks.store.get_session.return_value = {"history": [], "config": None, "agent_id": None}
    session_docs.save("s1", "menu.txt", b"x")
    fake = FakeStream(ComposeResult(action="ask", question="?"))
    monkeypatch.setattr(sessions, "stream", fake)

    client.post(
        "/sessions/s1/messages",
        json={"message": "hi", "doc_names": ["menu.txt", "policy.txt"]},
    )
    assert fake.kwargs["knowledge_docs"] == ["menu.txt", "policy.txt"]


# --- the forward at Create ---------------------------------------------------


def _arrange_create(mocks, monkeypatch):
    mocks.store.get_session.return_value = {
        "history": [], "config": VALID_CONFIG, "agent_id": None,
    }
    mocks.registry.next_port.return_value = 9001
    mocks.registry.record_backend.return_value = {"slug": "a", "service_dir": "/srv/a"}
    mocks.client.create_project.return_value = "p1"
    mocks.client.create_api_key.return_value = "tc_key"
    mocks.client.register_webhook.return_value = {"secret": "s" * 16}
    mocks.client.create_agent.return_value = {"id": "a1"}
    mocks.client.list_knowledge_bases.return_value = []
    mocks.client.create_knowledge_base.return_value = {"id": "kb1"}
    # The docker build is not this test's business.
    monkeypatch.setattr(sessions, "spawn", lambda coro: coro.close())


def test_create_forwards_staged_documents_then_discards_them(client, mocks, monkeypatch, staging):
    _arrange_create(mocks, monkeypatch)
    session_docs.save("s1", "menu.txt", b"Tuna roll 12")

    r = client.post("/sessions/s1/create")
    assert r.status_code == 200
    assert r.json()["data"]["knowledge"] == {"uploaded": ["menu.txt"], "failed": []}
    mocks.client.upload_document.assert_awaited_once()
    # Delivered, so the staged copy is redundant.
    assert session_docs.list_documents("s1") == []


def test_a_failed_forward_keeps_the_document_staged_for_retry(client, mocks, monkeypatch, staging):
    """The thing the browser could not do: fail, and still have the bytes."""
    _arrange_create(mocks, monkeypatch)
    session_docs.save("s1", "menu.txt", b"Tuna roll 12")
    mocks.client.upload_document.side_effect = RuntimeError("TurnCall said no")

    r = client.post("/sessions/s1/create")
    assert r.status_code == 200  # the agent is created regardless
    assert r.json()["data"]["knowledge"] == {"uploaded": [], "failed": ["menu.txt"]}
    assert session_docs.names("s1") == ["menu.txt"]


def test_create_on_an_existing_agent_retries_the_forward(client, mocks, monkeypatch, staging):
    mocks.store.get_session.return_value = {"history": [], "config": VALID_CONFIG, "agent_id": "a1"}
    mocks.registry.get_backend.return_value = {
        "port": 9001, "status": "running", "project_id": "p1", "api_key": "tc_key",
    }
    mocks.client.list_knowledge_bases.return_value = [
        {"id": "kb1", "name": "builder-knowledge"}
    ]
    session_docs.save("s1", "menu.txt", b"Tuna roll 12")

    r = client.post("/sessions/s1/create")
    assert r.json()["data"]["knowledge"] == {"uploaded": ["menu.txt"], "failed": []}
    assert session_docs.list_documents("s1") == []
