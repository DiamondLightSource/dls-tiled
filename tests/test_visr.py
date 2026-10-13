import asyncio
import json
from types import SimpleNamespace

import numpy
import pytest
from fastapi import HTTPException
from scanspec.specs import Line, Spiral
from tiled.adapters.utils import DataNotReadyError
from tiled.catalog import in_memory
from tiled.client import Context, from_context
from tiled.server.app import build_app

from dls_tiled import visr
from dls_tiled.visr import (
    compute_binned_image,
    get_data,
    get_setpoints,
    visr_router,
)


@pytest.fixture
def client(tmp_path):
    catalog = in_memory(writable_storage=str(tmp_path))
    app = build_app(catalog, include_routers=[visr_router])
    with Context.from_app(app) as context:
        yield from_context(context)


def write_step_scan(client, uid="scan"):
    """A 2x2 grid of step-scan points, two points per grid cell."""
    primary = client.create_container(uid).create_container("primary")
    x = numpy.array([0.0, 0.1, 1.0, 1.1, 0.0, 0.1, 1.0, 1.1])
    y = numpy.array([0.0, 0.1, 0.0, 0.1, 1.0, 1.1, 1.0, 1.1])
    primary.write_array(x, key="X")
    primary.write_array(y, key="Y")
    for channel, scale in (("RedTotal", 1), ("GreenTotal", 10), ("BlueTotal", 100)):
        primary.write_array(numpy.arange(8.0) * scale, key=channel)


def test_binned_step_scan(client):
    write_step_scan(client)

    response = client.context.http_client.get(
        "/api/v1/binned/scan", params={"width": 2, "height": 2}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # mean of the two points in each cell; one row per y bin, one column per x bin
    assert body["RedTotal"] == [[0.5, 2.5], [4.5, 6.5]]
    assert body["GreenTotal"] == [[5.0, 25.0], [45.0, 65.0]]
    assert body["x_limits"] == [0.0, 0.55, 1.1]
    assert body["y_limits"] == [0.0, 0.55, 1.1]
    assert body["n_points"] == 8


def test_binned_rows_are_y_and_columns_are_x(client):
    # x only takes two values and y four, so the image must be 4 rows by 2 columns
    primary = client.create_container("raster").create_container("primary")
    x = numpy.array([0.0, 1.0] * 4)
    y = numpy.repeat([0.0, 1.0, 2.0, 3.0], 2)
    primary.write_array(x, key="X")
    primary.write_array(y, key="Y")
    for channel in ("RedTotal", "GreenTotal", "BlueTotal"):
        primary.write_array(numpy.arange(8.0), key=channel)

    response = client.context.http_client.get(
        "/api/v1/binned/raster", params={"width": 2, "height": 4}
    )

    body = response.json()
    assert body["RedTotal"] == [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0], [6.0, 7.0]]
    assert len(body["x_limits"]) == 3 and len(body["y_limits"]) == 5


def test_binned_reports_empty_bins_as_null_and_keeps_a_measured_zero(client):
    primary = client.create_container("sparse").create_container("primary")
    primary.write_array(numpy.array([0.0, 1.0]), key="X")
    primary.write_array(numpy.array([0.0, 1.0]), key="Y")
    for channel, values in (
        ("RedTotal", [0.0, 5.0]),
        ("GreenTotal", [1.0, 2.0]),
        ("BlueTotal", [3.0, 4.0]),
    ):
        primary.write_array(numpy.array(values), key=channel)

    response = client.context.http_client.get(
        "/api/v1/binned/sparse", params={"width": 2, "height": 2}
    )

    # a measured 0.0 stays 0.0; the two cells no point landed in are null
    assert response.json()["RedTotal"] == [[0.0, None], [None, 5.0]]


def test_binned_missing_run_is_422(client):
    response = client.context.http_client.get("/api/v1/binned/nonexistent")

    assert response.status_code == 422


def test_compute_binned_image_empty_bins_are_zero():
    result = compute_binned_image(
        numpy.array([2.0]),
        numpy.array([0.0]),
        numpy.array([0.0]),
        bins=2,
        range=((0, 1), (0, 1)),
    )

    assert result["img"].tolist() == [[2.0, 0.0], [0.0, 0.0]]
    assert result["counts"].tolist() == [[1.0, 0.0], [0.0, 0.0]]


