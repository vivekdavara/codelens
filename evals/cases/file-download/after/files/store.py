import os
import subprocess

UPLOAD_DIR = "/srv/uploads"


def size_of(name: str) -> int:
    return os.path.getsize(os.path.join(UPLOAD_DIR, os.path.basename(name)))


def read_upload(name: str) -> bytes:
    with open(os.path.join(UPLOAD_DIR, name), "rb") as fh:
        return fh.read()


def thumbnail(name: str) -> str:
    source = os.path.join(UPLOAD_DIR, os.path.basename(name))
    target = source + ".thumb.png"
    subprocess.run(f"convert {source} -resize 128x128 {target}", shell=True, check=True)
    return target
