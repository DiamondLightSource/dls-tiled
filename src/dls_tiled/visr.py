"""ViSR plugin: server-side 2-D binning of scan data.

Enable it in the tiled config with ``routers: [dls_tiled.visr:visr_router]``.
"""

import asyncio
import enum
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import anyio.to_thread
import numpy
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
)
from h5py._hl.dataset import Dataset as H5Dataset
from scanspec.core import stack2dimension
from scanspec.specs import Spec
from starlette.status import (
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)
from tiled.adapters.utils import DataNotReadyError
from tiled.server.authentication import (  # type: ignore
    check_scopes,
    get_current_access_tags,
    get_current_principal,
    get_current_scopes,
    get_session_state,
)
from tiled.server.core import NoEntry
from tiled.server.dependencies import get_root_tree  # type: ignore
from tiled.server.schemas import Principal
from tiled.type_aliases import AccessTags, Scopes

from dls_tiled import rendering

logger = logging.getLogger(__name__)

CROSS_CHANNEL_RETRIES = int(os.getenv("DLS_TILED_VISR_CROSS_CHANNEL_RETRIES", "5"))
CROSS_CHANNEL_RETRY_DELAY = float(
    os.getenv("DLS_TILED_VISR_CROSS_CHANNEL_RETRY_DELAY", "0.1")
)
# Tiled raises DataNotReadyError when the file on disk has fewer frames than the
# catalog advertises, i.e. a live write hasn't landed yet. It is retryable.
NOT_READY_RETRIES = int(os.getenv("DLS_TILED_VISR_NOT_READY_RETRIES", "3"))
NOT_READY_RETRY_DELAY = float(os.getenv("DLS_TILED_VISR_NOT_READY_RETRY_DELAY", "0.2"))
RETRY_AFTER_SECONDS = 1


class ScanType(enum.Enum):
    StepScan = "StepScan"
    FlyScan = "FlyScan"


visr_router = APIRouter()


BINNED_CACHE_SECONDS = float(os.getenv("DLS_TILED_VISR_CACHE_SECONDS", "0.5"))
_recent: dict[tuple, tuple[float, tuple]] = {}
_inflight: dict[tuple, asyncio.Future] = {}


async def _shared(key: tuple, compute):
    """Await ``compute()`` once for concurrent callers with the same key, and reuse
    its result for ``BINNED_CACHE_SECONDS``. Errors are shared with the callers
    waiting on that call but never kept. A cache time of 0 turns this off."""
    if BINNED_CACHE_SECONDS <= 0:
        return await compute()
    now = time.monotonic()
    hit = _recent.get(key)
    if hit is not None and now - hit[0] < BINNED_CACHE_SECONDS:
        return hit[1]
    pending = _inflight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    # Mark any error as retrieved, so a call nobody else waited on doesn't warn.
    future.add_done_callback(lambda f: f.cancelled() or f.exception())
    _inflight[key] = future
    try:
        result = await compute()
    except BaseException as error:
        future.set_exception(error)
        raise
    else:
        future.set_result(result)
        for stale in [
            k for k, (t, _) in _recent.items() if now - t > BINNED_CACHE_SECONDS
        ]:
            del _recent[stale]
        _recent[key] = (time.monotonic(), result)
        return result
    finally:
        _inflight.pop(key, None)


async def get_data(root, segments) -> H5Dataset | numpy.ndarray | dict:
    try:
        adapter = await root.lookup_adapter(segments)
    except Exception:
        raise

    adapter_type = type(adapter).__name__

    if hasattr(adapter, "keys_range"):
        try:
            keys = await adapter.keys_range(0, 100)
            return {"adapter_type": adapter_type, "children": list(keys)}
        except Exception:
            raise
    else:
        # Leaf node - read it. A file that is still behind the catalog is retried
        # briefly, then reported as 503 with Retry-After (as Tiled's own routes
        # do) rather than as an error the client can't tell from a bad request.
        for attempt in range(NOT_READY_RETRIES + 1):
            try:
                if inspect.iscoroutinefunction(adapter.read):
                    return await adapter.read()
                return await anyio.to_thread.run_sync(adapter.read)
            except DataNotReadyError as err:
                if attempt == NOT_READY_RETRIES:
                    raise HTTPException(
                        status_code=HTTP_503_SERVICE_UNAVAILABLE,
                        detail=str(err),
                        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                    ) from None
                logger.info(
                    "Data for %s not ready (attempt %d/%d): %s",
                    "/".join(segments),
                    attempt + 1,
                    NOT_READY_RETRIES + 1,
                    err,
                )
                await anyio.sleep(NOT_READY_RETRY_DELAY)
        raise AssertionError("unreachable")  # pragma: no cover


async def fill_data(root, segments, shape=None, fill_value=numpy.nan):
    try:
        return await get_data(root, segments)
    except NoEntry:
        if shape is None:
            raise
        return numpy.full(shape, fill_value)


def _channel_length(array: H5Dataset | numpy.ndarray | dict) -> int:
    assert isinstance(array, (H5Dataset, numpy.ndarray))
    return array.shape[-1]


_AXIS_LETTERS = ("x", "y", "z")


