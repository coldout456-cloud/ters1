"""Хэширование пароля (scrypt из стандартной библиотеки, без лишних зависимостей).

Сгенерировать хэш для .env:   python security.py
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import secrets

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


if __name__ == "__main__":
    pw = getpass.getpass("Новый пароль: ")
    if len(pw) < 12:
        raise SystemExit("Пароль короче 12 символов — придумай подлиннее.")
    if getpass.getpass("Ещё раз: ") != pw:
        raise SystemExit("Пароли не совпали.")
    print("\nВставь в server/.env:\n")
    print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")
