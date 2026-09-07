"""Bounded-shape data codec; no object loading, references, pickle or evaluation."""

import base64
import math


def plain(value, depth=0):
    if depth > 32:
        return False
    if type(value) in (str, int, bool, type(None)):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(plain(v, depth + 1) for v in value)
    if type(value) is dict:
        return all(type(k) is str and plain(v, depth + 1) for k, v in value.items())
    return False


def pack(value, depth=0):
    if depth > 32:
        raise ValueError("data depth exceeded")
    if plain(value, depth):
        return {"kind": "json", "value": value}
    if type(value) is bytes:
        return {"kind": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if type(value) is list:
        return {"kind": "list", "value": [pack(v, depth + 1) for v in value]}
    if type(value) is tuple:
        return {"kind": "tuple", "value": [pack(v, depth + 1) for v in value]}
    if type(value) is dict and all(type(k) is str for k in value):
        return {
            "kind": "dict",
            "value": {k: pack(v, depth + 1) for k, v in value.items()},
        }
    raise ValueError("unsupported data type")


def unpack(node, depth=0):
    if depth > 32 or type(node) is not dict or set(node) != {"kind", "value"}:
        raise ValueError("invalid data envelope")
    kind, value = node["kind"], node["value"]
    if kind == "json" and plain(value, depth):
        return value
    if kind == "bytes" and type(value) is str:
        return base64.b64decode(value, validate=True)
    if kind == "list" and type(value) is list:
        return [unpack(v, depth + 1) for v in value]
    if kind == "tuple" and type(value) is list:
        return tuple(unpack(v, depth + 1) for v in value)
    if kind == "dict" and type(value) is dict and all(type(k) is str for k in value):
        return {k: unpack(v, depth + 1) for k, v in value.items()}
    raise ValueError("invalid data type")
