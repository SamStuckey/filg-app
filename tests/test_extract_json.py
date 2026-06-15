"""Regression tests for pipeline.extract_json.

This is the function that broke in prod ('list' object has no attribute 'get'):
it used to scan for [...] before {...}, so an object containing an array parsed to
the inner array and callers got a list. These lock the fix.
"""

from pipeline import extract_json


def test_object_with_inner_array_returns_object():
    # THE regression: intake's wedges_considered / vet's scores live inside the object.
    out = extract_json('```json\n{"coherent": true, "thesis": "x", '
                       '"wedges_considered": ["a", "b"], "clarifying_question": null}\n```')
    assert isinstance(out, dict)
    assert out["coherent"] is True and out["wedges_considered"] == ["a", "b"]


def test_nested_object_returns_outer_object():
    out = extract_json('{"verdict": "pursue", "scores": {"demand": 4, "market": 3}}')
    assert isinstance(out, dict) and out["scores"]["demand"] == 4


def test_bare_array_still_parses():
    assert extract_json('[{"text": "x"}, {"text": "y"}]') == [{"text": "x"}, {"text": "y"}]


def test_prose_wrapped_object():
    out = extract_json('Sure! Here is the result:\n{"a": 1}\nHope that helps.')
    assert out == {"a": 1}


def test_array_before_object_in_prose_picks_first_opener():
    # Whichever delimiter OPENS first wins; here the array does.
    assert extract_json('[1, 2] then {"a": 1}') == [1, 2]


def test_garbage_and_empty_return_none():
    assert extract_json("no json here at all") is None
    assert extract_json("") is None
    assert extract_json("{not valid json}") is None
