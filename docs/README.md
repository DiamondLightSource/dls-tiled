# Using Tiled at Diamond

[Tiled](https://blueskyproject.io/tiled/) is the data access service for scans
run with Bluesky at Diamond. It stores each scan's metadata and tabular data, knows
where its detector files are, and serves all of it over HTTP, with access
checked against your Diamond login.

| I want to...                                       | Read                                                                  |
| -------------------------------------------------- | --------------------------------------------------------------------- |
| Find a scan and read its data from Python          | [Getting started](#getting-started), [Searching](searching.md)        |
| Query Tiled from a web app or with GraphQL         | [Glazed](searching.md#graphql-with-glazed)                            |
| Follow a scan live while it runs                   | [Streaming](streaming.md)                                             |
| Produce a NeXus file for a scan                    | [NeXus files](nexus.md)                                               |
| Understand who can see what                        | [Authorization](authorization.md)                                     |
| Understand how the service is deployed             | [Architecture](architecture.md)                                       |
| Report that data can't be read                     | [Getting help](#getting-help)                                         |

## Getting started

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

### How the data is organised

Tiled is a tree. Runs are written by blueapi under the visit they belong to, so
you normally index straight to your visit:

```
<beamline>/<proposal>/<visit>/<run uid>/<stream>/<data>
p51/cm44254/cm44254-1/9a812aec-b092-4fb4-9c8e-c58beac77fdc/primary/panda1-counter-1-out_dataset
```

```python
visit = client["p51/cm44254/cm44254-1"]
```

> **Changing soon.** The [central authorization work](authorization.md#new-central-authorization)
> removes the proposal level and adds folders per visit, so the same visit will
> be at `p51/cm44254-1/raw`. Code that builds these paths will need updating
> when that is released.

### Reading a run

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

### Common use cases

- **Find scans in a visit** by plan, scan number, outcome or time:
  [Searching](searching.md).
- **Analysis scripts** read arrays and tables as NumPy, pandas or xarray objects,
  without needing to know file paths.
- **Live displays** get each point as it is taken: [Streaming](streaming.md).
- **Web applications** use the GraphQL interface, [Glazed](searching.md#graphql-with-glazed),
  or the REST API. Tiled's interactive API docs are at `/docs` on the server.
- **ViSR imaging** has a server-side binning endpoint, described in the
  [project README](../README.md#dls_tiledvisrvisr_router).
- **NeXus files** can be generated from a run: [NeXus files](nexus.md).

## Getting help

### If you can't read data for a beamline

Create a ticket in the
[ACQP Jira project](https://jira.diamond.ac.uk/projects/ACQP/issues/). Include:

- the beamline and visit, for example `p51` and `cm44254-1`
- the full node path, or the run's uid
- what you ran, the full error, and roughly when it happened
- your FedID, so access can be checked

Before raising a ticket, these symptoms can help you tell what is wrong:

| Symptom                                                 | Likely cause                                                                                                                                                                                                                  |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `401 Unauthorized`                                      | You are not logged in, or your token has expired. Connect again.                                                                                                                                                              |
| The node isn't there (`KeyError`, `404`)                | You don't have access to that visit, or the path is wrong. If you were only just added to the visit, wait a minute: Tiled remembers your permissions for up to 60 seconds.                                                    |
| The metadata reads, but reading the data gives a `500`  | Usually a problem with the file, not your permissions: Tiled can't open it, or a dataset is missing from it. The beamline's pod may also lack filesystem access to it. Quote the error's correlation ID in the ticket if you have it. |
| `503` with `Retry-After`                                | The file is still catching up with the catalog during a scan. Try again shortly.                                                                                                                                              |
| A whole beamline's data is unreadable                   | The beamline may not have its own Tiled deployment or data ACL yet ([why](architecture.md#adding-a-beamline)). Raise a ticket.                                                                                                |
