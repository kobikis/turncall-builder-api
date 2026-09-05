"""GitHub connections and pushing an Agent Backend (ADR-0013).

Covers the parts that must be right without a network: the token never lands in
the database readable, the divergence hash actually detects an upstream edit,
and secrets never enter the pushed file set.
"""

import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

from app import github  # noqa: E402


@pytest.mark.unit
class TestTokenStorage:
    def test_roundtrip_and_ciphertext_hides_the_token(self):
        c = github.encrypt_token("github_pat_secret_value")
        assert "github_pat_secret_value" not in c
        assert github.decrypt_token(c) == "github_pat_secret_value"

    def test_same_token_encrypts_differently_each_time(self):
        """Fernet includes a nonce; identical tokens must not collide in the DB."""
        assert github.encrypt_token("t") != github.encrypt_token("t")

    def test_a_changed_key_fails_loudly_rather_than_returning_junk(self, monkeypatch):
        c = github.encrypt_token("tok")
        monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
        with pytest.raises(github.GitHubError, match="has changed"):
            github.decrypt_token(c)

    def test_missing_key_explains_how_to_make_one(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN_ENCRYPTION_KEY", raising=False)
        with pytest.raises(github.GitHubError, match="Fernet.generate_key"):
            github.encrypt_token("tok")


@pytest.mark.unit
class TestDivergenceHash:
    def test_order_independent(self):
        assert github.tree_hash({"a": b"1", "b": b"2"}) == github.tree_hash(
            {"b": b"2", "a": b"1"}
        )

    def test_detects_an_edited_file(self):
        assert github.tree_hash({"app.py": b"x"}) != github.tree_hash({"app.py": b"y"})

    def test_detects_an_added_or_removed_file(self):
        base = {"app.py": b"x"}
        assert github.tree_hash(base) != github.tree_hash({**base, "new.py": b""})

    def test_detects_a_rename_with_identical_content(self):
        """Path is hashed, not just content — a moved file is a change."""
        assert github.tree_hash({"a.py": b"x"}) != github.tree_hash({"b.py": b"x"})


@pytest.mark.unit
class TestWhatGetsPushed:
    def test_secrets_and_local_state_never_leave(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hi')")
        (tmp_path / ".env").write_text("WEBHOOK_SECRET=supersecret")
        (tmp_path / ".env.example").write_text("WEBHOOK_SECRET=")
        (tmp_path / "events.db").write_bytes(b"sqlite")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00")

        files = github._collect(str(tmp_path))

        assert "app.py" in files
        # .env.example is the whole point of the previous PR — it must ship.
        assert ".env.example" in files
        assert ".env" not in files
        assert "events.db" not in files
        assert not any(f.startswith(".git/") for f in files)
        assert not any("__pycache__" in f for f in files)
        assert b"supersecret" not in b"".join(files.values())

    def test_nested_files_keep_their_relative_paths(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "mod.py").write_text("x = 1")
        assert "sub/mod.py" in github._collect(str(tmp_path))
