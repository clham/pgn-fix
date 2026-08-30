import pathlib

import pytest

DATA_DIR = pathlib.Path(__file__).parent / "data"


@pytest.fixture
def data_dir() -> pathlib.Path:
    return DATA_DIR
