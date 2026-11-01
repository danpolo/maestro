from plugin_loader import tax_rate


def test_israel_tax_plugin():
    assert tax_rate("IL") == 0.17