def test_image_rows_are_y_transposes_and_marks_empty_bins():
    img = numpy.array([[1.0, 2.0, 0.0], [3.0, 0.0, 0.0]])  # [x_bin, y_bin]
    counts = numpy.array([[1, 1, 0], [1, 0, 0]])

    assert visr.image_rows_are_y(img, counts) == [
        [1.0, 3.0],
        [2.0, None],
        [None, None],
    ]


def _setpoints(spec, **start):
    """Run get_setpoints against a fake root whose run has this start document."""
    metadata = {"start": {"spec": spec.serialize(), **start}}
    adapter = SimpleNamespace(metadata=lambda: metadata)

    async def lookup_adapter(path):
        return adapter

    return asyncio.run(
        get_setpoints(SimpleNamespace(lookup_adapter=lookup_adapter), "uid")
    )


def test_setpoints_follow_axis_names_not_dimension_order():
    # Raster scan: y is the outer dimension, x the inner one.
    spec = Line("sample_stage-y", 0, 1, 2) * Line("sample_stage-x", 0, 10, 3)

    x, y, z = _setpoints(spec)

    assert x.tolist() == [0, 5, 10, 0, 5, 10]
    assert y.tolist() == [0, 0, 0, 1, 1, 1]
    assert numpy.isnan(z).all()


def test_setpoints_of_runs_with_repr_axes_use_the_motors_list():
    spec = Line("<Motor object at 0x1>", 0, 1, 2) * Line(
        "<Motor object at 0x2>", 0, 10, 3
    )

    x, y, _ = _setpoints(spec, motors=["sample_stage-y", "sample_stage-x"])

    assert x.tolist() == [0, 5, 10, 0, 5, 10]
    assert y.tolist() == [0, 0, 0, 1, 1, 1]


def test_setpoints_keep_dimension_order_when_axes_are_not_x_y_z():
    spec = Line("theta", 0, 1, 2) * Line("energy", 0, 10, 3)

    x, y, _ = _setpoints(spec)

    assert x.tolist() == [0, 0, 0, 1, 1, 1]
    assert y.tolist() == [0, 5, 10, 0, 5, 10]


class _LaggingAdapter:
    """A leaf whose file is behind the catalog for the first `lag` reads."""

    def __init__(self, lag):
        self.lag = lag
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.reads <= self.lag:
            raise DataNotReadyError("advertises (25,), only (24,) available")
        return numpy.arange(25.0)


def _root_with(adapter):
    async def lookup_adapter(segments):
        return adapter

    return SimpleNamespace(lookup_adapter=lookup_adapter)


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(visr, "NOT_READY_RETRY_DELAY", 0.0)


def test_get_data_retries_a_file_that_is_behind_the_catalog(fast_retries):
    adapter = _LaggingAdapter(lag=2)

    data = asyncio.run(get_data(_root_with(adapter), ["uid", "primary", "RedTotal"]))

    assert isinstance(data, numpy.ndarray)
    assert data.shape == (25,)
    assert adapter.reads == 3


def test_get_data_answers_503_with_retry_after_when_the_file_stays_behind(fast_retries):
    adapter = _LaggingAdapter(lag=100)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(get_data(_root_with(adapter), ["uid", "primary", "RedTotal"]))

    assert excinfo.value.status_code == 503
    assert excinfo.value.headers == {"Retry-After": "1"}
    assert adapter.reads == visr.NOT_READY_RETRIES + 1


def test_binned_keeps_a_503_instead_of_turning_it_into_422(client, monkeypatch):
    write_step_scan(client)

    async def not_ready(root, segments):
        raise HTTPException(
            status_code=503, detail="not ready", headers={"Retry-After": "1"}
        )

    monkeypatch.setattr(visr, "get_data", not_ready)

    response = client.context.http_client.get("/api/v1/binned/scan")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


# --- the scan's own grid ------------------------------------------------------------


def _grid_or_none(spec, motors=None):
    return visr._grid_from_spec(json.dumps(spec.serialize()), json.dumps(motors))


def _grid(spec, motors=None):
    grid = _grid_or_none(spec, motors)
    assert grid is not None
    return grid


