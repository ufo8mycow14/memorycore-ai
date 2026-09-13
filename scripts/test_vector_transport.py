"""Float32 coordinates survive compact transport without decimal inflation."""
import base64
import json
import struct
import unittest
from scripts.vector_transport import pack_vector


class VectorTransportTests(unittest.TestCase):
    def test_exact_float32_round_trip_and_small_frame(self):
        values = list(struct.unpack("<384f", struct.pack("<384f", *([-0.0123456789] * 384))))
        packed = pack_vector(values)
        decoded = struct.unpack("<384f", base64.b64decode(packed["data"], validate=True))
        self.assertEqual(list(decoded), values)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(values)) / 3)
        self.assertEqual(len(json.dumps([packed] * 8)) < 20000, True)

    def test_invalid_coordinates_and_dimensions_rejected(self):
        for values in ([], [1.0] * 31, [1.0] * 1537, [True] * 64,
                       [float("nan")] * 64, [float("inf")] * 64, [1e7] * 64):
            with self.assertRaises(ValueError):
                pack_vector(values)
