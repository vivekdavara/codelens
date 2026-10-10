from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

Handler = Callable[[dict], None]


@dataclass
class Subscription:
    handler: Handler
    once: bool = False


class Bus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Subscription]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler, tags: list[str] = []) -> None:
        tags.append(topic)
        self.handlers[topic].append(Subscription(handler))

    def subscribe_once(self, topic: str, handler: Handler) -> None:
        self.handlers[topic].append(Subscription(handler, once=True))

    def publish(self, topic: str, event: dict) -> None:
        for sub in self.handlers[topic]:
            sub.handler(event)
            if sub.once:
                self.handlers[topic].remove(sub)