def _axis_letter(name: object) -> str | None:
    """Return "x", "y" or "z" if the axis name ends in one (e.g. "sample_stage-x")."""
    if not isinstance(name, str):
        return None
    letter = re.split(r"[-_.]", name.lower())[-1]
    return letter if letter in _AXIS_LETTERS else None


_REPR_NAME_RE = re.compile(r"""name=["']([^"']+)["']""")


def _axis_name(axis: object) -> str | None:
    """The device name of a spec axis, or None if the spec doesn't carry one.

    A spec axis is whatever the plan passed in: a plain name, an ophyd-async
    repr that carries the name (``Motor(name="sample_stage-x")``), or, in older
    runs, an object repr with no name (``<... Motor object at 0x7f...>``).
    """
    if not isinstance(axis, str):
        return None
    named = _REPR_NAME_RE.search(axis)
    if named:
        return named.group(1)
    return None if axis.startswith("<") else axis


def _axis_letters(axes: list, motors: object) -> list[str | None]:
    """The x/y/z letter of each spec axis, or None where it can't be told.

    The names come from the spec's axes. Where the spec has none (older runs
    store an object repr) the run's ``motors`` list, which is in the same order
    as the spec's axes, supplies them.
    """
    names = [_axis_name(a) for a in axes]
    if None in names and isinstance(motors, list) and len(motors) == len(axes):
        names = list(motors)
    return [_axis_letter(n) for n in names]


def _xyz_order(axes: list, motors: object) -> list[int]:
    """Indices that put a spec's axes in x, y, z order.

    A spec lists its axes outermost dimension first, which is not necessarily x
    first (a raster scan has y as the outer axis). If the axes cannot be told
    apart as x, y and z, keep the spec's own order.
    """
    letters = _axis_letters(axes, motors)
    if None in letters or len(set(letters)) != len(letters):
        return list(range(len(axes)))
    return sorted(range(len(axes)), key=lambda i: _AXIS_LETTERS.index(letters[i]))


def _bin_edges(centres: numpy.ndarray) -> numpy.ndarray:
    """Edges halfway between consecutive centres, extended half a step at each end."""
    if len(centres) == 1:
        return numpy.array([centres[0] - 0.5, centres[0] + 0.5])
    mids = (centres[:-1] + centres[1:]) / 2
    first = centres[0] - (centres[1] - centres[0]) / 2
    last = centres[-1] + (centres[-1] - centres[-2]) / 2
    return numpy.concatenate([[first], mids, [last]])


# Past this many cells the native-resolution image is too much to send on every
# poll, so the caller falls back to the coarse default binning.
MAX_GRID_CELLS = int(os.getenv("DLS_TILED_VISR_MAX_GRID_CELLS", "100000"))
# The ViSR plot needs at least three points on each axis, so a scan narrower than
# that keeps the previous default binning.
MIN_CELLS_PER_AXIS = 3


class ScanGrid(NamedTuple):
    """A scan's setpoint grid: bin edges on x and y, the point count, and each
    point's x and y setpoint in scan order."""

    x_edges: numpy.ndarray
    y_edges: numpy.ndarray
    n_total: int
    x_setpoints: numpy.ndarray
    y_setpoints: numpy.ndarray


@functools.lru_cache(maxsize=32)
def _grid_from_spec(spec_json: str, motors_json: str) -> ScanGrid | None:
    """Bin edges for x and y and the point count of the scan the spec describes.

    The grid is the set of distinct setpoint positions on each axis, so every
    setpoint falls in its own cell and the cells' edges lie halfway between
    neighbours. Returns None when the spec has no x and y axes that can be
    identified, or doesn't lay points out on a grid (a spiral, say), where the
    caller falls back to binning the positions as they come. Cached: the spec
    of a run never changes, and this is asked for on every poll.
    """
    spec = Spec.deserialize(json.loads(spec_json))
    by_axis = stack2dimension(spec.calculate()).midpoints
    axes = list(by_axis)
    letters = _axis_letters(axes, json.loads(motors_json))
    if "x" not in letters or "y" not in letters or len(set(letters)) != len(letters):
        return None
    n_total = len(by_axis[axes[0]])
    x_setpoints = numpy.asarray(by_axis[axes[letters.index("x")]], dtype=float)
    y_setpoints = numpy.asarray(by_axis[axes[letters.index("y")]], dtype=float)
    xs = numpy.unique(numpy.round(x_setpoints, 9))
    ys = numpy.unique(numpy.round(y_setpoints, 9))
    if min(len(xs), len(ys)) < MIN_CELLS_PER_AXIS:
        return None  # a line, or too few cells for the plot's axes
    if len(xs) * len(ys) > min(n_total, MAX_GRID_CELLS):
        return None  # more cells than points: not a grid
    return ScanGrid(_bin_edges(xs), _bin_edges(ys), n_total, x_setpoints, y_setpoints)


# Share of the recorded positions that must lie inside the spec's grid for the
# grid to be used for binning them.
MIN_POSITIONS_ON_GRID = 0.9


