"""CodeLens's own AST rules: each one fires on the mistake and stays quiet on the idioms that look like it."""

from textwrap import dedent

import pytest

from codelens.rules import check


def hits(source: str, code: str | None = None) -> list[tuple[int, str]]:
    return [(h.line, h.code) for h in check(dedent(source)) if code is None or h.code == code]


def test_a_file_that_does_not_parse_has_no_hits() -> None:
    assert check("def f(:\n    pass\n") == []
    assert check("x = 1\0\n") == []


# CL001 unawaited coroutine ----------------------------------------------------------------------------------


def test_a_coroutine_called_as_a_statement_without_await() -> None:
    source = """
        async def save(order):
            return order

        async def handler(order):
            save(order)
            await save(order)
            task = save(order)
            await task
    """
    assert hits(source, "CL001") == [(6, "CL001")]


def test_a_method_called_through_self_without_await() -> None:
    source = """
        class Repo:
            async def flush(self):
                pass

            def close(self):
                self.flush()

            @classmethod
            async def make(cls):
                cls.make()
    """
    assert hits(source, "CL001") == [(7, "CL001"), (11, "CL001")]


def test_a_coroutine_called_at_the_top_level_of_a_script() -> None:
    assert hits("async def main():\n    pass\n\nmain()\n", "CL001") == [(4, "CL001")]


@pytest.mark.parametrize(
    "source",
    [
        # The name is rebound by something else at the top level.
        "async def save():\n    pass\n\nsave = make_sync(save)\n\nsave()\n",
        "async def save():\n    pass\n\ndef save():\n    pass\n\nsave()\n",
        "async def save():\n    pass\n\nfrom other import save\n\nsave()\n",
        # Shadowed by a parameter or a local in the calling function.
        "async def save():\n    pass\n\ndef f(save):\n    save()\n",
        "async def save():\n    pass\n\ndef f():\n    save = print\n    save()\n",
        "async def save():\n    pass\n\ndef f():\n    global save\n    save()\n",
        # A decorator may turn it into something that isn't a coroutine function.
        "@syncify\nasync def save():\n    pass\n\nsave()\n",
        # Passed somewhere that runs it.
        "async def main():\n    pass\n\nasyncio.run(main())\n",
        # Unknown functions and other objects' methods are none of its business.
        "def f(other):\n    other.flush()\n    unknown()\n",
        # A sync method of the same name in the class wins.
        "class A:\n    async def f(self):\n        pass\n\n    def f(self):\n        pass\n\n"
        "    def g(self):\n        self.f()\n",
    ],
)
def test_calls_that_are_not_an_unawaited_coroutine(source: str) -> None:
    assert hits(source, "CL001") == []


# CL002 unreachable code -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("def f(db):\n    return db.order\n    db.commit()\n", 3),
        ("def f():\n    raise ValueError\n    cleanup()\n", 3),
        ("for x in xs:\n    continue\n    use(x)\n", 3),
        ("while True:\n    break\n    use()\n", 3),
        ("try:\n    pass\nexcept E:\n    raise\n    log()\n", 5),
        ("match x:\n    case 1:\n        return 1\n        log()\n", 4),
    ],
)
def test_a_statement_after_an_exit_in_the_same_block(source: str, line: int) -> None:
    assert hits(source, "CL002") == [(line, "CL002")]


def test_only_the_first_unreachable_statement_is_reported() -> None:
    assert hits("def f():\n    return 1\n    a()\n    b()\n", "CL002") == [(3, "CL002")]


def test_exits_in_one_branch_do_not_make_the_next_statement_unreachable() -> None:
    source = """
        def f(x):
            if x:
                return 1
            else:
                raise ValueError
            return 2
    """
    assert hits(source, "CL002") == []  # a rule about blocks, not control flow: quiet when unsure


# CL003 loop mutation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("for x in items:\n    if x < 0:\n        items.remove(x)\n", 3),
        ("for x in items:\n    items.append(x * 2)\n", 2),
        ("for x in items:\n    items += [x]\n", 2),
        ("for k in d:\n    del d[k]\n", 2),
        ("for k in d.keys():\n    d.pop(k)\n", 2),
        ("for k, v in d.items():\n    d.setdefault(v, k)\n", 2),
        ("for i, x in enumerate(items):\n    items.pop(i)\n", 2),
        ("for x in self.pending:\n    self.pending.discard(x)\n", 2),
        ("for x in items:\n    try:\n        y = items.pop()\n    except IndexError:\n        pass\n", 3),
        ("for x in items:\n    with lock:\n        items.clear()\n", 3),
    ],
)
def test_changing_the_collection_being_looped_over(source: str, line: int) -> None:
    assert hits(source, "CL003") == [(line, "CL003")]


