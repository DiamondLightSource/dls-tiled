"""Draw scan points into an image for a viewport (prototype).

Each renderer takes the points of one channel (values and positions) and a
``Viewport`` (a rectangle in data coordinates and an output size in pixels), and
returns two ``(height, width)`` arrays: the image (NaN where nothing is drawn)
and the coverage (0 to 1, how much of the pixel is covered by the drawn points).
Row 0 is the viewport's ``y0`` edge and column 0 its ``x0`` edge.

- ``cells``: the scan's grid cells, each with the mean of the points in it.
- ``rectangles``: a rectangle of the nominal cell size centred on each recorded
  position, so the picture follows where the points really were.
- ``gaussian``: a beam-sized 2-D Gaussian per point, so any gaps the scan left
  between beam footprints show up as low coverage.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import numpy

FWHM_TO_SIGMA = 1 / (2 * numpy.sqrt(2 * numpy.log(2)))


@dataclass(frozen=True)
class Viewport:
    """A rectangle in data coordinates drawn at ``width`` x ``height`` pixels."""

    x0: float
    x1: float
    y0: float
    y1: float
    width: int
    height: int

    @classmethod
    def fit(cls, x0, x1, y0, y1, size):
        """The viewport with ``size`` pixels along its longer side, square pixels."""
        span_x, span_y = abs(x1 - x0) or 1.0, abs(y1 - y0) or 1.0
        if span_x >= span_y:
            width, height = size, max(1, round(size * span_y / span_x))
        else:
            width, height = max(1, round(size * span_x / span_y)), size
        return cls(float(x0), float(x1), float(y0), float(y1), int(width), int(height))

    @property
    def pixel_x(self) -> float:
        return (self.x1 - self.x0) / self.width

    @property
    def pixel_y(self) -> float:
        return (self.y1 - self.y0) / self.height

    def centres(self):
        """Pixel centres along x (columns) and y (rows)."""
        xs = self.x0 + (numpy.arange(self.width) + 0.5) * self.pixel_x
        ys = self.y0 + (numpy.arange(self.height) + 0.5) * self.pixel_y
        return xs, ys

    def column(self, x):
        """Fractional column of a data x coordinate."""
        return (numpy.asarray(x, dtype=float) - self.x0) / self.pixel_x

    def row(self, y):
        return (numpy.asarray(y, dtype=float) - self.y0) / self.pixel_y


def _finish(total, weight, coverage):
    image = numpy.full(total.shape, numpy.nan)
    drawn = weight > 0
    image[drawn] = total[drawn] / weight[drawn]
    return image, numpy.clip(coverage, 0.0, 1.0)


def render_cells(values, x, y, x_edges, y_edges, viewport: Viewport):
    """The scan's grid cells (mean of the points in each), sampled at each pixel."""
    values = numpy.asarray(values, dtype=float)
    counts, _, _ = numpy.histogram2d(x, y, bins=[x_edges, y_edges])
    sums, _, _ = numpy.histogram2d(x, y, bins=[x_edges, y_edges], weights=values)
    xs, ys = viewport.centres()
    col = numpy.searchsorted(x_edges, xs, side="right") - 1
    row = numpy.searchsorted(y_edges, ys, side="right") - 1
    inside_x = (col >= 0) & (col < len(x_edges) - 1)
    inside_y = (row >= 0) & (row < len(y_edges) - 1)
    total = numpy.zeros((viewport.height, viewport.width))
    weight = numpy.zeros_like(total)
    rr, cc = numpy.meshgrid(
        numpy.where(inside_y)[0], numpy.where(inside_x)[0], indexing="ij"
    )
    cell_y, cell_x = row[rr], col[cc]
    total[rr, cc] = sums[cell_x, cell_y]
    weight[rr, cc] = counts[cell_x, cell_y]
    return _finish(total, weight, (weight > 0).astype(float))