def positions_fit_grid(x, y, x_edges, y_edges, uid="") -> bool:
    """Whether the recorded positions lie on the spec's grid.

    The grid is in the spec's coordinates. Positions recorded in another frame
    (fly scans currently record positions offset from their setpoints) would
    leave it empty; the caller then tries ``align_to_setpoints`` and otherwise
    keeps the previous binning.
    """
    x, y = numpy.asarray(x, dtype=float), numpy.asarray(y, dtype=float)
    if x.size == 0:
        return True  # nothing received yet: the grid is all there is to show
    inside = (
        (x >= x_edges[0]) & (x <= x_edges[-1]) & (y >= y_edges[0]) & (y <= y_edges[-1])
    )
    share = float(inside.mean())
    if share < MIN_POSITIONS_ON_GRID:
        logger.info(
            "Recorded positions for '%s' lie outside the spec's grid "
            "(%.0f%% inside x %s..%s, y %s..%s); keeping the default binning",
            uid,
            100 * share,
            x_edges[0],
            x_edges[-1],
            y_edges[0],
            y_edges[-1],
        )
        return False
    return True


def align_to_setpoints(x, y, grid: ScanGrid):
    """Shift recorded positions into the spec's frame by a constant offset.

    Fly scans currently record positions that are offset from their setpoints by
    a constant amount per axis (the encoder capture isn't in the motors' user
    coordinates). Each point's offset is its recorded position minus its own
    setpoint, matched by index; the median of those is the axis offset, so a few
    dropped or late points don't move it. Returns the shifted positions and the
    offset ``(dx, dy)``, or None when the points don't sit within half a cell of
    their setpoints after shifting (the frames differ by more than an offset).
    """
    x, y = numpy.asarray(x, dtype=float), numpy.asarray(y, dtype=float)
    n = min(len(x), len(grid.x_setpoints))
    if n == 0:
        return None
    dx = float(numpy.median(x[:n] - grid.x_setpoints[:n]))
    dy = float(numpy.median(y[:n] - grid.y_setpoints[:n]))
    half_x = float(numpy.min(numpy.diff(grid.x_edges))) / 2
    half_y = float(numpy.min(numpy.diff(grid.y_edges))) / 2
    residual_x = numpy.median(numpy.abs(x[:n] - dx - grid.x_setpoints[:n]))
    residual_y = numpy.median(numpy.abs(y[:n] - dy - grid.y_setpoints[:n]))
    if residual_x > half_x or residual_y > half_y:
        return None
    return x - dx, y - dy, (dx, dy)


async def get_scan_grid(root, uid):
    """The run's setpoint grid ``(x_edges, y_edges, n_total)``, or None."""
    try:
        adapter = await root.lookup_adapter([uid])
        start = adapter.metadata()["start"]
        return _grid_from_spec(
            json.dumps(start["spec"]), json.dumps(start.get("motors"))
        )
    except Exception as e:
        logger.debug("No usable scan grid for '%s': %s", uid, e)
        return None


async def get_setpoints(root, uid):
    """Return setpoints from the bluesky start document stored in a node's metadata."""
    adapter = await root.lookup_adapter([uid])
    metadata = adapter.metadata()
    try:
        start = metadata["start"]
        spec = Spec.deserialize(start["spec"])
    except KeyError as e:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Could not find 'start.spec' in metadata for '{uid}': {e}",
        ) from None

    by_axis = stack2dimension(spec.calculate()).midpoints
    axes = list(by_axis)
    midpoints = [by_axis[axes[i]] for i in _xyz_order(axes, start.get("motors"))]
    x = midpoints[0]
    y = midpoints[1] if len(midpoints) > 1 else numpy.full(x.shape, numpy.nan)
    z = midpoints[2] if len(midpoints) > 2 else numpy.full(x.shape, numpy.nan)
    return numpy.array([x, y, z])


async def _fetch_readback_x(root, uid) -> tuple[H5Dataset | numpy.ndarray, ScanType]:
    try:
        x = await get_data(root, [uid, "primary", "internal", "sample_stage-x"])
        scan_type = ScanType.FlyScan
    except NoEntry:
        try:
            x = await get_data(root, [uid, "primary", "sample_stage-x"])
            scan_type = ScanType.FlyScan
        except NoEntry:
            x = await get_data(root, [uid, "primary", "X"])
            scan_type = ScanType.StepScan
    assert isinstance(x, (H5Dataset, numpy.ndarray))
    return x, scan_type


async def _fetch_readback_yz(root, uid, scan_type, x_shape):
    if scan_type == ScanType.FlyScan:
        try:
            y = await get_data(root, [uid, "primary", "internal", "sample_stage-y"])
        except NoEntry:
            y = await fill_data(root, [uid, "primary", "sample_stage-y"], x_shape)
        try:
            z = await get_data(root, [uid, "primary", "internal", "sample_stage-z"])
        except NoEntry:
            z = await fill_data(root, [uid, "primary", "sample_stage-z"], x_shape)
    else:
        y = await fill_data(root, [uid, "primary", "Y"], x_shape)
        z = await fill_data(root, [uid, "primary", "Z"], x_shape)
    return y, z


