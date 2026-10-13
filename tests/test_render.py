"""Tests for the prototype /render endpoint and its renderers."""

import struct

import numpy
import pytest
from scanspec.specs import Line
from tiled.catalog import in_memory
from tiled.client import Context, from_context
from tiled.server.app import build_app

from dls_tiled import rendering, visr
from dls_tiled.rendering import Viewport
from dls_tiled.visr import visr_router


@pytest.fixture(autouse=True)
def no_shared_results(monkeypatch):
    visr._recent.clear()
    visr._inflight.clear()
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.0)


@pytest.fixture
def client(tmp_path):
    app = build_app(
        in_memory(writable_storage=str(tmp_path)), include_routers=[visr_router]
    )
    with Context.from_app(app) as context:
        yield from_context(context)


# --- renderers ---


def test_viewport_fit_keeps_pixels_square():
    viewport = Viewport.fit(0, 4, 0, 2, 8)

    assert (viewport.width, viewport.height) == (8, 4)
    xs, ys = viewport.centres()
    assert xs[0] == 0.25 and ys[-1] == 1.75


def test_cells_fill_each_grid_cell_with_its_mean():
    edges = numpy.array([-0.5, 0.5, 1.5, 2.5])
    x = numpy.array([0.0, 1.0, 2.0, 0.0])
    y = numpy.array([0.0, 0.0, 0.0, 2.0])
    values = numpy.array([1.0, 2.0, 3.0, 4.0])

    image, coverage = rendering.render_cells(
        values, x, y, edges, edges, Viewport.fit(-0.5, 2.5, -0.5, 2.5, 3)
    )

    assert numpy.array_equal(image[0], [1.0, 2.0, 3.0])  # row 0 is y = 0
    assert numpy.isnan(image[1]).all()
    assert image[2, 0] == 4.0 and coverage[2, 0] == 1.0 and coverage[1, 1] == 0.0


def test_cells_zoomed_into_one_cell_show_that_cell_everywhere():
    edges = numpy.array([-0.5, 0.5, 1.5])
    image, _ = rendering.render_cells(
        numpy.array([1.0, 2.0]),
        numpy.array([0.0, 1.0]),
        numpy.array([0.0, 0.0]),
        edges,
        edges,
        Viewport.fit(0.6, 1.4, -0.4, 0.4, 4),
    )

    assert numpy.all(image == 2.0)


def test_rectangles_cover_their_cell_around_the_recorded_position():
    image, coverage = rendering.render_rectangles(
        [5.0], [2.0], [2.0], 2.0, 2.0, Viewport.fit(0, 4, 0, 4, 4)
    )

    assert numpy.all(image[1:3, 1:3] == 5.0) and numpy.all(coverage[1:3, 1:3] == 1.0)
    assert numpy.isnan(image[0]).all() and coverage[0].sum() == 0.0


def test_rectangles_cover_part_of_a_pixel_they_only_partly_overlap():
    # x: [1.75, 2.75] covers a quarter of column 1 and three quarters of column 2;
    # y: [2, 3] covers row 2 exactly
    _, coverage = rendering.render_rectangles(
        [1.0], [2.25], [2.5], 1.0, 1.0, Viewport.fit(0, 4, 0, 4, 4)
    )

    assert coverage[2, 1] == pytest.approx(0.25)
    assert coverage[2, 2] == pytest.approx(0.75)
    assert coverage[1].sum() == 0.0


def test_rectangles_average_where_they_overlap_weighted_by_coverage():
    # x: [0.5, 2.5] (value 2) and [1.5, 3.5] (value 4)
    image, _ = rendering.render_rectangles(
        [2.0, 4.0], [1.5, 2.5], [2.0, 2.0], 2.0, 2.0, Viewport.fit(0, 4, 0, 4, 4)
    )

    assert image[1, 0] == pytest.approx(2.0)  # only the first: half the pixel
    assert image[1, 1] == pytest.approx((2.0 * 1 + 4.0 * 0.5) / 1.5)
    assert image[1, 2] == pytest.approx((2.0 * 0.5 + 4.0 * 1) / 1.5)
    assert image[1, 3] == pytest.approx(4.0)  # only the second


def test_gaussian_leaves_the_gap_between_far_apart_beams_empty():
    image, coverage = rendering.render_gaussian(
        [1.0, 1.0], [1.0, 9.0], [1.0, 1.0], 1.0, 1.0, Viewport.fit(0, 10, 0, 2, 50)
    )

    middle = 25  # x = 5, four beam widths from either point
    assert numpy.isnan(image[5, middle]) and coverage[5, middle] < 0.02
    assert coverage[5, 4] > 0.9  # x = 0.9, at a beam centre


def test_gaussian_weights_overlapping_beams_by_distance():
    image, _ = rendering.render_gaussian(
        [0.0, 10.0], [1.0, 2.0], [1.0, 1.0], 2.0, 2.0, Viewport.fit(0, 3, 0, 2, 30)
    )

    assert (
        image[10, 10] < 5.0 < image[10, 20]
    )  # nearer the first point, then the second


