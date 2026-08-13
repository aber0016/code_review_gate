import hashlib


def digest(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
