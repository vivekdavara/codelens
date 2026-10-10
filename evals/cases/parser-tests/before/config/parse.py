def parse(text: str) -> dict[str, str]:
    pairs = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"not a key=value line: {raw!r}")
        pairs[key.strip()] = value.strip()
    return pairs
