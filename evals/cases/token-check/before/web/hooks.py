import json


def handle(body: bytes) -> dict:
    event = json.loads(body)
    return {"ok": True, "type": event.get("type")}