def test_grid_edges_sit_halfway_between_setpoints():
    spec = Line("sample_stage-y", 0, 2, 3) * ~Line("sample_stage-x", 0, 10, 3)

    x_edges, y_edges, n_total, *_ = _grid(spec)

    assert x_edges.tolist() == [-2.5, 2.5, 7.5, 12.5]
    assert y_edges.tolist() == [-0.5, 0.5, 1.5, 2.5]
    assert n_total == 9


def test_grid_of_runs_with_repr_axes_uses_the_motors_list():
    spec = Line("<Motor object at 0x1>", 0, 2, 3) * Line(
        "<Motor object at 0x2>", 0, 10, 3
    )

    x_edges, y_edges, *_ = _grid(spec, ["sample_stage-y", "sample_stage-x"])

    assert len(x_edges) == 4 and len(y_edges) == 4


def test_grid_is_none_when_the_axes_are_not_x_and_y():
    assert _grid_or_none(Line("theta", 0, 1, 2) * Line("energy", 0, 10, 3)) is None


def test_grid_is_none_when_the_points_do_not_lie_on_a_grid():
    assert (
        _grid_or_none(
            Spiral(
                x_axis="x", x_centre=0, x_diameter=5, x_step=1, y_axis="y", y_centre=0
            )
        )
        is None
    )


def test_grid_is_none_when_it_has_more_cells_than_the_cap(monkeypatch):
    spec = Line("y", 0, 2, 4) * Line("x", 0, 2, 4)
    visr._grid_from_spec.cache_clear()
    assert _grid_or_none(spec) is not None

    monkeypatch.setattr(visr, "MAX_GRID_CELLS", 10)
    visr._grid_from_spec.cache_clear()

    assert _grid_or_none(spec) is None
    visr._grid_from_spec.cache_clear()


def test_grid_is_none_for_a_scan_narrower_than_three_setpoints():
    # a line, and a 2-wide grid: too few points on an axis for the UI's plot axes
    assert _grid_or_none(Line("y", 0, 2, 3) * Line("x", 3, 3, 1)) is None
    assert _grid_or_none(Line("y", 0, 2, 3) * Line("x", 0, 1, 2)) is None


def write_grid_scan(client, uid, n_points):
    """The first `n_points` of a 3 (x) by 3 (y) raster; its spec is in the start doc."""
    spec = Line("Y", 0, 2, 3) * Line("X", 0, 2, 3)
    metadata = {"start": {"spec": spec.serialize(), "motors": ["Y", "X"]}}
    primary = client.create_container(uid, metadata=metadata).create_container(
        "primary"
    )
    x = numpy.array([0.0, 1.0, 2.0] * 3)[:n_points]
    y = numpy.repeat([0.0, 1.0, 2.0], 3)[:n_points]
    primary.write_array(x, key="X")
    primary.write_array(y, key="Y")
    for channel in ("RedTotal", "GreenTotal", "BlueTotal"):
        primary.write_array(numpy.arange(9.0)[:n_points], key=channel)


def test_binned_uses_the_scans_grid_without_being_asked(client):
    write_grid_scan(client, "grid", 9)

    body = client.context.http_client.get("/api/v1/binned/grid").json()

    assert body["RedTotal"] == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0], [6.0, 7.0, 8.0]]
    assert body["x_limits"] == [-0.5, 0.5, 1.5, 2.5]
    assert body["y_limits"] == [-0.5, 0.5, 1.5, 2.5]
    assert (body["n_points"], body["n_total"]) == (9, 9)


def test_binned_grid_is_the_same_shape_from_the_first_points_of_a_scan(client):
    write_grid_scan(client, "partial", 7)  # the last row is only partly measured

    body = client.context.http_client.get("/api/v1/binned/partial").json()

    assert body["RedTotal"] == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0], [6.0, None, None]]
    assert body["y_limits"] == [-0.5, 0.5, 1.5, 2.5]
    assert (body["n_points"], body["n_total"]) == (7, 9)


def test_binned_explicit_width_and_height_override_the_grid(client):
    write_grid_scan(client, "chosen", 9)

    body = client.context.http_client.get(
        "/api/v1/binned/chosen", params={"width": 1, "height": 1}
    ).json()

    assert body["RedTotal"] == [[4.0]]


def test_binned_without_a_spec_still_reports_no_total(client):
    write_step_scan(client)

    body = client.context.http_client.get("/api/v1/binned/scan").json()

    assert body["n_total"] is None


