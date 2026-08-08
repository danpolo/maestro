import re

import maestro


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", maestro.__version__)
