import importlib


def tax_rate(country):
    return importlib.import_module(f"plugins.{country.lower()}").RATE
