from environment import currency


def test_test_currency():
    assert currency() == "ILS"
