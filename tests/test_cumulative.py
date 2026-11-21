from siat.load import cumulative_likely


def q(year, vals):
    return [("Q", f"{year}-Q{i + 1}", v) for i, v in enumerate(vals)]


def test_cumulative_quarterly_gdp_like():
    s = q(2016, [47816, 114568, 185109, 290213]) + q(2017, [72109, 176936, 295641, 400000])
    assert cumulative_likely(s)


def test_growth_rates_are_not_cumulative():
    s = q(2016, [112.1, 110.4, 110.7, 107.7]) + q(2017, [104.1, 103.1, 100.9, 101.0])
    assert not cumulative_likely(s)


def test_annual_series_never_flagged():
    assert not cumulative_likely([("Y", str(y), float(y)) for y in range(2010, 2025)])
