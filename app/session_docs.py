"""Documents staged on a [[Session]] during Build mode.

Files attached before an Agent exists used to live in the browser (`queuedDocs`
in the console) until [[Create]]. They live here instead, and the server owns
them for the whole interview. Three things follow from that:

- The bytes exist server-side during the interview, which is what lets the
  Builder read them at all (ADR-0014). This is the point; the rest follows.
- The forward into [[Agent Knowledge]] at Create is server-side, so a failure is
  retryable. The browser could only report it and tell the user to redo the
  upload by hand from the Knowledge tab.
- The Builder is told what is attached without depending on the console to keep
  sending the filenames on every turn.

Two directories per session: `raw/` is forwarded to TurnCall verbatim, `text/`
is the extracted plain text and holds one `.txt` per staged file.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
from typing import Any

logger = logging.getLogger("turncall_builder")

# Same volume the generated Agent Backends live on, so staged files survive a
# container restart without any new infrastructure.
GENERATED_DIR = os.environ.get("GENERATED_DIR", "/generated")

MAX_BYTES = 10 * 1024 * 1024

# Mirrors what TurnCall's ingestion accepts, so anything staged here can always
# be forwarded to Agent Knowledge at Create.
ALLOWED_SUFFIXES = frozenset(
    {".pdf", ".txt", ".md", ".docx", ".csv", ".json", ".yaml", ".yml", ".xml", ".tsv"}
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(filename: str) -> str:
    """A filename that cannot escape its session directory.

    This is a trust boundary: the name is user-supplied and the file lands on a
    volume shared with every generated backend. Take the basename, collapse
    everything outside a conservative set, and refuse what reduces to nothing or
    to a dot-file.
    """
    cleaned = _UNSAFE.sub("_", os.path.basename(filename or "").strip())
    cleaned = cleaned.lstrip(".")[:120]
    if not cleaned.strip("_"):
        raise ValueError("unusable filename")
    return cleaned


def session_dir(sid: str) -> str:
    return os.path.join(GENERATED_DIR, f"session-{safe_name(sid)}")


def raw_dir(sid: str) -> str:
    return os.path.join(session_dir(sid), "raw")


def text_dir(sid: str) -> str:
    return os.path.join(session_dir(sid), "text")


def extract_text(name: str, data: bytes) -> str | None:
    """Plain text for the Builder to read, or None if it can't be had.

    Best-effort by design. A file that won't extract still stages and still
    reaches Agent Knowledge, where TurnCall's own ingestion handles it — all
    that's lost is the Builder reading it during the interview, and the console
    reports that per file rather than failing the upload.
    """
    suffix = os.path.splitext(name)[1].lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
            return "\n\n".join(pages).strip() or None
        if suffix == ".docx":
            import docx

            doc = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs).strip() or None
        return data.decode("utf-8").strip() or None
    except Exception:  # noqa: BLE001 - any parser failure degrades to "unreadable"
        logger.warning("could not extract text from %s", name, exc_info=True)
        return None


def save(sid: str, filename: str, data: bytes) -> dict[str, Any]:
    """Stage one document. Raises ValueError for anything the caller should
    report as a 422 — size, type and name are all validated here, at the edge."""
    name = safe_name(filename)
    if os.path.splitext(name)[1].lower() not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"{name}: unsupported file type — "
            f"{', '.join(sorted(ALLOWED_SUFFIXES))} only"
        )
    if not data:
        raise ValueError(f"{name} is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name} is larger than {MAX_BYTES // (1024 * 1024)}MB")

    os.makedirs(raw_dir(sid), exist_ok=True)
    os.makedirs(text_dir(sid), exist_ok=True)
    with open(os.path.join(raw_dir(sid), name), "wb") as fh:
        fh.write(data)

    text = extract_text(name, data)
    text_path = os.path.join(text_dir(sid), f"{name}.txt")
    if text is None:
        # A re-upload of a now-unreadable file must not leave the old text behind.
        if os.path.exists(text_path):
            os.remove(text_path)
    else:
        with open(text_path, "w") as fh:
            fh.write(text)
    return {"name": name, "size": len(data), "readable": text is not None}


def list_documents(sid: str) -> list[dict[str, Any]]:
    """What this session has staged. `readable` is whether the Builder can read
    it — the console surfaces it, because "attached" and "understood" differ."""
    directory = raw_dir(sid)
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        out.append(
            {
                "name": name,
                "size": os.path.getsize(path),
                "readable": os.path.isfile(os.path.join(text_dir(sid), f"{name}.txt")),
            }
        )
    return out


def names(sid: str) -> list[str]:
    return [d["name"] for d in list_documents(sid)]


def read_raw(sid: str, name: str) -> bytes:
    with open(os.path.join(raw_dir(sid), safe_name(name)), "rb") as fh:
        return fh.read()


def delete(sid: str, name: str) -> bool:
    """Remove one staged document. False if it wasn't there."""
    clean = safe_name(name)
    path = os.path.join(raw_dir(sid), clean)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    text_path = os.path.join(text_dir(sid), f"{clean}.txt")
    if os.path.isfile(text_path):
        os.remove(text_path)
    return True


def discard(sid: str) -> None:
    """Drop everything this session staged — called once its documents have
    reached Agent Knowledge and the copies here are redundant.

    ponytail: a session abandoned before Create keeps its directory. Sweep
    GENERATED_DIR/session-* by mtime if that ever grows enough to matter.
    """
    shutil.rmtree(session_dir(sid), ignore_errors=True)
