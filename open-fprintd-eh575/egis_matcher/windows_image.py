"""Bit-exact port of the EH575 (ET510) Windows frame-processing chain.

Every routine names its origin in EgisTouchFP0575.dll 3.7.1.1 (image base
0x180000000); see windows-pipeline.md, "Recovered Frame Processing". The
functions operate on row-major ``bytes``/``bytearray`` images and reproduce
the driver's C integer semantics, including truncating division and uint8
wraparound, so that Linux can produce the image Windows hands to WinBio.
"""

import math

WIDTH = 103
HEIGHT = 52
FRAME_BYTES = WIDTH * HEIGHT
NORMALIZE_TARGET = 0x8C
QUALITY_ACCEPT = 60


def _c_div(numerator, denominator):
    quotient = abs(numerator) // abs(denominator)
    return quotient if (numerator < 0) == (denominator < 0) else -quotient


def _i32(value):
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _check(image, width, height):
    if len(image) != width * height:
        raise ValueError("image size does not match geometry")


# --- stage 1: FUN_1800095e0 / FUN_1800082d4 ---------------------------------

def subtract_minimum(frame, bad_pixels, fill):
    """Fill bad pixels with ``fill + 1`` and subtract the good-pixel minimum.

    ``fill`` is calibration byte 0x14fa. The subtraction wraps in uint8, as
    in the driver.
    """
    _check(frame, WIDTH, HEIGHT)
    _check(bad_pixels, WIDTH, HEIGHT)
    image = bytearray(frame)
    minimum = 0xFF
    for index, flag in enumerate(bad_pixels):
        if flag == 1:
            image[index] = (fill + 1) & 0xFF
        elif image[index] < minimum:
            minimum = image[index]
    return bytearray((value - minimum) & 0xFF for value in image)


# --- stage 2: FUN_18000b5d0 (mode 0) -----------------------------------------

def invert(image):
    return bytearray(255 - value for value in image)


# --- shared primitives --------------------------------------------------------

def _resample_taps(source_length, target_length):
    step = (source_length << 10) // target_length
    taps = []
    for index in range(target_length):
        position = index * step
        first = position >> 10
        boundary = (first + 1) * 0x400 - position
        weights = []
        remaining = step
        while True:
            weight = min(remaining, boundary)
            weights.append(weight)
            remaining -= weight
            boundary = 0x400
            if remaining <= 0:
                break
        taps.append((first, weights))
    return step, taps


