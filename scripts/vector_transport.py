"""Bounded float32 transport for derived vectors, never model-facing evidence."""
import base64
import math
import struct


def pack_vector(values):
    if not isinstance(values, (list, tuple)) or not 32 <= len(values) <= 1536:
        raise ValueError("Vector dimension bound")
    if any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e6 for value in values):
        raise ValueError("Invalid vector coordinate")
    raw = struct.pack("<" + "f" * len(values), *values)
    return {"encoding": "f32le-base64", "data": base64.b64encode(raw).decode("ascii")}
