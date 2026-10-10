import json
import os


def expected_token() -> str:
    return os.environ["WEBHOOK_TOKEN"]


def handle(body: bytes, token: str) -> dict:
    if token == expected_token():
        event = json.loads(body)
        return {"ok": True, "type": event.get("type")}
    return {"ok": False}
