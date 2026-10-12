from stock.levels import parse_sku, reorder_point


def test_reorder_point_covers_the_lead_time():
    assert reorder_point(5, 3) == 15


def test_parse_sku():
    assert parse_sku("AB-1234") == ("AB", 1234)
