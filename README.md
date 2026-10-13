[![CI](https://github.com/DiamondLightSource/dls-tiled/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-tiled/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-tiled/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-tiled)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls_tiled

Tiled server with Diamond Light Source plugins

The upstream [Tiled](https://blueskyproject.io/tiled/) server image
(`ghcr.io/bluesky/tiled`) with Diamond Light Source plugins added to its
environment. Tiled itself is unmodified; the image is a drop-in replacement for
the upstream one in the [bluesky tiled helm chart](https://github.com/bluesky/tiled/tree/main/helm/tiled).

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/dls-tiled>
Docker          | `docker run ghcr.io/diamondlightsource/dls-tiled:latest`
Releases        | <https://github.com/DiamondLightSource/dls-tiled/releases>

## Plugins

Plugins are FastAPI routers. Enable one by listing it under `routers:` in the
tiled server config.

### `dls_tiled.visr:visr_router`

`GET /api/v1/binned/{uid}` bins a ViSR scan into a 2-D image on the server and
returns the mean `RedTotal`, `GreenTotal` and `BlueTotal` in each bin, plus the
bin edges as `x_limits` and `y_limits`. Positions come from the recorded
readbacks, or from the ScanSpec setpoints in the run's start document with
`setpoints=true`. Other query parameters: `x_dim_index`, `y_dim_index`, `width`,
`height`, `xmin`/`xmax`/`ymin`/`ymax` and the repeatable
`slice_dim=dim:center:thickness`.

```yaml
routers:
  - dls_tiled.visr:visr_router
```

Ported from Hiran Wijesinghe's [visr-tiled](https://github.com/hyperrealist/visr-tiled).

## Helm chart

`Charts/tiled` is a generic Helm chart for the image, published on each tag to
`oci://ghcr.io/diamondlightsource/charts/tiled`. The chart version and appVersion
are both the git tag, and appVersion is the default image tag.

```bash
helm install tiled oci://ghcr.io/diamondlightsource/charts/tiled --version X.Y.Z
```

Plugins are off by default; see the `config.routers` comment in
`Charts/tiled/values.yaml`. The `TILED_SINGLE_USER_API_KEY` environment variable
is read from the optional secret `tiled-secrets`.

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
