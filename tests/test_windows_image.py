import hashlib
import math
import unittest

from egis_matcher import windows_image as wi

W, H, N = wi.WIDTH, wi.HEIGHT, wi.FRAME_BYTES


class _Lcg:
    def __init__(self, seed):
        self.state = seed & 0xFFFFFFFF

    def next(self, bound):
        self.state = (self.state * 1103515245 + 12345) & 0xFFFFFFFF
        return (self.state >> 16) % bound


def synthetic_frame(seed):
    """Integer-only ridge pattern with a finger-shaped contact region."""
    rng = _Lcg(seed)
    if seed == 0:
        return bytes(40 + rng.next(6) for _ in range(N))  # untouched sensor
    dx, dy = rng.next(9) - 4, rng.next(9) - 4 or 1
    period = 7 + rng.next(4)
    cx, cy = rng.next(W), rng.next(H)
    radius = 25 + rng.next(80)
    out = bytearray(N)
    for y in range(H):
        for x in range(W):
            if (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
                value = 40 + rng.next(8)
            else:
                phase = (x * dx + y * dy) % period
                value = 60 + 120 * abs(2 * phase - period) // period + rng.next(20)
            out[y * W + x] = value
    return bytes(out)


def synthetic_bad_pixels(seed):
    rng = _Lcg(seed ^ 0x5A5A)
    mask = bytearray(N)
    for _ in range(seed * 7):
        mask[rng.next(N)] = 1
    if seed % 2:
        mask[N - 1] = 1
        mask[0] = 1
    return bytes(mask)


def _digest(data):
    return hashlib.sha256(bytes(data)).hexdigest()[:16]


# Produced by executing the EgisTouchFP0575.dll 3.7.1.1 routines under
# emulation (FUN_1800082d4/FUN_1800095e0, FUN_18000b5d0, FUN_180008010,
# FUN_18000b670, FUN_180005184, FUN_18000c0f8) on the synthetic inputs above.
# Each entry: seed -> (fill, processed digest, (score, corners, edges),
# finished digest before the vertical flip).
DRIVER_VECTORS = {
    0: (11, "d9b715695a0e39ee", (0, 0, 0), "ef367fdb20a51d87"),
    1: (48, "6f4eebd6ee84f459", (0, 1, 1), "91e98b69089a2cc1"),
    2: (85, "4d311c6c7daf5e5c", (256, 4, 8), "0e44fc27b9d46cd3"),
    3: (122, "4e54995e44090778", (0, 2, 4), "2b3f2da052c6ef0b"),
    4: (159, "357493d9c69022ab", (160, 2, 4), "ed6748fe3fc89619"),
    5: (196, "201515f240df116b", (148, 0, 2), "9c6dec4c0a123901"),
    6: (233, "ff94a8844eeea2bd", (0, 1, 1), "29cddc9fcdbc07ae"),
    7: (14, "5bf76bb0dd97c027", (256, 2, 6), "205744ac7c7194d4"),
}


class DriverEquivalenceTests(unittest.TestCase):
    def test_chain_matches_emulated_driver(self):
        self.assertTrue(DRIVER_VECTORS)
        for seed, (fill, processed, metrics, finished) in DRIVER_VECTORS.items():
            with self.subTest(seed=seed):
                bad = synthetic_bad_pixels(seed)
                image, *quality = wi.process_frame(synthetic_frame(seed), bad, fill)
                self.assertEqual(_digest(image), processed)
                self.assertEqual(tuple(quality), metrics)
                final = wi.repair_bad_pixels(wi.normalize(image), bad)
                self.assertEqual(_digest(final), finished)


class StageTests(unittest.TestCase):
    def test_subtract_minimum_fills_bad_pixels_and_wraps(self):
        frame = bytearray([50]) * N
        frame[1] = 20
        bad = bytearray(N)
        bad[0] = 1
        image = wi.subtract_minimum(frame, bad, 10)
        self.assertEqual(image[1], 0)
        self.assertEqual(image[2], 30)
        self.assertEqual(image[0], (11 - 20) & 0xFF)

    def test_invert(self):
        self.assertEqual(wi.invert(bytes([0, 1, 255])), bytes([255, 254, 0]))

    def test_flip_vertical_reverses_rows(self):
        image = bytes(row for row in range(H) for _ in range(W))
        flipped = wi.flip_vertical(image)
        self.assertEqual(flipped[:W], bytes([H - 1]) * W)
        self.assertEqual(flipped[-W:], bytes(W))

    def test_repair_weight_rows_match_driver_table(self):
        self.assertEqual(wi._REPAIR_WEIGHTS[0], (0, 0, 0, 16, 16, 0, 0, 0))
        self.assertEqual(wi._REPAIR_WEIGHTS[1], (8, 0, 0, 8, 8, 0, 0, 8))
        self.assertEqual(wi._REPAIR_WEIGHTS[2], (16, 0, 0, 0, 0, 0, 0, 16))
        self.assertEqual(wi._REPAIR_WEIGHTS[4], (0, 16, 0, 0, 0, 0, 16, 0))
        self.assertEqual(wi._REPAIR_WEIGHTS[6], (0, 0, 16, 0, 0, 16, 0, 0))
        self.assertEqual(wi._REPAIR_WEIGHTS[7], (0, 0, 8, 8, 8, 8, 0, 0))

    def test_half_angle_table_endpoints(self):
        self.assertEqual(wi._HALF_ANGLE[:4], (0, 2, 5, 7))
        self.assertEqual(wi._HALF_ANGLE[128], 240)

    def test_table_sqrt_is_nearest_integer(self):
        for value in range(0, 0xFE01, 37):
            self.assertLessEqual(abs(wi._table_sqrt(value) - math.sqrt(value)), 0.5)
        self.assertEqual(wi._table_sqrt(0xFE01), 0xFF)

    def test_blank_frame_scores_zero(self):
        _, score, _, _ = wi.process_frame(synthetic_frame(0), bytes(N), 0)
        self.assertLess(score, wi.QUALITY_ACCEPT)

    def test_repair_leaves_clean_image_untouched(self):
        image = synthetic_frame(3)
        self.assertEqual(wi.repair_bad_pixels(image, bytes(N)), image)

    def test_size_is_validated(self):
        with self.assertRaises(ValueError):
            wi.quality(b"\0" * 10)


class FrameSelectorTests(unittest.TestCase):
    def test_keeps_best_and_stops_on_low_quality(self):
        selector = wi.FrameSelector(b"seed", 100, 4)
        self.assertFalse(selector.offer(b"a", 150, 6))
        self.assertTrue(selector.offer(b"b", 40, 8))
        self.assertEqual((bytes(selector.image), selector.score, selector.edges), (b"a", 150, 6))
        self.assertEqual(selector.flags, wi.FrameSelector.PARTIAL_EDGES)

    def test_requires_score_and_edges_to_improve(self):
        selector = wi.FrameSelector(b"seed", 100, 4)
        selector.offer(b"a", 200, 3)
        self.assertEqual(bytes(selector.image), b"seed")

    def test_stops_when_score_stabilises_with_full_edges(self):
        selector = wi.FrameSelector(b"seed", 100, 8, previous_score=100)
        self.assertFalse(selector.offer(b"a", 200, 8))
        self.assertTrue(selector.offer(b"b", 205, 8))
        self.assertEqual(selector.flags, 0)

    def test_stops_after_ten_frames(self):
        selector = wi.FrameSelector(b"seed", 100, 0)
        results = [selector.offer(b"x", 100 + 20 * index, 0) for index in range(10)]
        self.assertEqual(results, [False] * 9 + [True])
        self.assertEqual(selector.flags, wi.FrameSelector.LOW_EDGES)


if __name__ == "__main__":
    unittest.main()
