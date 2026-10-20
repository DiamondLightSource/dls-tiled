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

### Postgres databases

Tiled uses postgres for the catalog (`tiled`), the authentication database
(`auth`) and tabular data storage (`data`). Until this is done by an init
container in the chart, create and initialize them by hand.

Create the databases from the postgres pod:

```bash
kubectl exec -it tiled-testing-postgres-77669bf4c5-t8jmc -n tiled-staging -- psql -U postgres
```

```sql
CREATE DATABASE tiled;
CREATE DATABASE auth;
CREATE DATABASE data;
```

Then start a throwaway tiled pod, exec into it and create the tables:

```bash
kubectl run tiled-init -n tiled-staging --image=ghcr.io/bluesky/tiled:0.2.18 \
  --restart=Never --command \
  -- sleep infinity
kubectl exec -it tiled-init -n tiled-staging -- bash
```

```bash
export POSTGRESQL_PASSWORD="password"

tiled catalog init --if-not-exists postgresql+asyncpg://postgres:${POSTGRESQL_PASSWORD}@tiled-testing-postgres:5432/tiled

tiled admin initialize-database postgresql+asyncpg://postgres:${POSTGRESQL_PASSWORD}@tiled-testing-postgres:5432/auth

kubectl delete pod tiled-init -n tiled-staging --now
```

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
