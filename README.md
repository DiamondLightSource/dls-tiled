[![CI](https://github.com/epics-containers/dls-tiled/actions/workflows/ci.yml/badge.svg)](https://github.com/epics-containers/dls-tiled/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/epics-containers/dls-tiled/branch/main/graph/badge.svg)](https://codecov.io/gh/epics-containers/dls-tiled)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls_tiled

Tiled server with Diamond Light Source plugins

The upstream [Tiled](https://blueskyproject.io/tiled/) server image
(`ghcr.io/bluesky/tiled`) with Diamond Light Source plugins added to its
environment. Tiled itself is unmodified; the image is a drop-in replacement for
the upstream one in the [bluesky tiled helm chart](https://github.com/bluesky/tiled/tree/main/helm/tiled).

What            | Where
:---:           | :---:
Source          | <https://github.com/epics-containers/dls-tiled>
Docker          | `docker run ghcr.io/epics-containers/dls-tiled:latest`
Releases        | <https://github.com/epics-containers/dls-tiled/releases>

## Plugins

Plugins are FastAPI routers. Enable one by listing it under `routers:` in the
tiled server config.

### `dls_tiled.visr:visr_router`

`GET /api/v1/binned/{uid}` bins a ViSR scan into a 2-D image on the server and
returns the mean `RedTotal`, `GreenTotal` and `BlueTotal` in each bin as
matrices with one row per y bin and one column per x bin (`null` where no point
landed), the bin edges as `x_limits` and `y_limits`, and `n_points`/`n_total`,
the points used so far and the points the scan will have (`null` without a
spec). Positions come from the recorded readbacks, or from the ScanSpec
setpoints in the run's start document with `setpoints=true`.

Unless `width`/`height` or a full `xmin`/`xmax`/`ymin`/`ymax` range are given,
the bins follow the scan's setpoint grid from its spec (one cell per setpoint,
fixed from the first poll), provided the spec is a grid of at least 3×3 and the
recorded positions lie on it. Positions that are off the grid by a constant
amount per axis (as fly scans currently record them) are first shifted onto it
by the median of each point's position minus its setpoint, and the shift is
returned as `position_offset`. Otherwise numpy's default 10×10 over the data.
Other query parameters: `x_dim_index`, `y_dim_index` and the repeatable
`slice_dim=dim:center:thickness`.

A file still behind the catalog is retried briefly and then answered with 503
and `Retry-After`. Identical requests share one result for a short while and
carry an `ETag`, so a repeated poll of an unchanged image gets a `304`.

| Environment variable | Default | |
|---|---|---|
| `DLS_TILED_VISR_NOT_READY_RETRIES` | `3` | retries before answering 503 |
| `DLS_TILED_VISR_NOT_READY_RETRY_DELAY` | `0.2` | seconds between those retries |
| `DLS_TILED_VISR_CACHE_SECONDS` | `0.5` | how long a result is shared; `0` turns sharing off |
| `DLS_TILED_VISR_MAX_GRID_CELLS` | `100000` | largest scan grid used by default |

```yaml
routers:
  - dls_tiled.visr:visr_router
```

Ported from Hiran Wijesinghe's [visr-tiled](https://github.com/hyperrealist/visr-tiled).

## Logging

tiled's default logging config has no handler for the plugin loggers, so their
INFO messages are dropped. The image includes `/deploy/log_config.yml`, which is
tiled's default plus the `dls_tiled` loggers. Select it by overriding the
container command, e.g. with the tiled helm chart:

```yaml
args: [tiled, serve, config, --host, 0.0.0.0, --port, "8000", --scalable,
       --log-config, /deploy/log_config.yml]
```

## Tiled version

`TILED_VERSION` at the top of the `Dockerfile` selects the upstream release.
The image build fails if installing the plugins would change that version.