@pytest.mark.parametrize(
    "source",
    [
        # A copy is the fix.
        "for x in list(items):\n    items.remove(x)\n",
        "for x in items[:]:\n    items.remove(x)\n",
        "for k in list(d):\n    del d[k]\n",
        # Changing it and then leaving the loop at once is safe.
        "for x in items:\n    if x == target:\n        items.remove(x)\n        break\n",
        "for x in items:\n    items.remove(x)\n    return x\n",
        # Another collection, rebinding the name, or replacing an item.
        "for x in items:\n    seen.add(x)\n",
        "for x in items:\n    items = [y for y in items if y != x]\n",
        "for i, x in enumerate(items):\n    items[i] = x * 2\n",
        # A function defined in the loop runs later.
        "for x in items:\n    def later():\n        items.remove(x)\n",
        "for x in items:\n    callbacks.append(lambda: items.remove(x))\n",
    ],
)
def test_loops_that_do_not_change_what_they_iterate(source: str) -> None:
    assert hits(source, "CL003") == []


def test_the_message_shows_the_change() -> None:
    (hit,) = check("for x in items:\n    items += [x]\n")
    assert hit.message == "`items += [x]` changes `items` while the loop is iterating over it"
    (hit,) = check("for x in items:\n    items += [" + "x, " * 30 + "]\n")
    assert hit.message.startswith("`items += [x, x,") and "…`" in hit.message


def test_a_change_inside_a_match_case_or_a_handler() -> None:
    source = """
        for x in items:
            match x:
                case 0:
                    items.remove(x)
            try:
                pass
            finally:
                del items[0]
    """
    assert hits(source, "CL003") == [(5, "CL003"), (9, "CL003")]


@pytest.mark.parametrize(
    "call",
    [
        "self.clear(cookie.domain, cookie.path, cookie.name)",  # CookieJar.clear, not list.clear
        "items.append(x, y)",
        "items.pop(1, 2, 3)",
        "items.remove(x, key=1)",
    ],
)
def test_calls_that_cannot_be_a_built_in_collection_method(call: str) -> None:
    subject = call.split(".")[0]
    assert hits(f"for x in {subject}:\n    {call}\n", "CL003") == []


def test_dict_update_with_keywords_is_still_a_change() -> None:
    assert hits("for k in d:\n    d.update(extra=1)\n", "CL003") == [(2, "CL003")]


def test_deleting_from_another_collection_in_the_loop() -> None:
    assert hits("for k in d:\n    del other[k], d.attr\n", "CL003") == []


def test_a_nested_loop_over_the_same_list_reports_the_line_once() -> None:
    source = "for x in items:\n    for y in items:\n        items.remove(y)\n"
    assert hits(source, "CL003") == [(3, "CL003")]


# CL004 missing f prefix -------------------------------------------------------------------------------------


def test_a_string_naming_local_variables_without_an_f_prefix() -> None:
    source = """
        def receipt(order, total):
            note = "order {order.id}: {total:.2f} ({total!r})"
            return note
    """
    assert hits(source, "CL004") == [(3, "CL004")]


@pytest.mark.parametrize(
    "source",
    [
        'def f(total):\n    return f"{total}"\n',
        'def f(total):\n    return "{total}".format(total=total)\n',
        'def f(total):\n    return _("{total}").format(total=total)\n',
        'def f(total):\n    return "{total}".format_map(locals())\n',
        'def f(total):\n    logger.info("total {total}", total=total)\n',
        # Escaped braces, names that aren't local, and module-level templates.
        'def f(total):\n    return "{{total}}"\n',
        'def f():\n    return "{total}"\n',
        'TEMPLATE = "{total}"\n',
        # Docstrings explain placeholders.
        'def f(total):\n    """Formats {total} for display."""\n',
        # Not every placeholder is a local: probably filled in somewhere else.
        'def f(total):\n    return "{total} {currency}"\n',
        # A token searched for or replaced, not text: CodeLens's own prompts.py does this.
        'def f(text, total):\n    return text.replace("{total}", str(total))\n',
        'def f(text, total):\n    return "{total}" in text\n',
    ],
)
def test_strings_that_are_not_a_missing_f_prefix(source: str) -> None:
    assert hits(source, "CL004") == []


def test_every_hit_has_a_message_naming_what_it_saw() -> None:
    source = """
        async def go():
            pass

        def f(items, n):
            go()
            for x in items:
                items.pop()
            s = "{n}"
            return s
            s = 1
    """
    source = dedent(source)
    messages = {h.code: h.message for h in check(source)}
    assert "`go()`" in messages["CL001"]
    assert "`return`" in messages["CL002"]
    assert "`items.pop()`" in messages["CL003"]
    assert "`{n}`" in messages["CL004"]
