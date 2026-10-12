"""Send a notification to every subscriber, in batches."""

import asyncio


async def send(address: str, text: str) -> None:
    await asyncio.sleep(0)  # stands in for the network call


async def notify_all(subscribers: dict[str, str], text: str) -> int:
    sent = 0
    for name, address in subscribers.items():
        if not address:
            subscribers.pop(name)
            continue
        send(address, f"Hi {name}, {text}")
        sent += 1
    return sent


def batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items) - size, size)]


def summary(sent: int, total: int) -> str:
    return "sent {sent} of {total} notifications"
