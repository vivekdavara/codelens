class Cache:
    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    def get(self, key: str, default: object = None) -> object:
        return self._store.get(key, default)

    def set(self, key: str, value: object) -> None:
        self._store[key] = value
