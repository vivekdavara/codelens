import time

import requests


def fetch(url: str, timeout: float = 5.0) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def fetch_with_retries(url: str, attempts: int = 3, backoff: float = 0.5) -> bytes | None:
    for attempt in range(attempts):
        try:
            return fetch(url)
        except requests.Timeout:
            continue
            time.sleep(backoff * 2**attempt)
    return None