async def get_readbacks(root, uid, readback_x):
    """
    Utility function to load readback positions (x, y, z) and detect scan type.

    Args:
        root: The root tree to fetch data from.
        uid: Unique identifier for the dataset.
        readback_x: The x readback data.

    Returns:
        A tuple containing:
            - A numpy array with readback positions (x, y, z).
            - The detected scan type (FlyScan or StepScan).
    """
    try:
        readback_x, scan_type = await _fetch_readback_x(root, uid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"Could not find readback data for '{uid = }': {e}"),
        ) from None

    assert isinstance(readback_x, H5Dataset) or isinstance(readback_x, numpy.ndarray)
    readback_y, readback_z = await _fetch_readback_yz(
        root, uid, scan_type, readback_x.shape
    )

    # x, y, and z are each fetched independently and can race a live write,
    # landing at different lengths. Target the longest length seen on the
    # first read and retry re-fetching all three until they reach it,
    # falling back to truncating to the shortest if they don't converge
    # within the retry budget -- same strategy as the cross-channel
    # reconciliation in binned().
    lengths = {
        "x": _channel_length(readback_x),
        "y": _channel_length(readback_y),
        "z": _channel_length(readback_z),
    }
    target_len = max(lengths.values())
    for attempt in range(CROSS_CHANNEL_RETRIES):
        if min(lengths.values()) >= target_len:
            break
        logger.info(
            "Cross-axis length mismatch for '%s' (attempt %d/%d): %s, "
            "waiting to reach %d",
            uid,
            attempt + 1,
            CROSS_CHANNEL_RETRIES,
            lengths,
            target_len,
        )
        await anyio.sleep(CROSS_CHANNEL_RETRY_DELAY)
        try:
            new_x, new_scan_type = await _fetch_readback_x(root, uid)
            new_y, new_z = await _fetch_readback_yz(
                root, uid, new_scan_type, new_x.shape
            )
        except Exception as e:
            logger.info(
                "Re-fetch failed while reconciling readback lengths for '%s': %s",
                uid,
                e,
            )
            break
        readback_x, readback_y, readback_z, scan_type = (
            new_x,
            new_y,
            new_z,
            new_scan_type,
        )
        lengths = {
            "x": _channel_length(readback_x),
            "y": _channel_length(readback_y),
            "z": _channel_length(readback_z),
        }
    else:
        logger.info(
            "Giving up on cross-axis reconciliation for '%s' after %d attempt(s), "
            "falling back to shortest: %s",
            uid,
            CROSS_CHANNEL_RETRIES,
            lengths,
        )

    common_len = min(target_len, min(lengths.values()))
    if any(length != common_len for length in lengths.values()):
        readback_x = readback_x[:common_len]
        readback_y = readback_y[:common_len]
        readback_z = readback_z[:common_len]

    return numpy.array([readback_x, readback_y, readback_z]), scan_type


@visr_router.get("/binned/{path:path}")
async def binned(  # type: ignore
    path: str,
    request: Request,
    x_dim_index: int = 0,
    y_dim_index: int = 1,
    xmin: float | None = None,
    xmax: float | None = None,
    ymin: float | None = None,
    ymax: float | None = None,
    width: int | None = None,
    height: int | None = None,
    setpoints: bool = False,
    slice_dim: list[str] | None = Query(  # noqa: B008
        None, description="Repeatable: dim:center:thickness"
    ),
    principal: Principal | None = Depends(get_current_principal),  # type: ignore  # noqa: B008
    root_tree=Depends(get_root_tree),  # type: ignore  # noqa: B008
    session_state: dict = Depends(get_session_state),  # type: ignore  # noqa: B008
    authn_access_tags: AccessTags | None = Depends(get_current_access_tags),  # type: ignore  # noqa: B008
    authn_scopes: Scopes = Depends(get_current_scopes),  # type: ignore  # noqa: B008
    _=Security(check_scopes, scopes=["read:data"]),  # noqa: B008
):
    """Fetch a folded representation of an array dataset.

    Returns ``RedTotal``/``GreenTotal``/``BlueTotal`` as matrices with one row per
    y bin and one column per x bin (the layout image plots expect), ``null`` for
    bins no point fell into, ``x_limits``/``y_limits`` as the bin edges (one more
    than the columns/rows), ``n_points``, the number of scan points used, and
    ``n_total``, the number the scan will have (``null`` if the run has no spec),
    and ``position_offset``: ``{"x": dx, "y": dy}`` when the recorded positions
    were shifted onto the spec's grid (see ``align_to_setpoints``), else ``null``.

    Unless ``width``/``height`` or a full range are given, bins follow the scan's
    setpoint grid when its spec describes one: one cell per setpoint, a range
    fixed from the first poll.

    Args:
        x_dim_index: Index into the position array to use as the x axis (default 0).
        y_dim_index: Index into the position array to use as the y axis (default 1).
        xmin: Lower bound of the x histogram range. When combined with xmax, ymin, and
            ymax, passed as the ``range`` argument to ``numpy.histogram2d``.
        xmax: Upper bound of the x histogram range.
        ymin: Lower bound of the y histogram range.
        ymax: Upper bound of the y histogram range.
        width: Number of bins along the x axis. Requires ``height`` to also be set.
        height: Number of bins along the y axis. Requires ``width`` to also be set.
        setpoints: If ``True``, derive positions from the ScanSpec setpoints stored in
            the run's start document rather than from the recorded readback values.
        slice_dim: Repeatable query parameter that restricts which data points
            contribute to the image by filtering along a dimension that is neither x
            nor y.  Each value must be formatted as ``dim:center:thickness``, where
            *dim* is the integer dimension index, *center* is the centre of the slice,
            and *thickness* is the half-width (points with
            ``|position - center| <= thickness`` are kept).
    """
    root = request.app.state.root_tree
    segments = [s for s in path.strip("/").split("/") if s]
    uid = segments[0]

    # A UI polls this several times a second per client while points arrive only
    # every couple of seconds, so identical requests share one computation (and
    # one read of the files) and its result is reused for a moment.
    key = (uid, tuple(sorted(request.query_params.multi_items())))

    async def compute():
        output = await _binned_output(
            root,
            uid,
            x_dim_index,
            y_dim_index,
            xmin,
            xmax,
            ymin,
            ymax,
            width,
            height,
            setpoints,
            slice_dim,
        )
        payload = json.dumps(output, separators=(",", ":")).encode()
        return payload, f'"{hashlib.sha1(payload).hexdigest()[:20]}"'

    payload, etag = await _shared(key, compute)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/json", headers=headers)


