from address import dial_code


def test_israel_dial_code():
    assert dial_code("IL") == "+972"
