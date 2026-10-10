import time

import requests


def fetch(url: str, timeout: float = 5.0) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content
