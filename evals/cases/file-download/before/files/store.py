import os

UPLOAD_DIR = "/srv/uploads"


def size_of(name: str) -> int:
    return os.path.getsize(os.path.join(UPLOAD_DIR, os.path.basename(name)))
