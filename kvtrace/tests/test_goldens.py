"""With no phase 1 flag, outputs equal the goldens exactly."""

import pytest
from golden_cases import CASES, load


@pytest.mark.parametrize("name", sorted(CASES))
def test_output_equals_golden(name):
    assert CASES[name]() == load(name)
