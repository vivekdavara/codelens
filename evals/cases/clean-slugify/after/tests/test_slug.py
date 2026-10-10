from text.slug import slugify, title_to_path


def test_slugify_collapses_runs_and_trims():
    assert slugify("  Hello, World!  ") == "hello-world"


def test_title_to_path():
    assert title_to_path("Release 2.0") == "/posts/release-2-0"