class ScanPoints(NamedTuple):
    """The points of a run ready to draw: each channel's values and their positions."""

    data: dict
    x: numpy.ndarray
    y: numpy.ndarray
    n_points: int


async def load_points(root, uid, x_dim_index, y_dim_index, setpoints, slice_dim):
    """Read a run's channels and positions, reconciled to one length and sliced.

    Positions are the recorded readbacks, or the spec's setpoints with
    ``setpoints``; ``n_points`` counts the points before ``slice_dim`` is applied.
    """
    try:
        red_total = await get_data(root, [uid, "primary", "RedTotal"])
        green_total = await get_data(root, [uid, "primary", "GreenTotal"])
        blue_total = await get_data(root, [uid, "primary", "BlueTotal"])
        data = {
            "RedTotal": red_total,
            "GreenTotal": green_total,
            "BlueTotal": blue_total,
        }
        assert isinstance(red_total, H5Dataset) or isinstance(red_total, numpy.ndarray)
        assert isinstance(green_total, H5Dataset) or isinstance(
            green_total, numpy.ndarray
        )
        assert isinstance(blue_total, H5Dataset) or isinstance(
            blue_total, numpy.ndarray
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"Could not find data channels for '{uid = }': {e}"),
        ) from None

    # Get positions either from setpoints (spec) or readbacks
    try:
        if setpoints:
            readbacks = await get_setpoints(root, uid)
        else:
            readbacks, _ = await get_readbacks(root, uid, None)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"Could not find position data for '{uid = }': {e}"),
        ) from None

    # The three totals and the readbacks are each fetched independently and can
    # race a live write, landing at different lengths. Target the longest length
    # seen on the first read (an indication the others will reach it shortly),
    # and retry re-fetching everything until they do. If they don't catch up
    # within the retry budget, fall back to truncating to whatever the shortest
    # currently is, so we always return a consistent (if possibly stale) image.
    # Setpoints come from the spec and are complete from the start, so they take
    # no part in this: waiting for the data to reach them would only ever time
    # out while a scan is running.
    def current_lengths() -> dict[str, int]:
        found = {
            "RedTotal": _channel_length(red_total),
            "GreenTotal": _channel_length(green_total),
            "BlueTotal": _channel_length(blue_total),
        }
        if not setpoints:
            found["readbacks"] = readbacks.shape[-1]
        return found

    lengths = current_lengths()
    target_len = max(lengths.values())
    for attempt in range(CROSS_CHANNEL_RETRIES):
        if min(lengths.values()) >= target_len:
            break
        logger.info(
            "Cross-channel length mismatch for '%s' (attempt %d/%d): %s, "
            "waiting to reach %d",
            uid,
            attempt + 1,
            CROSS_CHANNEL_RETRIES,
            lengths,
            target_len,
        )
        await anyio.sleep(CROSS_CHANNEL_RETRY_DELAY)
        try:
            new_red_total = await get_data(root, [uid, "primary", "RedTotal"])
            new_green_total = await get_data(root, [uid, "primary", "GreenTotal"])
            new_blue_total = await get_data(root, [uid, "primary", "BlueTotal"])
            if setpoints:
                new_readbacks = readbacks
            else:
                new_readbacks, _ = await get_readbacks(root, uid, None)
        except Exception as e:
            logger.info(
                "Re-fetch failed while reconciling channel lengths for '%s': %s",
                uid,
                e,
            )
            break
        # Only commit the re-fetch once all four succeed together, so
        # red_total/green_total/blue_total/readbacks and `lengths` never
        # drift out of sync with each other.
        red_total, green_total, blue_total, readbacks = (
            new_red_total,
            new_green_total,
            new_blue_total,
            new_readbacks,
        )
        lengths = current_lengths()
    else:
        logger.info(
            "Giving up on cross-channel reconciliation for '%s' after %d attempt(s), "
            "falling back to shortest: %s",
            uid,
            CROSS_CHANNEL_RETRIES,
            lengths,
        )

    common_len = min(target_len, min(lengths.values()), readbacks.shape[-1])
    if any(length != common_len for length in lengths.values()) or (
        readbacks.shape[-1] != common_len
    ):
        red_total = red_total[:common_len]
        green_total = green_total[:common_len]
        blue_total = blue_total[:common_len]
        readbacks = readbacks[:, :common_len]

    data = {
        "RedTotal": red_total,
        "GreenTotal": green_total,
        "BlueTotal": blue_total,
    }
    n_points = int(readbacks.shape[-1])

    # mask out the points that lie outside the slice
    mask = numpy.ones(readbacks.shape[-1], dtype=bool)  # one entry per point
    if slice_dim is not None:
        for slice_spec in slice_dim:
            try:
                dim_str, center_str, thick_str = slice_spec.split(":")
                dim = int(dim_str)
                center = float(center_str)
                thickness = float(thick_str)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid slice_dim format: {slice_spec}."
                        " Expected dim:center:thickness"
                    ),
                ) from None
            if dim < 0 or dim >= readbacks.shape[0]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"slice_dim index {dim} is out of range"
                        f" (0-{readbacks.shape[0] - 1})"
                    ),
                )
            if dim in (x_dim_index, y_dim_index):
                raise HTTPException(
                    status_code=400,
                    detail=f"slice_dim cannot contain x or y dimension {dim}",
                )
            mask &= numpy.abs(readbacks[dim, :] - center) <= thickness

        readbacks = readbacks[:, mask]
        data = {channel: d[mask] for channel, d in data.items()}

    assert isinstance(red_total, (H5Dataset, numpy.ndarray))
    logger.debug(
        "readbacks shape %s, red_total shape %s (sliced=%s)",
        readbacks.shape,
        red_total.shape,
        slice_dim is not None,
    )

    return ScanPoints(
        data, readbacks[x_dim_index, :], readbacks[y_dim_index, :], n_points
    )


