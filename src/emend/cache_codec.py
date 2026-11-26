"""Serialization for rebuildable cache rows.

Cache databases live inside the analysed checkout, so their rows are
untrusted input: a repository can ship a prepared ``.emend/cache``.  Rows are
therefore compressed JSON, which can only ever produce plain data.

JSON is handled by ``msgspec``: it is the fastest native codec that supports
free-threaded interpreters.
"""

from __future__ import annotations

import zlib
from typing import Any

import msgspec

_encode = msgspec.json.Encoder().encode
_decode = msgspec.json.Decoder().decode


def encode(value: Any) -> bytes:
    """Serialize JSON-compatible *value* to a compact cache payload."""
    return zlib.compress(_encode(value), 1)


def decode(payload: bytes) -> Any:
    """Return the value stored by :func:`encode`.

    Raises ``ValueError`` for a payload this module did not write, including
    rows left by earlier cache formats.
    """
    try:
        return _decode(zlib.decompress(payload))
    except (zlib.error, TypeError, msgspec.DecodeError) as error:
        raise ValueError("unreadable cache payload") from error
