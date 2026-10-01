"""One Fernet helper for every secret the Builder stores encrypted (#54).

Each caller keeps its own environment variable and its own key — nothing is
shared, renamed or defaulted — and its own error type and wording, so a missing
or rotated key reads exactly as it did before the extraction.
"""

import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class SecretBox:
    env: str  # the environment variable holding this box's Fernet key
    error: type[Exception]
    disabled: str  # what stops working without the key, e.g. "integrations are disabled"
    changed: str  # the full message when a stored value no longer decrypts
    # Used when `env` is unset, so a fresh deployment needs one key rather than
    # one per feature. Never consulted while `env` is set, so a deployment that
    # already has its own key keeps reading what that key sealed.
    fallback_env: str | None = None

    def _fernet(self) -> Fernet:
        key = os.environ.get(self.env, "") or (
            os.environ.get(self.fallback_env, "") if self.fallback_env else ""
        )
        if not key:
            raise self.error(
                f"{self.env} is not set — {self.disabled}. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise self.error(
                f"{self.env} is not a valid Fernet key (44 url-safe base64 characters)."
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet().encrypt(value.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Raises if the key changed — rotating it orphans every stored value."""
        try:
            return self._fernet().decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise self.error(self.changed) from exc