async def _binned_output(
    root,
    uid,
    x_dim_index,
    y_dim_index,
    xmin,
    xmax,
    ymin,
    ymax,
    width,
    height,
    setpoints,
    slice_dim,
) -> dict:
    """Compute the binned images for one run (the body of the /binned endpoint)."""
    data, x_positions, y_positions, n_points = await load_points(
        root, uid, x_dim_index, y_dim_index, setpoints, slice_dim
    )

    # bundle the kwargs
    histogram2d_kwargs: dict = {}
    explicit_bins = all(opt is not None for opt in (width, height))
    explicit_range = all(opt is not None for opt in (xmin, xmax, ymin, ymax))
    if explicit_bins:
        histogram2d_kwargs["bins"] = (width, height)
    if explicit_range:
        histogram2d_kwargs["range"] = ((xmin, xmax), (ymin, ymax))

    # Unless the caller chose the binning, use the scan's own setpoint grid: one
    # cell per setpoint and a range fixed by the spec from the first poll, rather
    # than a fixed 10x10 over whatever extent the points received so far happen
    # to cover (which rescales the whole image as the scan fills in).
    grid = await get_scan_grid(root, uid)
    n_total = grid.n_total if grid is not None else None
    position_offset = None
    if (
        grid is not None
        and not explicit_bins
        and not explicit_range
        and (x_dim_index, y_dim_index) == (0, 1)
    ):
        fits = positions_fit_grid(
            x_positions, y_positions, grid.x_edges, grid.y_edges, uid
        )
        if not fits:
            # Positions recorded in another frame (fly scans, for now): bring them
            # into the spec's frame if a constant offset per axis does it.
            aligned = align_to_setpoints(x_positions, y_positions, grid)
            if aligned is not None and positions_fit_grid(
                aligned[0], aligned[1], grid.x_edges, grid.y_edges, uid
            ):
                x_positions, y_positions, position_offset = aligned
                fits = True
                logger.info(
                    "Shifted the recorded positions of '%s' by %s to match its spec",
                    uid,
                    position_offset,
                )
        if fits:
            histogram2d_kwargs["bins"] = [grid.x_edges, grid.y_edges]

    binned_output: dict = {}
    for channel in ("RedTotal", "GreenTotal", "BlueTotal"):
        binned_channel = compute_binned_image(
            data[channel], x_positions, y_positions, **histogram2d_kwargs
        )
        binned_output[channel] = image_rows_are_y(
            binned_channel["img"], binned_channel["counts"]
        )
    binned_output["x_limits"] = binned_channel["x"].tolist()
    binned_output["y_limits"] = binned_channel["y"].tolist()
    binned_output["n_points"] = n_points
    binned_output["n_total"] = n_total
    binned_output["position_offset"] = (
        None
        if position_offset is None
        else {"x": position_offset[0], "y": position_offset[1]}
    )

    return binned_output


RENDER_KINDS = ("cells", "rectangles", "gaussian", "ovals")
MAX_RENDER_SIZE = 2048