def render_rectangles(values, x, y, cell_x, cell_y, viewport: Viewport):
    """A rectangle of ``cell_x`` by ``cell_y`` centred on each point.

    Where rectangles overlap, the pixel takes the mean of their values, weighted
    by how much of the pixel each one covers.
    """
    values = numpy.asarray(values, dtype=float)
    total = numpy.zeros((viewport.height, viewport.width))
    weight = numpy.zeros_like(total)
    for v, px, py in zip(values, viewport.column(x), viewport.row(y), strict=True):
        if not numpy.isfinite(v):
            continue
        cx0, cx1 = (
            px - cell_x / 2 / viewport.pixel_x,
            px + cell_x / 2 / viewport.pixel_x,
        )
        cy0, cy1 = (
            py - cell_y / 2 / viewport.pixel_y,
            py + cell_y / 2 / viewport.pixel_y,
        )
        c_lo, c_hi = (
            max(0, int(numpy.floor(cx0))),
            min(viewport.width, int(numpy.ceil(cx1))),
        )
        r_lo, r_hi = (
            max(0, int(numpy.floor(cy0))),
            min(viewport.height, int(numpy.ceil(cy1))),
        )
        if c_lo >= c_hi or r_lo >= r_hi:
            continue
        cols = numpy.arange(c_lo, c_hi)
        rows = numpy.arange(r_lo, r_hi)
        # fraction of each pixel the rectangle covers, along each axis
        fx = numpy.clip(numpy.minimum(cols + 1, cx1) - numpy.maximum(cols, cx0), 0, 1)
        fy = numpy.clip(numpy.minimum(rows + 1, cy1) - numpy.maximum(rows, cy0), 0, 1)
        overlap = numpy.outer(fy, fx)
        total[r_lo:r_hi, c_lo:c_hi] += overlap * v
        weight[r_lo:r_hi, c_lo:c_hi] += overlap
    return _finish(total, weight, weight)


def render_gaussian(values, x, y, fwhm_x, fwhm_y, viewport: Viewport, reach=3.0):
    """A 2-D Gaussian of the beam's size (FWHM in data units) on each point.

    Each pixel takes the mean of the points' values weighted by their Gaussians.
    Coverage is the summed Gaussian weight, 1 at the centre of an isolated beam,
    so the space between beam footprints that the scan didn't cover shows up as
    low coverage rather than being filled in.
    """
    values = numpy.asarray(values, dtype=float)
    sx = fwhm_x * FWHM_TO_SIGMA / viewport.pixel_x  # sigma in pixels
    sy = fwhm_y * FWHM_TO_SIGMA / viewport.pixel_y
    total = numpy.zeros((viewport.height, viewport.width))
    weight = numpy.zeros_like(total)
    for v, px, py in zip(values, viewport.column(x), viewport.row(y), strict=True):
        if not numpy.isfinite(v):
            continue
        c_lo, c_hi = (
            max(0, int(px - reach * sx)),
            min(viewport.width, int(px + reach * sx) + 1),
        )
        r_lo, r_hi = (
            max(0, int(py - reach * sy)),
            min(viewport.height, int(py + reach * sy) + 1),
        )
        if c_lo >= c_hi or r_lo >= r_hi:
            continue
        gx = numpy.exp(-0.5 * ((numpy.arange(c_lo, c_hi) + 0.5 - px) / sx) ** 2)
        gy = numpy.exp(-0.5 * ((numpy.arange(r_lo, r_hi) + 0.5 - py) / sy) ** 2)
        g = numpy.outer(gy, gx)
        total[r_lo:r_hi, c_lo:c_hi] += g * v
        weight[r_lo:r_hi, c_lo:c_hi] += g
    image, coverage = _finish(total, weight, weight)
    image[coverage < 0.02] = numpy.nan  # beyond the beams' reach: nothing drawn
    return image, coverage


def to_rows(image) -> list[list[float | None]]:
    """An image as JSON rows, ``None`` where nothing was drawn."""
    out = image.astype(object)
    out[numpy.isnan(image)] = None
    return out.tolist()


CHANNEL_COLOUR = {"RedTotal": 0, "GreenTotal": 1, "BlueTotal": 2}


def to_png(image, coverage, colour=0, vmin=None, vmax=None) -> bytes:
    """An RGBA PNG of one channel, tinted (0/1/2 = red/green/blue), transparent
    where nothing was drawn. Row 0 of the image is the top row of the PNG."""
    valid = image[~numpy.isnan(image)]
    lo = float(valid.min()) if vmin is None and valid.size else (vmin or 0.0)
    hi = float(valid.max()) if vmax is None and valid.size else (vmax or 1.0)
    scaled = numpy.zeros(image.shape)
    if hi > lo:
        scaled = (numpy.nan_to_num(image, nan=lo) - lo) / (hi - lo)
    rgba = numpy.zeros(image.shape + (4,), dtype=numpy.uint8)
    rgba[..., colour] = numpy.clip(numpy.round(scaled * 255), 0, 255)
    alpha = numpy.where(numpy.isnan(image), 0.0, coverage)
    rgba[..., 3] = numpy.clip(numpy.round(alpha * 255), 0, 255)
    return _encode_png(rgba)


def _encode_png(rgba) -> bytes:
    height, width, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[r].tobytes() for r in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