# --- sharing work between polls ------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_binned_cache(monkeypatch):
    """Start every test with an empty cache, switched off unless the test opts in."""
    visr._recent.clear()
    visr._inflight.clear()
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.0)


class _Counting:
    """An async compute() that records how often it ran."""

    def __init__(self, result=(b"{}", '"e"'), delay=0.05, error=None):
        self.calls = 0
        self.result, self.delay, self.error = result, delay, error

    async def __call__(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def test_concurrent_identical_requests_share_one_computation(monkeypatch):
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.5)
    compute = _Counting()

    async def scenario():
        return await asyncio.gather(
            *(visr._shared(("uid", ()), compute) for _ in range(8))
        )

    results = asyncio.run(scenario())

    assert compute.calls == 1
    assert results == [compute.result] * 8


def test_a_result_is_reused_for_a_moment_and_then_recomputed(monkeypatch):
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.1)
    compute = _Counting(delay=0)

    async def scenario():
        await visr._shared(("uid", ()), compute)
        await visr._shared(("uid", ()), compute)
        assert compute.calls == 1
        await asyncio.sleep(0.12)
        await visr._shared(("uid", ()), compute)

    asyncio.run(scenario())

    assert compute.calls == 2


def test_different_requests_do_not_share(monkeypatch):
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.5)
    compute = _Counting(delay=0)

    async def scenario():
        await visr._shared(("a", ()), compute)
        await visr._shared(("b", ()), compute)
        await visr._shared(("a", (("width", "3"),)), compute)

    asyncio.run(scenario())

    assert compute.calls == 3


def test_errors_reach_everyone_waiting_but_are_not_kept(monkeypatch):
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.5)
    failing = _Counting(error=HTTPException(status_code=503, detail="not ready"))
    working = _Counting(delay=0)

    async def scenario():
        outcomes = await asyncio.gather(
            *(visr._shared(("uid", ()), failing) for _ in range(3)),
            return_exceptions=True,
        )
        assert all(
            isinstance(o, HTTPException) and o.status_code == 503 for o in outcomes
        )
        return await visr._shared(("uid", ()), working)  # the error wasn't cached

    assert asyncio.run(scenario()) == working.result
    assert failing.calls == 1 and working.calls == 1


def test_a_cache_time_of_zero_turns_sharing_off():
    compute = _Counting(delay=0)

    async def scenario():
        await visr._shared(("uid", ()), compute)
        await visr._shared(("uid", ()), compute)

    asyncio.run(scenario())

    assert compute.calls == 2


def test_binned_has_an_etag_and_answers_304_when_nothing_changed(client, monkeypatch):
    monkeypatch.setattr(visr, "BINNED_CACHE_SECONDS", 0.5)
    write_grid_scan(client, "grid", 9)
    http = client.context.http_client

    first = http.get("/api/v1/binned/grid")
    second = http.get(
        "/api/v1/binned/grid", headers={"If-None-Match": first.headers["etag"]}
    )

    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    assert second.status_code == 304 and second.content == b""
    assert second.headers["etag"] == first.headers["etag"]


def test_binned_etag_changes_when_the_image_does(client):
    write_grid_scan(client, "short", 7)
    write_grid_scan(client, "full", 9)
    http = client.context.http_client

    short = http.get("/api/v1/binned/short").headers["etag"]
    full = http.get("/api/v1/binned/full").headers["etag"]

    assert short != full
    stale = http.get("/api/v1/binned/full", headers={"If-None-Match": short})
    assert stale.status_code == 200


# --- the axis forms real runs store ---


@pytest.mark.parametrize(
    ("stored", "name"),
    [
        ("sample_stage-x", "sample_stage-x"),
        ('Motor(name="sample_stage-x")', "sample_stage-x"),
        ("Motor(name='sample_stage-y')", "sample_stage-y"),
        ("<ophyd_async.epics.motor.Motor object at 0x7f797c058590>", None),
    ],
)
def test_axis_name_reads_the_name_out_of_the_forms_runs_store(stored, name):
    assert visr._axis_name(stored) == name