def _nominal_step(grid: ScanGrid) -> tuple[float, float]:
    """The scan's step on x and y: the median width of its grid cells."""
    return float(numpy.median(numpy.diff(grid.x_edges))), float(
        numpy.median(numpy.diff(grid.y_edges))
    )


def _beam_from_metadata(start: dict) -> tuple[float, float] | None:
    """Beam size (FWHM) from the start document's ``beam_size``, if it has one:
    a single number, or ``{"x": ..., "y": ...}``."""
    beam = start.get("beam_size")
    if isinstance(beam, (int, float)):
        return float(beam), float(beam)
    if isinstance(beam, dict) and {"x", "y"} <= set(beam):
        return float(beam["x"]), float(beam["y"])
    return None


@visr_router.get("/render/{path:path}")
async def render(  # type: ignore
    path: str,
    request: Request,
    kind: str = "cells",
    x0: float | None = None,
    x1: float | None = None,
    y0: float | None = None,
    y1: float | None = None,
    size: int = 256,
    setpoints: bool = False,
    cell_x: float | None = None,
    cell_y: float | None = None,
    beam_x: float | None = None,
    beam_y: float | None = None,
    format: str = "json",
    channel: str = "RedTotal",
    vmin: float | None = None,
    vmax: float | None = None,
    line_width: float = 1.5,
    line_opacity: float = 0.35,
    slice_dim: list[str] | None = Query(None),  # noqa: B008
    principal: Principal | None = Depends(get_current_principal),  # type: ignore  # noqa: B008
    root_tree=Depends(get_root_tree),  # type: ignore  # noqa: B008
    session_state: dict = Depends(get_session_state),  # type: ignore  # noqa: B008
    authn_access_tags: AccessTags | None = Depends(get_current_access_tags),  # type: ignore  # noqa: B008
    authn_scopes: Scopes = Depends(get_current_scopes),  # type: ignore  # noqa: B008
    _=Security(check_scopes, scopes=["read:data"]),  # noqa: B008
):
    """Draw a run's points into an image for a viewport (prototype).

    ``kind`` picks how each point is drawn: ``cells`` (the scan's grid cells),
    ``rectangles`` (a nominal-size cell centred on each recorded position),
    ``gaussian`` (a beam-sized Gaussian per point) or ``ovals`` (a beam-sized
    oval per point with a faint white outline, ``line_width`` pixels at
    ``line_opacity``; overlaps are split between the ovals rather than blended,
    see ``rendering.oval_layout``). The beam size (FWHM) comes from
    ``beam_x``/``beam_y``, else the start document's ``beam_size``, else the
    scan's step. The viewport
    is ``x0``..``x1`` by ``y0``..``y1`` in data coordinates (default: the scan's
    extent) at ``size`` pixels along its longer side.

    ``format=json`` returns every channel as rows (row 0 at ``y0``, ``null``
    where nothing is drawn) plus a ``coverage`` layer (0 to 1). ``format=png``
    returns one ``channel`` as a tinted RGBA image, transparent where nothing is
    drawn, scaled to ``vmin``/``vmax`` if given (else to its own range). For
    ``ovals``, JSON adds an ``outline`` layer (opacity of the white lines) and
    the PNG has the lines drawn in.
    """
    if kind not in RENDER_KINDS:
        raise HTTPException(
            status_code=400, detail=f"kind must be one of {RENDER_KINDS}"
        )
    if format not in ("json", "png"):
        raise HTTPException(status_code=400, detail="format must be json or png")
    if format == "png" and channel not in rendering.CHANNEL_COLOUR:
        raise HTTPException(
            status_code=400,
            detail=f"channel must be one of {tuple(rendering.CHANNEL_COLOUR)}",
        )
    if not 1 <= size <= MAX_RENDER_SIZE:
        raise HTTPException(
            status_code=400, detail=f"size must be 1..{MAX_RENDER_SIZE}"
        )
    if not (0 < line_width <= 20 and 0 <= line_opacity <= 1):
        raise HTTPException(
            status_code=400,
            detail="line_width must be in (0, 20], line_opacity in [0, 1]",
        )
    root = request.app.state.root_tree
    uid = [s for s in path.strip("/").split("/") if s][0]
    key = ("render", uid, tuple(sorted(request.query_params.multi_items())))

    async def compute():
        points = await load_points(root, uid, 0, 1, setpoints, slice_dim)
        grid = await get_scan_grid(root, uid)
        x, y, offset = points.x, points.y, None
        if (
            grid is not None
            and not setpoints
            and not positions_fit_grid(x, y, grid.x_edges, grid.y_edges, uid)
        ):
            aligned = align_to_setpoints(x, y, grid)
            if aligned is not None and positions_fit_grid(
                aligned[0], aligned[1], grid.x_edges, grid.y_edges, uid
            ):
                x, y, offset = aligned

        step = _nominal_step(grid) if grid is not None else None
        if grid is not None:
            extent = (
                grid.x_edges[0],
                grid.x_edges[-1],
                grid.y_edges[0],
                grid.y_edges[-1],
            )
        elif len(x):
            extent = (float(x.min()), float(x.max()), float(y.min()), float(y.max()))
        else:
            extent = (0.0, 1.0, 0.0, 1.0)
        bounds = [
            v if v is not None else e
            for v, e in zip((x0, x1, y0, y1), extent, strict=True)
        ]
        if bounds[0] == bounds[1] or bounds[2] == bounds[3]:
            raise HTTPException(status_code=400, detail="the viewport has no area")
        viewport = rendering.Viewport.fit(
            bounds[0], bounds[1], bounds[2], bounds[3], size
        )

        details: dict = {}
        outline = None
        beam = None
        draw: Callable[[Any], tuple[Any, Any]]
        if kind in ("gaussian", "ovals"):
            if beam_x is not None and beam_y is not None:
                beam, source = (beam_x, beam_y), "request"
            else:
                adapter = await root.lookup_adapter([uid])
                beam = _beam_from_metadata(adapter.metadata().get("start", {}))
                source = "metadata"
                if beam is None and step is not None:
                    beam, source = step, "scan step"
            if beam is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{kind} needs beam_x/beam_y without a beam size or grid",
                )
            details["beam"] = {"x": beam[0], "y": beam[1], "source": source}

        if kind == "cells":
            if grid is not None:
                edges = (grid.x_edges, grid.y_edges)
            else:  # no spec: the previous default binning over the data
                _, ex, ey = (
                    numpy.histogram2d(x, y) if len(x) else (None, [0, 1], [0, 1])
                )
                edges = (numpy.asarray(ex), numpy.asarray(ey))

            draw = lambda v: rendering.render_cells(  # noqa: E731
                v, x, y, edges[0], edges[1], viewport
            )
        elif kind == "rectangles":
            size_x = cell_x if cell_x is not None else (step[0] if step else None)
            size_y = cell_y if cell_y is not None else (step[1] if step else None)
            if size_x is None or size_y is None:
                raise HTTPException(
                    status_code=400,
                    detail="rectangles need cell_x/cell_y if the run has no scan grid",
                )
            details["cell"] = {"x": size_x, "y": size_y}

            draw = lambda v: rendering.render_rectangles(  # noqa: E731
                v, x, y, size_x, size_y, viewport
            )
        elif kind == "gaussian":
            assert beam is not None
            draw = lambda v: rendering.render_gaussian(  # noqa: E731
                v, x, y, beam[0], beam[1], viewport
            )
        else:
            assert beam is not None
            layout = rendering.oval_layout(
                x, y, beam[0], beam[1], viewport, line_width, line_opacity
            )
            outline = layout.outline
            details["outline_style"] = {"width": line_width, "opacity": line_opacity}
            draw = layout.paint

        if format == "png":
            image, coverage = draw(points.data[channel])
            payload = rendering.to_png(
                image, coverage, rendering.CHANNEL_COLOUR[channel], vmin, vmax, outline
            )
            media = "image/png"
        else:
            images = {
                name: draw(points.data[name])
                for name in ("RedTotal", "GreenTotal", "BlueTotal")
            }
            coverage = next(iter(images.values()))[1]
            output = {
                "kind": kind,
                "viewport": {
                    "x0": viewport.x0,
                    "x1": viewport.x1,
                    "y0": viewport.y0,
                    "y1": viewport.y1,
                    "width": viewport.width,
                    "height": viewport.height,
                },
                "channels": {
                    name: rendering.to_rows(img) for name, (img, _) in images.items()
                },
                "coverage": numpy.round(coverage, 3).tolist(),
                **(
                    {"outline": numpy.round(outline, 3).tolist()}
                    if outline is not None
                    else {}
                ),
                "n_points": points.n_points,
                "n_total": grid.n_total if grid is not None else None,
                "position_offset": None
                if offset is None
                else {"x": offset[0], "y": offset[1]},
                **details,
            }
            payload = json.dumps(output, separators=(",", ":")).encode()
            media = "application/json"
        return payload, f'"{hashlib.sha1(payload).hexdigest()[:20]}"', media

    payload, etag, media = await _shared(key, compute)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type=media, headers=headers)


def image_rows_are_y(img, counts) -> list[list[float | None]]:
    """The binned image as plot-ready rows: one row per y bin, one column per x
    bin, and ``None`` (JSON ``null``) where no point landed.

    ``numpy.histogram2d`` indexes its result ``[x_bin, y_bin]``, so it is
    transposed here. An empty bin is reported as ``None`` rather than ``0`` so a
    plot can tell "no data yet" from a measured zero.
    """
    rows = numpy.where(counts > 0, img, numpy.nan).T.astype(object)
    rows[counts.T == 0] = None
    return rows.tolist()


def compute_binned_image(data, readback_x, readback_y, **kwargs):
    counts, edges_x, edges_y = numpy.histogram2d(readback_x, readback_y, **kwargs)
    weights, _, _ = numpy.histogram2d(readback_x, readback_y, weights=data, **kwargs)
    img = numpy.divide(weights, counts, out=numpy.zeros_like(weights), where=counts > 0)
    return {"img": img, "counts": counts, "x": edges_x, "y": edges_y}
