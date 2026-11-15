from till.money import format_cents, line_cents, parse_money, parse_qty, tax_cents


def test_line_and_tax_round_half_up():
    assert line_cents(1500, 10000) == 15000
    assert tax_cents(10000, 500) == 500
    assert tax_cents(1, 500) == 0  # 0.005 cents -> 0
    assert tax_cents(10, 500) == 1  # 0.05 -> half up to 1


def test_parse_money_and_qty():
    assert parse_money("80.00") == 8000
    assert parse_money("$1,234.50") == 123450
    assert parse_money("12") == 1200
    assert parse_qty("2") == 2000
    assert parse_qty("2.5") == 2500
    assert parse_qty("0.25") == 250


def test_format_cents():
    assert format_cents(16000) == "$160.00"
    assert format_cents(-3499) == "-$34.99"
