import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase ASCII letters and digits, with runs of anything else as single hyphens."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def title_to_path(title: str) -> str:
    return f"/posts/{slugify(title)}"
