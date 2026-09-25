"""Credential protection: Fernet encryption at rest, masking on screen, never in prompts or logs."""
from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class CredentialError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise CredentialError("FERNET_KEY is not set; refusing to store credentials unencrypted.")
    return Fernet(key.encode())


def encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode() if secret else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialError("Stored credential could not be decrypted (wrong FERNET_KEY?).") from exc


def mask(secret: str) -> str:
    return "" if not secret else "•" * 8


def scrub(text: str, *secrets: str) -> str:
    """Remove secrets from any string before it is logged or sent to an LLM."""
    for s in secrets:
        if s:
            text = text.replace(s, "••••••••")
    return text
