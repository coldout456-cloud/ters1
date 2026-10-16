"""Хэширование пароля и двухфакторная проверка (TOTP) — всё из стандартной
библиотеки, без лишних зависимостей.

Сгенерировать хэш пароля для .env:   python security.py
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import re
import secrets
import struct
import time

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt:{_N}:{_R}:{_P}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split(":")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238) — код из Google Authenticator / Authy и т.п.
# --------------------------------------------------------------------------- #

def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    padded = secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, period: int = 30, window: int = 1) -> bool:
    """window=1 — принимаем код и соседнего 30-секундного интервала (разница часов)."""
    if not secret_b32 or not re.fullmatch(r"\d{6}", code or ""):
        return False
    counter = int(time.time()) // period
    try:
        return any(
            hmac.compare_digest(_hotp(secret_b32, counter + drift), code)
            for drift in range(-window, window + 1)
        )
    except Exception:
        return False


if __name__ == "__main__":
    pw = getpass.getpass("Новый пароль: ")
    if len(pw) < 12:
        raise SystemExit("Пароль короче 12 символов — придумай подлиннее.")
    if getpass.getpass("Ещё раз: ") != pw:
        raise SystemExit("Пароли не совпали.")
    print("\nВставь в server/.env:\n")
    print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")