def resample(image, width, height, target_width, target_height):
    """Area-weighted resample in 1/1024 fixed point (FUN_180005e70)."""
    _check(image, width, height)
    step, taps = _resample_taps(height, target_height)
    vertical = bytearray(width * target_height)
    for row, (first, weights) in enumerate(taps):
        for column in range(width):
            total = step // 2
            for offset, weight in enumerate(weights):
                total += weight * image[(first + offset) * width + column]
            vertical[row * width + column] = (total // step) & 0xFF
    step, taps = _resample_taps(width, target_width)
    output = bytearray(target_width * target_height)
    for row in range(target_height):
        base = row * width
        for column, (first, weights) in enumerate(taps):
            total = step // 2
            for offset, weight in enumerate(weights):
                total += weight * vertical[base + first + offset]
            output[row * target_width + column] = (total // step) & 0xFF
    return output


def _window_sums(values, width, height, radius):
    """Clipped (2r+1)^2 window sums and sample counts via summed-area table."""
    stride = width + 1
    table = [0] * (stride * (height + 1))
    for y in range(height):
        running = 0
        for x in range(width):
            running += values[y * width + x]
            table[(y + 1) * stride + x + 1] = table[y * stride + x + 1] + running
    sums, counts = [], []
    for y in range(height):
        top, bottom = max(0, y - radius), min(height, y + radius + 1)
        for x in range(width):
            left, right = max(0, x - radius), min(width, x + radius + 1)
            sums.append(table[bottom * stride + right] - table[top * stride + right]
                        - table[bottom * stride + left] + table[top * stride + left])
            counts.append((bottom - top) * (right - left))
    return sums, counts


def box_mean(image, width, height, radius):
    """Edge-clipped box mean (FUN_18001497c for r=1, FUN_180014b1c)."""
    sums, counts = _window_sums(image, width, height, radius)
    return bytearray((total // count) & 0xFF for total, count in zip(sums, counts))


def local_stretch(smoothed, mean, width, height, radius=0x40):
    """Contrast stretch around the local mean ± mean absolute deviation
    (FUN_18001456c)."""
    deviation = [abs(a - b) for a, b in zip(mean, smoothed)]
    sums, counts = _window_sums(deviation, width, height, radius)
    output = bytearray(width * height)
    for index, (total, count) in enumerate(zip(sums, counts)):
        spread = (total // count) & 0xFF
        centre = mean[index]
        value = smoothed[index]
        low = max(centre - spread, 0)
        high = 0xFF if centre > 0xFF - spread else (centre + spread) & 0xFF
        span = (high - low) & 0xFF
        if span:
            if value <= low:
                value = 0
            elif value >= high:
                value = 0xFF
            else:
                value = ((value - low) * 0xFF // span) & 0xFF
        output[index] = value
    return output


def _half_angle(index):
    """Entry ``index`` of the 129-entry half-angle arctangent table."""
    return round(math.degrees(math.atan(index / 128)) * 16 / 1.5 / 2)


_HALF_ANGLE = tuple(_half_angle(index) for index in range(129))


def _round_sqrt(value):
    return (math.isqrt(4 * value) + 1) // 2


def _table_sqrt(value):
    """Integer square root via the driver's tiered tables plus refinement."""
    if value >= 0xFE01:
        return 0xFF
    shift = (0 if value < 0x100 else 4 if value < 0x400 else 5 if value < 0x1000
             else 6 if value < 0x4000 else 7)
    root = _round_sqrt((value >> shift) << shift)
    if (root + 1) * (root + 1) - value < value - root * root:
        root += 1
    return root & 0xFF


def _angle(a, b):
    """Doubled-angle orientation in 1.5° units (0..119) from ``a = gxx - gyy``
    and ``b = 2 gxy``; 30 or 90 when ``a == 0``."""
    if a == 0:
        return 90 if b > 0 else 30
    abs_a, abs_b = abs(a), abs(b)
    if abs_a < abs_b:
        angle = 0x1E0 - _HALF_ANGLE[(abs_b // 2 + abs_a * 0x80) // abs_b]
    else:
        angle = _HALF_ANGLE[(abs_a // 2 + abs_b * 0x80) // abs_a]
    angle = ((angle + 8) >> 4) & 0xFF
    if a < 0:
        if b < 0:
            return angle
        return (120 - angle) & 0xFF if angle else 0
    if b >= 0:
        return (angle + 60) & 0xFF
    return (60 - angle) & 0xFF


def _orientation(xx, yy, xy):
    return _angle(_i32(xx - yy), _i32(xy * 2))


def block_orientation(image, width, height, threshold=0x32):
    """16x16 block orientation (1.5° units) and coherence maps
    (FUN_180013d2c)."""
    blocks_x, blocks_y = width >> 4, height >> 4
    sums = []
    for block_y in range(blocks_y):
        row_start = max(block_y * 16, 1)
        row_stop = min((block_y + 1) * 16, height - 1)
        for block_x in range(blocks_x):
            column_start = max(block_x * 16, 1)
            column_stop = min((block_x + 1) * 16, width - 1)
            gxx = gyy = gxy = 0
            for y in range(row_start, row_stop):
                top = (y - 1) * width
                middle = y * width
                bottom = (y + 1) * width
                for x in range(column_start, column_stop):
                    diagonal = image[bottom + x + 1] - image[top + x - 1]
                    anti = image[top + x + 1] - image[bottom + x - 1]
                    gx = diagonal + 4 * (image[middle + x + 1] - image[middle + x - 1]) + anti
                    gy = diagonal + 4 * (image[bottom + x] - image[top + x]) - anti
                    gxx = _i32(gxx + gx * gx)
                    gyy = _i32(gyy + gy * gy)
                    gxy = _i32(gxy + gx * gy)
            sums.append((gxx, gyy, gxy))
    orientation = bytearray(blocks_x * blocks_y)
    coherence = bytearray(blocks_x * blocks_y)
    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            xx = yy = xy = 0
            for y in range(max(block_y - 1, 0), min(block_y + 2, blocks_y)):
                for x in range(max(block_x - 1, 0), min(block_x + 2, blocks_x)):
                    sxx, syy, sxy = sums[y * blocks_x + x]
                    xx, yy, xy = _i32(xx + sxx), _i32(yy + syy), _i32(xy + sxy)
            xx = _c_div(_i32(xx + 0x480), 0x900)
            yy = _c_div(_i32(yy + 0x480), 0x900)
            xy = _c_div(_i32(xy + 0x480), 0x900)
            index = block_y * blocks_x + block_x
            orientation[index] = _orientation(xx, yy, xy)
            xx, yy = _c_div(xx, 0x24), _c_div(yy, 0x24)
            half_xx, half_yy = xx >> 2, yy >> 2
            difference = half_xx - half_yy
            total = half_xx + half_yy
            cross = (_c_div(xy, 0x24) >> 2) * 2
            scale = _c_div(_i32(total * total), 0xFE01)
            if xx + yy < threshold * threshold or scale == 0:
                energy = 0
            else:
                energy = _c_div(_i32(difference * difference + cross * cross), scale)
            coherence[index] = _table_sqrt(energy) if energy >= 0 else 0
    return orientation, coherence


def mark_background(orientation, coherence, blocks_x, blocks_y, threshold=0x50):
    """Flag blocks whose 3x3 neighbourhood is mostly incoherent
    (FUN_180013bbc). Writes 0xff into ``orientation``; returns the count."""
    marked = 0
    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            strong = total = 0
            for y in range(max(block_y - 1, 0), min(block_y + 1, blocks_y - 1) + 1):
                for x in range(max(block_x - 1, 0), min(block_x + 1, blocks_x - 1) + 1):
                    total += 1
                    strong += coherence[y * blocks_x + x] > threshold
            if strong <= total // 2:
                orientation[block_y * blocks_x + block_x] = 0xFF
                marked += 1
    return marked


def _segment(image, width, height):
    """Shared front end of FUN_1800138e0 and FUN_180014dac."""
    smoothed = box_mean(image, width, height, 1)
    mean = box_mean(smoothed, width, height, 4)
    stretched = local_stretch(smoothed, mean, width, height)
    stretched = box_mean(stretched, width, height, 1)
    return block_orientation(stretched, width, height)


def _mean(image, width, height):
    return sum(image) // (width * height)


# --- stage 3: FUN_180008010 -----------------------------------------------------

def coherence_map(image, width, height):
    """FUN_1800138e0 mode 3: the coherence map, or ``None`` for a blank image
    (mean >= 0xf5), in which case the caller's buffer is left unchanged."""
    if _mean(image, width, height) >= 0xF5:
        return None
    _, coherence = _segment(image, width, height)
    return coherence


def _corner_sum(image, width, start, value, sums, counts):
    if value < 0x3C:
        bucket = 0
    elif value < 0x79:
        return
    else:
        bucket = 1
    for row in range(8):
        sums[bucket] += sum(image[start + row * width:start + row * width + 8])
    counts[bucket] += 0x40


def correct_corners(image, width=WIDTH, height=HEIGHT):
    """Brighten the image so background corners reach white (FUN_180008010,
    mode 0)."""
    _check(image, width, height)
    grid = resample(image, width, height, 0xA0, 0x50)
    coherence = coherence_map(grid, 0xA0, 0x50)
    if coherence is not None:
        grid[:len(coherence)] = coherence
    sums, counts = [0, 0], [0, 0]
    bottom = (height - 8) * width
    for start, value in ((0, grid[0]), (width - 8, grid[9]), (bottom, grid[0x28]),
                         (bottom + width - 8, grid[0x31])):
        _corner_sum(image, width, start, value, sums, counts)
    output = bytearray(image)
    if counts[0] <= 0:
        return output
    background = 0xFF - _c_div(sums[0], counts[0])
    foreground = 0xFF - (_c_div(sums[1], counts[1]) if counts[1] > 0 else 0)
    if background > foreground:
        return output
    return bytearray(min(0xFF, max(0, value + background)) for value in output)


# --- stage 4: quality score FUN_18000b670 / FUN_180014dac -----------------------

def otsu(image, exclude_bright=True):
    """Otsu threshold and class means (FUN_180005b34; FUN_180005d38 is the
    threshold with ``exclude_bright``). Returns ``(threshold, upper_mean,
    lower_mean)`` with the driver's 64-bit unsigned arithmetic."""
    histogram = [0] * 256
    total = excluded = 0
    for value in image:
        if value < 0xF0 or not exclude_bright:
            total += value
            histogram[value] += 1
        else:
            excluded += 1
    count = len(image) - excluded
    best = 0
    threshold, lower_count, lower_sum, upper_count, upper_sum = 0, 1, 0, 1, 0
    seen = weighted = 0
    for value, frequency in enumerate(histogram):
        if not frequency:
            continue
        seen += frequency
        weighted += frequency * value
        rest = count - seen
        if seen and rest:
            rest_sum = total - weighted
            variance = ((weighted * weighted * rest) // seen
                        + (seen * rest_sum * rest_sum) // rest
                        - 2 * weighted * rest_sum) & 0xFFFFFFFFFFFFFFFF
            if best < variance:
                best = variance
                threshold, lower_count, lower_sum = value, seen, weighted
                upper_count, upper_sum = rest, rest_sum
    return (threshold, _c_div(upper_sum, upper_count) & 0xFF,
            _c_div(lower_sum, lower_count) & 0xFF)


def _saturate_top(image, fraction=8):
    """FUN_18000623c: push the brightest 1/fraction of pixels to white."""
    histogram = [0] * 256
    for value in image:
        histogram[value] += 1
    remaining = len(image) // fraction
    level = 0xFF
    for bin_value in range(0xFF, -1, -1):
        remaining -= histogram[bin_value]
        if remaining < 1:
            break
        level -= 1
    shift = 0xFF - level
    return bytearray(min(0xFF, max(0, value + shift)) if value >= 0xFF - shift else value
                     for value in image)


def _block_mean(image, width, start, size=16):
    total = 0
    for row in range(size):
        total += sum(image[start + row * width:start + row * width + size])
    return total // (size * size)


def quality(image, width=WIDTH, height=HEIGHT):
    """Return ``(score, corners, edges)`` exactly as FUN_18000b670 does.

    ``score`` is 0..256 (frames below 60 are rejected), ``corners`` counts
    bright corner blocks and ``edges`` the eight corner/edge blocks.
    """
    _check(image, width, height)
    width_q, height_q = 0x90, 0x50
    grid = resample(image, width, height, width_q, height_q)
    blocks_x, blocks_y = width_q >> 4, height_q >> 4
    mean = _mean(grid, width_q, height_q)
    if mean >= 0xF5:
        return 0x100 - ((blocks_x * blocks_y) << 8) // (blocks_x * blocks_y), 0, 0
    inset = _c_div(blocks_x - 4, 3)
    stretched = _saturate_top(grid)
    _, upper_mean, _ = otsu(stretched, exclude_bright=False)
    threshold, _, _ = otsu(stretched, exclude_bright=False)
    level = max(0xF5, upper_mean - _c_div(upper_mean - threshold, 8))
    last = (height_q - 0x11) * width_q
    starts = [0, width_q - 0x11, last + width_q - 0x11, last]
    if blocks_x > 3:
        near, far = inset * 0x10 + 0xF, width_q - inset * 0x10 - 0x21
        starts += [near, far, last + far, last + near]
    means = [_block_mean(stretched, width_q, start) for start in starts]
    corners = sum(value < level for value in means[:4])
    edges = sum(value < level for value in means)
    orientation, coherence = _segment(grid, width_q, height_q)
    horizontal = sum(1 for value in orientation if ((value - 4) & 0xFF) > 0x70)
    background = mark_background(orientation, coherence, blocks_x, blocks_y)
    end = (blocks_y - 1) * blocks_x
    probes = [0, blocks_x - 1, end + blocks_x - 1, end,
              inset + 1, blocks_x - inset - 2, end + blocks_x - inset - 2, end + inset + 1]
    for position, (value, probe) in enumerate(zip(means, probes)):
        if value < level and mean <= value and coherence[probe] < 0x50:
            edges -= 1
            if position < 4:
                corners -= 1
    blocks = blocks_x * blocks_y
    if horizontal >= _c_div(blocks * 2, 3):
        score = 0
    else:
        score = 0x100 - _c_div(background << 8, blocks)
    return score, corners, edges


# --- stage 5: FUN_180005184 --------------------------------------------------------

def normalize(image, target=NORMALIZE_TARGET):
    """Rescale so the Otsu threshold maps to ``target`` (FUN_180005184)."""
    threshold, _, _ = otsu(image)
    gain = 0x10 if threshold == 0xFF else _c_div((0xFF - target) * 0x10, 0xFF - threshold)
    output = bytearray(len(image))
    for index, value in enumerate(image):
        scaled = ((0xFF - value) * gain + 8) >> 4
        output[index] = 0 if scaled >= 0xFF else 0xFF if scaled < 0 else 0xFF - scaled
    return output


# --- stage 6: bad-pixel repair FUN_18000c0f8 --------------------------------------

# Neighbour order NW N NE W E SW S SE; even rows pick one opposite pair
# (0: W/E, 2: NW/SE, 4: N/S, 6: NE/SW) and odd rows split the weight across
# the two adjacent even rows, row 7 wrapping to row 0 (table 0x18002d640).
_PAIRS = {0: (3, 4), 2: (0, 7), 4: (1, 6), 6: (2, 5)}


def _repair_weights():
    rows = []
    for bucket in range(8):
        row = [0] * 8
        if bucket % 2 == 0:
            for neighbour in _PAIRS[bucket]:
                row[neighbour] = 16
        else:
            for even in (bucket - 1, (bucket + 1) % 8):
                for neighbour in _PAIRS[even]:
                    row[neighbour] = 8
        rows.append(tuple(row))
    return tuple(rows)


_REPAIR_WEIGHTS = _repair_weights()


def _repair_orientation(image, mask, x, y, width, height, window=16):
    """FUN_180005360: Prewitt orientation over a window, or ``None``."""
    half = window // 2
    gxx = gyy = gxy = samples = 0
    for xx in range(x - half, x + half):
        if not 0 < xx < width - 1:
            continue
        for yy in range(y - half, y + half):
            if not 0 < yy < height - 1:
                continue
            top, middle, bottom = (yy - 1) * width + xx, yy * width + xx, (yy + 1) * width + xx
            # A 32-bit load at index-1 masked with 0xffffff00: columns xx..xx+2.
            if any(mask[row + offset] for row in (top, middle, bottom) for offset in range(3)):
                continue
            gx = (image[middle + 1] - image[middle - 1] - image[bottom - 1] - image[top - 1]
                  + image[bottom + 1] + image[top + 1])
            gy = (image[bottom] - image[top] - image[top + 1] - image[top - 1]
                  + image[bottom + 1] + image[bottom - 1])
            gxx += gx * gx
            gyy += gy * gy
            gxy += gx * gy
            samples += 1
    if not samples:
        return None
    rounding = samples // 2
    b = _c_div(rounding + gxy, samples) * 2
    a = _c_div(gxx + rounding, samples) - _c_div(gyy + rounding, samples)
    return _angle(a, b)


def _interpolate(image, mask, x, y, width, height, bucket):
    """FUN_1800057a4: weighted mean of the unflagged 3x3 neighbours."""
    weights = _REPAIR_WEIGHTS[bucket]
    total = weight_sum = neighbour = 0
    for row in range(3):
        yy = y - 1 + row
        if not 0 <= yy <= height - 1:
            continue  # the weight index only advances on in-range rows
        for column in range(3):
            if row == 1 and column == 1:
                continue
            xx = x - 1 + column
            if 0 <= xx < width - 1 and not mask[yy * width + xx] & 1:
                total += image[yy * width + xx] * weights[neighbour]
                weight_sum += weights[neighbour]
            neighbour += 1
    return None if weight_sum < 1 else total // weight_sum


def repair_bad_pixels(image, bad_pixels, width=WIDTH, height=HEIGHT, passes=2):
    """Interpolate flagged pixels along the local ridge direction
    (FUN_18000c0f8 → FUN_1800051c0)."""
    _check(image, width, height)
    _check(bad_pixels, width, height)
    count = width * height
    # The driver widens a stack copy and may write one byte past its end;
    # the orientation mask check can later read that byte.
    mask = bytearray(bad_pixels) + b"\0"
    index = 0
    while index < count:
        if mask[index] == 1:
            index += 1
            if mask[index] != 1:
                mask[index] = 1
        index += 1
    output = bytearray(image)
    for _ in range(passes):
        unrepaired = 0
        for y in range(height):
            for x in range(width):
                if not mask[y * width + x] & 1:
                    continue
                angle = _repair_orientation(output, mask, x, y, width, height)
                if angle is not None:
                    bucket = ((angle + 7) * 0x112 >> 12) & 7
                    value = _interpolate(output, mask, x, y, width, height, bucket)
                    if value is not None:
                        output[y * width + x] = value & 0xFF
                        mask[y * width + x] = 2
                        continue
                unrepaired += 1
        if not unrepaired:
            break
    return output


# --- pipeline -------------------------------------------------------------------

def flip_vertical(image, width=WIDTH, height=HEIGHT):
    """ET510 GetImage copies rows last-to-first into the WinBio buffer."""
    _check(image, width, height)
    return bytearray(b"".join(bytes(image[row * width:(row + 1) * width])
                              for row in range(height - 1, -1, -1)))


def process_frame(raw_frame, bad_pixels, fill):
    """Per-frame state-0x15 chain: stages 1-3 plus the quality score.

    Returns ``(image, score, corners, edges)``. Hardware exposure control
    (FUN_18000938c, sensor registers 0x0f/0x12) happens before capture and
    has no pixel-level effect here.
    """
    image = subtract_minimum(raw_frame, bad_pixels, fill)
    image = correct_corners(invert(image))
    score, corners, edges = quality(image)
    return image, score, corners, edges


def finish_frame(image, bad_pixels):
    """Stages 5-7: normalise, repair, and flip the selected frame."""
    image = normalize(image)
    image = repair_bad_pixels(image, bad_pixels)
    return flip_vertical(image)


class FrameSelector:
    """Best-of-N frame selection from FUN_18000a93c.

    Seed it with the processed finger-detect frame (the driver stores that
    frame's score, edge count and image before entering state 0x15), then
    ``offer`` each processed frame until it returns True.
    """

    MAX_FRAMES = 10
    LOW_EDGES = 0x1
    PARTIAL_EDGES = 0x8

    def __init__(self, image, score, edges, previous_score=None):
        self.image = bytearray(image)
        self.score = score
        self.edges = edges
        self.previous_score = score if previous_score is None else previous_score
        self.frames = 0

    def offer(self, image, score, edges):
        self.frames += 1
        if score >= self.score and edges >= self.edges:
            self.image, self.score, self.edges = bytearray(image), score, edges
        if score < QUALITY_ACCEPT or self.frames > self.MAX_FRAMES - 1:
            return True
        stable = abs(self.previous_score - score) < 9
        self.previous_score = score
        return edges > 7 and stable and self.frames > 1

    @property
    def flags(self):
        if self.edges >= 8:
            return 0
        return self.LOW_EDGES if self.edges < 2 else self.PARTIAL_EDGES
