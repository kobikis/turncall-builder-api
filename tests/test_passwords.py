"""Real argon2id hash/verify — the one security path that must actually work.
No mocks fixture here, so these call the genuine PasswordHasher."""

from app import auth_store


def test_hash_is_argon2id_and_not_plaintext():
    h = auth_store.hash_password("hunter2xx")
    assert h.startswith("$argon2id$")
    assert "hunter2xx" not in h


def test_verify_accepts_correct_and_rejects_wrong():
    h = auth_store.hash_password("hunter2xx")
    assert auth_store.verify_password(h, "hunter2xx") is True
    assert auth_store.verify_password(h, "wrongpass") is False


def test_hashes_are_salted_unique():
    assert auth_store.hash_password("same") != auth_store.hash_password("same")


def test_verify_rejects_malformed_hash_without_raising():
    # A garbage stored hash must fail closed (False), not raise a 500.
    assert auth_store.verify_password("not-a-valid-argon2-hash", "whatever") is False