def test_setpoints_of_runs_with_named_reprs_need_no_motors_list():
    # recent runs store Motor(name=...) and fly scans have no motors list
    spec = Line('Motor(name="sample_stage-y")', 0, 1, 2) * Line(
        'Motor(name="sample_stage-x")', 0, 10, 3
    )

    x, y, _ = _setpoints(spec)

    assert x.tolist() == [0, 5, 10, 0, 5, 10]
    assert y.tolist() == [0, 0, 0, 1, 1, 1]


def test_grid_of_runs_with_named_reprs_needs_no_motors_list():
    spec = Line('Motor(name="sample_stage-y")', 0, 2, 3) * ~Line(
        'Motor(name="sample_stage-x")', 0, 10, 3
    )

    x_edges, y_edges, n_total, *_ = _grid(spec)

    assert x_edges.tolist() == [-2.5, 2.5, 7.5, 12.5]
    assert y_edges.tolist() == [-0.5, 0.5, 1.5, 2.5]
    assert n_total == 9


def write_offset_scan(client, uid, x_shift, y_shift, x_scale=1.0, n_points=9):
    """A 3x3 raster whose recorded positions are offset (and scaled) from the spec."""
    spec = Line("Y", 0, 2, 3) * Line("X", 0, 2, 3)
    metadata = {"start": {"spec": spec.serialize()}}
    primary = client.create_container(uid, metadata=metadata).create_container(
        "primary"
    )
    x = numpy.array([0.0, 1.0, 2.0] * 3) * x_scale + x_shift
    y = numpy.repeat([0.0, 1.0, 2.0], 3) + y_shift
    primary.write_array(x[:n_points], key="X")
    primary.write_array(y[:n_points], key="Y")
    for channel in ("RedTotal", "GreenTotal", "BlueTotal"):
        primary.write_array(numpy.arange(9.0)[:n_points], key=channel)


def test_binned_shifts_offset_positions_onto_the_scan_grid(client):
    # fly scans currently record positions offset from their setpoints
    write_offset_scan(client, "offset", x_shift=1.0, y_shift=10.4)

    body = client.context.http_client.get("/api/v1/binned/offset").json()

    assert body["RedTotal"] == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0], [6.0, 7.0, 8.0]]
    assert body["x_limits"] == [-0.5, 0.5, 1.5, 2.5]  # the spec's frame
    assert body["position_offset"] == pytest.approx({"x": 1.0, "y": 10.4})


def test_binned_aligns_from_the_first_points_of_an_offset_scan(client):
    write_offset_scan(client, "early", x_shift=1.0, y_shift=10.4, n_points=2)

    body = client.context.http_client.get("/api/v1/binned/early").json()

    assert body["RedTotal"] == [
        [0.0, 1.0, None],
        [None, None, None],
        [None, None, None],
    ]
    assert body["position_offset"] == pytest.approx({"x": 1.0, "y": 10.4})


def test_binned_keeps_the_default_binning_when_no_offset_explains_the_positions(
    client,
):
    write_offset_scan(client, "scaled", x_shift=1.0, y_shift=10.4, x_scale=3.0)

    body = client.context.http_client.get("/api/v1/binned/scaled").json()

    assert len(body["RedTotal"]) == 10 and len(body["RedTotal"][0]) == 10
    assert body["position_offset"] is None
    assert body["n_total"] == 9  # still known from the spec


def test_positions_that_already_fit_are_not_shifted(client):
    write_grid_scan(client, "fits", 9)

    body = client.context.http_client.get("/api/v1/binned/fits").json()

    assert body["position_offset"] is None


def test_alignment_offset_ignores_a_dropped_point():
    grid = _grid(Line("y", 0, 4, 5) * Line("x", 0, 4, 5))
    x = grid.x_setpoints + 1.0
    y = grid.y_setpoints + 10.4
    # one point lost near the end shifts the last few points by one place
    x, y = numpy.delete(x, 21), numpy.delete(y, 21)

    aligned = visr.align_to_setpoints(x, y, grid)

    assert aligned is not None
    assert aligned[2] == pytest.approx((1.0, 10.4))


def test_positions_fit_grid():
    edges = numpy.array([-0.5, 0.5, 1.5, 2.5])
    on = numpy.array([0.0, 1.0, 2.0])

    assert visr.positions_fit_grid(on, on, edges, edges)
    assert not visr.positions_fit_grid(on + 10, on, edges, edges)
    assert visr.positions_fit_grid(numpy.array([]), numpy.array([]), edges, edges)
