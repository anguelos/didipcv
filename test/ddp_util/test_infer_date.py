import pytest
import json
from pathlib import Path
import ddp_util


p = Path(__file__).with_name('infer_date_data.json')
positive_examples = json.load(open(p, "r"))


@pytest.mark.parametrize("date_str, expected", positive_examples)
def test_infer_date(date_str, expected):
    assert ddp_util.infer_date(date_str) == tuple(expected)


# TODO: Add negative examples.