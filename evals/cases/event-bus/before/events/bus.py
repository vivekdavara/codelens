from collections import defaultdict
from collections.abc import Callable

Handler = Callable[[dict], None]


class Bus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self.handlers[topic].append(handler)

    def publish(self, topic: str, event: dict) -> None:
        for handler in self.handlers[topic]:
            handler(event)
