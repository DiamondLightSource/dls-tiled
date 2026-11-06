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
returns the mean `RedTotal`, `GreenTotal` and `BlueTotal` in each bin as
matrices with one row per y bin and one column per x bin (`null` where no point
landed), the bin edges as `x_limits` and `y_limits`, and `n_points`/`n_total`,
the points used so far and the points the scan will have (`null` without a
spec). Positions come from the recorded readbacks, or from the ScanSpec
setpoints in the run's start document with `setpoints=true`.

Unless `width`/`height` or a full `xmin`/`xmax`/`ymin`/`ymax` range are given,
the bins follow the scan's setpoint grid from its spec (one cell per setpoint,
fixed from the first poll), provided the spec lays its points out on a grid
(any size, down to a line or a single point) and the recorded positions lie on
it. Positions that are off the grid by a constant
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

## DLS access policy

`dls_tiled.policy:DiamondOpenPolicyAgentAuthorizationPolicy` is a tiled access
policy that delegates every decision to an [Open Policy Agent](https://www.openpolicyagent.org/)
server, using the `diamond.tiled` rules in the central authorisation service.
Each request sends OPA the user's access token and the token audience, plus the
node's `instrument`, `instrument_session` and `folder`. These come from the
node's single Diamond tag in its `access_tags`: either a JSON blob a client
sends when creating a node, or the tag OPA stored on an existing node. tiled
keeps tags as an unordered set, so a node with more than one Diamond tag sends
no node fields and gets no write access. Reading, creating
and modifying nodes need a user principal; anonymous requests and service
principals get a 401.

The tree and its tags:

```
<instrument>/                i22                lets callers see the instrument node only
  <visit>/                   i22,cm12345-1
    raw/                     i22,cm12345-1      written by the instrument's tiled-writer
    processed/               i22,cm12345-1,p    written by the processing service account
    processing/              i22,cm12345-1,t    written by anyone on the visit
```

Anyone on the visit can read all three folders, and sees the instrument node
above it. To create a node a client sends one access tag holding a JSON blob,
e.g. `{"instrument": "i22", "instrument_session": "cm12345-1", "folder": "processing"}`
(`folder` defaults to `raw`; `{"instrument": "i22"}` alone is the instrument
node) and OPA replaces it with the node's tag.

A node's tag always matches its path, because the envoy in front of the
per-instrument tiled pods routes on the instrument in the path: a node tagged
for `i22` is always under `/i22/`, and one tagged for visit `cm12345-1` under
`/i22/cm12345-1/`. When a node is created, or its tags changed, the policy works
out its instrument, visit and folder from where it is in the tree and asks OPA
about that place. It refuses (403) with no OPA call:

- tags that name another instrument, visit or folder than the node's path
  (an existing node's tag, e.g. `i22,cm12345-1,t`, is accepted as well as a blob)
- a node anywhere else: at the root, as a fourth folder of a visit, with a visit
  key in upper case (tags write it in lower case), or with no key
- changing a node's tags to anything but the tag its path needs, so a node
  cannot be moved to another folder by retagging

A node created with no access tags gets its path's tag too (if OPA allows),
rather than being left untagged. The entities and links of tiled's GraphQL API
are not in the tree, so they are tagged from what the caller sends, as before.

It also refuses OPA answers other than the one tag the path needs. tiled does
not tell an access policy where a node is being created, so this needs the
server started with `dls-tiled serve`, which is `tiled serve` plus a middleware
that records the node path of each create, register and metadata update
request (`dls-tiled serve config config.yml --host 0.0.0.0`; the image runs it
by default). For uvicorn or gunicorn workers, use
`uvicorn --factory dls_tiled.node_path:app_factory` (with `TILED_CONFIG` set)
in place of tiled's `app_factory`. Without the middleware the policy refuses
to create nodes or change their tags.

- reading: OPA returns the tags the user may see (`allowed_tags_endpoint`);
  `["*"]` means everything.
- creating a node: OPA returns the tag to store in the new node's `access_tags`
  (`create_node_endpoint`).
- changing a node's `access_tags`: OPA returns the tags for the node's place,
  as for creating it, or nothing, which refuses the change with a 403
  (`modify_node_endpoint`, by default the create rule).
- scopes on a node: OPA returns the allowed scopes (`scopes_endpoint`).

Every argument, with the defaults for the optional ones:

```yaml
access_control:
  access_policy: dls_tiled.policy:DiamondOpenPolicyAgentAuthorizationPolicy
  args:
    authorization_provider: http://opa:8181/v1/data/  # OPA data API base URL
    token_audience: account                           # sent to OPA as "audience"
    create_node_endpoint: tiled/node_tags
    allowed_tags_endpoint: tiled/user_tags
    scopes_endpoint: tiled/scopes
    modify_node_endpoint: tiled/node_tags
    empty_access_tags_public: true  # outside the tree (GraphQL), allow creates with no access_tags without asking OPA
    provider: null
```

The endpoints are appended to `authorization_provider` as they are, so it needs
a trailing `/`.

Ported from Zoheb Shaikh's [diamond-tiled](https://github.com/DiamondLightSource/diamond-tiled).

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
kubectl run tiled-init -n tiled-staging --image=ghcr.io/bluesky/tiled:0.2.21 \
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
