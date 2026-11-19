import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        rounds = int(iterations)
        salt_bytes = bytes.fromhex(salt)
    except ValueError:
        return False
    if algorithm != _ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, rounds)
    return hmac.compare_digest(digest.hex(), expected)


# Checked against when the email is unknown, so a failed login costs the same
# time whether or not the account exists.
DUMMY_HASH = hash_password(secrets.token_hex(16))
