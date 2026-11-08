# Getting started with Tiled

[Tiled](https://blueskyproject.io/tiled/) is the data access service for scans
run with Bluesky at Diamond, and DAQ Core's main purpose for it is
[acquisition](../explanations/service-levels.md). It stores each scan's metadata and tabular data, knows
where its detector files are, and serves all of it over HTTP, with access
checked against your Diamond login.

| I want to...                                       | Read                                                                  |
| -------------------------------------------------- | --------------------------------------------------------------------- |
| Find a scan and read its data from Python          | [Reading a run](#reading-a-run), [Searching](../how-to/search.md)        |
| Query Tiled from a web app or with GraphQL         | [Glazed](../how-to/search.md#graphql-with-glazed)                            |
| Follow a scan live while it runs                   | [Streaming](../how-to/stream-live-data.md)                                             |
| Produce a NeXus file for a scan                    | [NeXus files](../how-to/create-nexus-files.md)                                               |
| Understand who can see what                        | [Authorization](../explanations/authorization.md)                                     |
| Understand how the service is deployed             | [Architecture](../explanations/architecture.md)                                       |
| Know what happens during an outage                 | [Service levels and outages](../explanations/service-levels.md)        |
| Connect from UDC or another script without a login | [Use a service account](../how-to/use-a-service-account.md)            |
| Create a processed folder for a visit              | [Use a service account](../how-to/use-a-service-account.md#create-a-processed-folder) |
| Ask for a custom endpoint for a beamline           | [Request a custom endpoint](../how-to/request-a-custom-endpoint.md)    |
| Report that data can't be read                     | [Getting help](../how-to/get-help.md)                                         |

## Install and connect

Install the client. `bluesky-tiled-plugins` gives you Bluesky-aware objects for
runs and event streams, and the live streaming API:

```sh
pip install "tiled[client]" bluesky-tiled-plugins
```

Connect. The first time, Tiled opens a browser so you can log in with your
Diamond account. Tokens are then cached in `~/.cache/tiled/tokens`.

```python
from tiled.client import from_uri

client = from_uri("https://tiled.diamond.ac.uk")
```

## How the data is organised

Tiled is a tree. Runs are written by blueapi under the visit they belong to, so
you normally index straight to your visit:

```
<beamline>/<proposal>/<visit>/<run uid>/<stream>/<data>
p51/cm44254/cm44254-1/9a812aec-b092-4fb4-9c8e-c58beac77fdc/primary/panda1-counter-1-out_dataset
```

```python
visit = client["p51/cm44254/cm44254-1"]
```

> **Changing soon.** The [central authorization work](../explanations/authorization.md#new-central-authorization)
> removes the proposal level and adds folders per visit, so the same visit will
> be at `p51/cm44254-1/raw`. Code that builds these paths will need updating
> when that is released.

## Reading a run

```python
run = visit["9a812aec-b092-4fb4-9c8e-c58beac77fdc"]

run.metadata["start"]          # the Bluesky start document
run.metadata.get("stop")       # the stop document; missing while the scan runs
list(run)                      # stream names, e.g. ["primary", "baseline"]

ds = run["primary"].read()     # the whole stream as an xarray.Dataset
values = run["primary"]["panda1-counter-1-out_dataset"].read()  # one signal
```

Detector data can be many gigabytes. Slice arrays to read only what you need:

```python
images = run["primary"]["detector"]    # nothing is downloaded yet
first_frame = images[0]
```

## Common use cases

- **Find scans in a visit** by plan, scan number, outcome or time:
  [Searching](../how-to/search.md).
- **Analysis scripts** read arrays and tables as NumPy, pandas or xarray objects,
  without needing to know file paths.
- **Live displays** get each point as it is taken: [Streaming](../how-to/stream-live-data.md).
- **Web applications** use the GraphQL interface, [Glazed](../how-to/search.md#graphql-with-glazed),
  or the REST API. Tiled's interactive API docs are at `/docs` on the server.
- **ViSR imaging** has a server-side binning endpoint, described in the
  [project README](https://github.com/DiamondLightSource/dls-tiled#dls_tiledvisrvisr_router).
- **NeXus files** can be generated from a run: [NeXus files](../how-to/create-nexus-files.md).
