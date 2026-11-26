"""The cache row codec stores plain data and nothing else."""

from __future__ import annotations

import json
import pickle
import random
import zlib

import pytest

from emend import cache_codec


@pytest.mark.parametrize("value", [
    None, True, False, 0, -1, 2**63 - 1, -(2**63), 1.5, -0.0, "",
    "héllo ☃ \U0001f600 \"quoted\" \\ \n\t\x00",
    [], {}, [1, [2, [3, {"a": None}]]],
    {"rows": [[1, 2.5, "x", True, None]], "nested": {"key": [1]}},
])
def test_codec_round_trips_plain_data(value):
    restored = cache_codec.decode(cache_codec.encode(value))
    assert restored == value
    assert type(restored) is type(value)


def test_codec_stores_tuples_as_lists():
    assert cache_codec.decode(cache_codec.encode((1, ("a", 2)))) == [1, ["a", 2]]


def test_codec_keeps_floats_exact():
    generator = random.Random(1)
    values = [
        generator.random() * 10 ** generator.randint(-300, 300) for _ in range(20_000)
    ]
    assert cache_codec.decode(cache_codec.encode(values)) == values


def test_codec_output_is_compressed_standard_json():
    value = {"names": ["a.b", "a.c"], "count": 2}
    assert json.loads(zlib.decompress(cache_codec.encode(value))) == value


@pytest.mark.parametrize("payload", [
    b"",
    b"not compressed",
    zlib.compress(b""),
    zlib.compress(b"{"),
    zlib.compress(b"[1] trailing"),
    zlib.compress(pickle.dumps({"a": 1})),
    "text",
    None,
])
def test_codec_rejects_rows_it_did_not_write(payload):
    with pytest.raises(ValueError):
        cache_codec.decode(payload)
