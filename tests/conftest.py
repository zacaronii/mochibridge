import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


@pytest.fixture
def fixture():
    return load_fixture