def test_png_is_a_png_of_the_viewport_size_with_transparent_gaps():
    image = numpy.array([[1.0, numpy.nan], [2.0, 3.0]])
    payload = rendering.to_png(image, numpy.ones_like(image), colour=1)

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", payload[16:24])
    assert (width, height) == (2, 2)


# --- the endpoint ---


def write_scan(client, uid, n_points=9, x_shift=0.0, y_shift=0.0, start_extra=None):
    """The first `n_points` of a 3x3 raster whose spec is in the start document."""
    spec = Line("Y", 0, 2, 3) * Line("X", 0, 2, 3)
    start = {"spec": spec.serialize(), **(start_extra or {})}
    primary = client.create_container(uid, metadata={"start": start}).create_container(
        "primary"
    )
    primary.write_array(
        (numpy.array([0.0, 1.0, 2.0] * 3) + x_shift)[:n_points], key="X"
    )
    primary.write_array(
        (numpy.repeat([0.0, 1.0, 2.0], 3) + y_shift)[:n_points], key="Y"
    )
    for channel in ("RedTotal", "GreenTotal", "BlueTotal"):
        primary.write_array(numpy.arange(9.0)[:n_points], key=channel)


def get(client, uid, **params):
    return client.context.http_client.get(f"/api/v1/render/{uid}", params=params)


def test_render_cells_over_the_scan_extent_by_default(client):
    write_scan(client, "grid")

    body = get(client, "grid", size=3).json()

    assert body["kind"] == "cells"
    assert body["viewport"] == {
        "x0": -0.5,
        "x1": 2.5,
        "y0": -0.5,
        "y1": 2.5,
        "width": 3,
        "height": 3,
    }
    assert body["channels"]["RedTotal"] == [
        [0.0, 1.0, 2.0],
        [3.0, 4.0, 5.0],
        [6.0, 7.0, 8.0],
    ]
    assert body["n_points"] == 9 and body["n_total"] == 9


def test_render_zooms_into_a_viewport(client):
    write_scan(client, "zoom")

    body = get(client, "zoom", x0=0.5, x1=2.5, y0=-0.5, y1=0.5, size=4).json()

    assert (body["viewport"]["width"], body["viewport"]["height"]) == (4, 2)
    assert body["channels"]["RedTotal"][0] == [1.0, 1.0, 2.0, 2.0]


def test_render_rectangles_use_the_scan_step_and_aligned_fly_positions(client):
    write_scan(client, "fly", x_shift=1.0, y_shift=10.4)

    body = get(client, "fly", kind="rectangles", size=3).json()

    assert body["cell"] == {"x": 1.0, "y": 1.0}
    assert body["position_offset"] == pytest.approx({"x": 1.0, "y": 10.4})
    assert body["channels"]["RedTotal"] == [
        [0.0, 1.0, 2.0],
        [3.0, 4.0, 5.0],
        [6.0, 7.0, 8.0],
    ]


def test_render_gaussian_takes_the_beam_from_the_request_metadata_or_step(client):
    write_scan(client, "beam", start_extra={"beam_size": {"x": 0.5, "y": 0.25}})
    write_scan(client, "nobeam")

    asked = get(client, "beam", kind="gaussian", beam_x=0.2, beam_y=0.2).json()
    stored = get(client, "beam", kind="gaussian").json()
    step = get(client, "nobeam", kind="gaussian").json()

    assert asked["beam"] == {"x": 0.2, "y": 0.2, "source": "request"}
    assert stored["beam"] == {"x": 0.5, "y": 0.25, "source": "metadata"}
    assert step["beam"] == {"x": 1.0, "y": 1.0, "source": "scan step"}


def test_render_gaussian_shows_gaps_a_small_beam_leaves(client):
    write_scan(client, "gaps")

    body = get(client, "gaps", kind="gaussian", beam_x=0.2, beam_y=0.2, size=60).json()

    coverage = numpy.array(body["coverage"])
    assert coverage.max() > 0.9  # on a point
    assert (
        coverage[:, 30].min() < 0.02 or coverage[30, :].min() < 0.02
    )  # between points
    assert None in body["channels"]["RedTotal"][30]


def test_render_png(client):
    write_scan(client, "png")

    response = get(
        client, "png", format="png", channel="GreenTotal", size=16, vmin=0, vmax=8
    )

    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    assert struct.unpack(">II", response.content[16:24]) == (16, 16)


def test_render_shows_the_scan_extent_from_its_first_points(client):
    write_scan(client, "early", n_points=2)

    body = get(client, "early", size=3).json()

    assert body["viewport"]["x0"] == -0.5 and body["viewport"]["y1"] == 2.5
    assert body["channels"]["RedTotal"][0] == [0.0, 1.0, None]
    assert body["n_points"] == 2


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "contour"},
        {"format": "svg"},
        {"format": "png", "channel": "GrayTotal"},
        {"size": 0},
        {"x0": 1.0, "x1": 1.0},
    ],
)
def test_render_rejects_bad_requests(client, params):
    write_scan(client, "bad")

    assert get(client, "bad", **params).status_code == 400
