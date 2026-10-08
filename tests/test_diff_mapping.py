from codelens.diff import FileDiff, LineKind, Side, parse_patch

TEXT = """\
diff --git a/svc/pay.py b/svc/pay.py
--- a/svc/pay.py
+++ b/svc/pay.py
@@ -3,6 +3,8 @@ def refund(amount, fee):
     if amount <= 0:
         raise ValueError("amount")
-    net = amount - fee
+    net = amount - fee
+    if net < 0:
+        net = 0
     log(net)
     return net
 
@@ -40,4 +42,3 @@ def charge(card, amount):
     token = card.token
-    audit(token)
-    send(token, amount)
+    send(token, amount, idempotency_key=key)
     return True
"""


def file() -> FileDiff:
    f = parse_patch(TEXT).get("svc/pay.py")
    assert f is not None
    return f


def test_anchor_right_side_added_and_context() -> None:
    f = file()
    added = f.anchor(6)
    assert added is not None and added.kind is LineKind.ADDED and added.content == "    if net < 0:"
    context = f.anchor(3)
    assert context is not None and context.kind is LineKind.CONTEXT and context.old_lineno == 3


def test_anchor_left_side_finds_removed_lines() -> None:
    removed = file().anchor(41, Side.LEFT)
    assert removed is not None and removed.kind is LineKind.REMOVED
    assert removed.content == "    audit(token)"


def test_lines_outside_hunks_do_not_anchor() -> None:
    f = file()
    assert f.anchor(1) is None  # above the first hunk
    assert f.anchor(20) is None  # between hunks
    assert f.anchor(46) is None  # past the end
    assert f.anchor(41, Side.RIGHT) is None  # 41 is not shown on the new side
    assert f.anchor(44, Side.LEFT) is None  # the old side of hunk 2 ends at 43


def test_commentable_lines_per_side() -> None:
    f = file()
    assert f.commentable_lines(Side.RIGHT) == set(range(3, 11)) | {42, 43, 44}
    assert f.commentable_lines(Side.LEFT) == set(range(3, 9)) | {40, 41, 42, 43}


def test_added_lines_and_changed_ranges() -> None:
    f = file()
    assert f.added_lines() == [5, 6, 7, 43]
    assert f.changed_ranges() == [(5, 7), (43, 43)]


def test_pure_deletion_has_no_changed_ranges() -> None:
    text = "--- a/x\n+++ b/x\n@@ -1,2 +1 @@\n keep\n-drop\n"
    f = parse_patch(text).files[0]
    assert f.added_lines() == [] and f.changed_ranges() == []
    assert f.commentable_lines(Side.RIGHT) == {1}


def test_render_numbered_puts_new_line_numbers_in_the_margin() -> None:
    assert file().hunks[1].render_numbered() == (
        "@@ -40,4 +42,3 @@ def charge(card, amount):\n"
        "42      token = card.token\n"
        "   -    audit(token)\n"
        "   -    send(token, amount)\n"
        "43 +    send(token, amount, idempotency_key=key)\n"
        "44      return True"
    )


def test_render_numbered_widens_margin_across_a_digit_boundary() -> None:
    text = "--- a/x\n+++ b/x\n@@ -98,2 +98,3 @@\n a\n+b\n c\n"
    lines = parse_patch(text).files[0].hunks[0].render_numbered().splitlines()
    assert lines[1:] == [" 98  a", " 99 +b", "100  c"]
