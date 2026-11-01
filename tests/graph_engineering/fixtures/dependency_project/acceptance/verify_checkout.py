from pricing import discount


def test_large_order_discount():
    assert discount(100) == 10
