# Searching and querying

There are two ways to query Tiled: the [Python client](#searching-from-python),
which has the full range of queries, and [Glazed](#graphql-with-glazed), a
GraphQL interface suited to web applications.

## Searching from Python

`search` filters the **direct children** of a node. Runs are stored under their
visit, so search the visit. Searching the root finds nothing, because the root's
children are beamlines.

```python
from tiled.client import from_uri
from tiled.queries import Key

client = from_uri("https://tiled.diamond.ac.uk")
visit = client["p51/cm44254/cm44254-1"]

results = visit.search(Key("start.plan_name") == "scan")
len(results)
run = results.values().first()
```

`Key` addresses metadata with dots. A run's metadata is its Bluesky start and stop
documents, so keys begin with `start.` or `stop.`. Always include that prefix:
leaving it out only works on catalogs with Bluesky's custom client, and Diamond's
visit nodes aren't those.

Each `search` returns another searchable node, so chain them to combine
conditions:

```python
failed_scans = (
    visit.search(Key("start.plan_name") == "scan")
    .search(Key("stop.exit_status") != "success")
)
```

### Useful metadata

| Key                                                      | Meaning                                       |
| -------------------------------------------------------- | --------------------------------------------- |
| `start.uid`                                              | The run's unique ID, also its key in Tiled    |
| `start.scan_id`                                          | The scan number                               |
| `start.time`                                             | Start time, as a Unix timestamp               |
| `start.plan_name`, `start.plan_type`                     | The plan that ran                             |
| `start.instrument`, `start.instrument_session`           | Beamline and visit                            |
| `start.detectors`, `start.motors`                        | Lists of device names                         |
| `start.num_points`, `start.shape`                        | Size of the scan                              |
| `start.blueapi_task_id`                                  | The blueapi task that produced the run        |
| `stop.exit_status`                                       | `success`, `abort` or `fail`                  |
| `stop.reason`, `stop.num_events.primary`                 | Why it stopped; events recorded in `primary`  |

To see what else a run has, look at `run.metadata`. A run that is still going has
no `stop` key.

### Query types

All from `tiled.queries`:

| Query                 | Example                                                     | Finds runs where...                     |
| --------------------- | ----------------------------------------------------------- | --------------------------------------- |
| Equals                | `Key("start.scan_id") == 42`                                | the value matches                       |
| Not equal             | `Key("stop.exit_status") != "success"`                      | the value differs                       |
| Comparison            | `Key("start.num_points") > 100` (also `>=`, `<`, `<=`)      | the number compares                     |
| `In`                  | `In("start.plan_name", ["scan", "grid_scan"])`              | the value is one of these               |
| `NotIn`               | `NotIn("start.plan_name", ["count"])`                       | the value is none of these              |
| `Contains`            | `Contains("start.detectors", "panda1")`                     | the list includes this value            |
| `KeyPresent`          | `KeyPresent("stop")`                                        | the key exists (here: the run finished) |
| `Like`                | `Like("start.sample", "sil%")`                              | SQL-style match, `%` is a wildcard      |
| `FullText`            | `FullText("lysozyme")`                                      | any metadata contains the whole word    |
| `SpecsQuery`          | `SpecsQuery(["BlueskyRun"])`                                | the node has these specs                |
| `StructureFamilyQuery`| `StructureFamilyQuery("container")`                         | the node is of this type                |

`Regex` exists in `tiled.queries`, but the catalog doesn't support it. Use `Like`.

### Recipes

```python
from datetime import datetime

from tiled.queries import Contains, Key, KeyPresent

# A scan by number
visit.search(Key("start.scan_id") == 42).values().first()

# The most recent run
visit.values().last()

# The run from a blueapi task
visit.search(Key("start.blueapi_task_id") == task_id).values().first()

# Everything since a given time
visit.search(Key("start.time") > datetime(2026, 7, 2, 9, 0).timestamp())

# Finished scans using a detector, newest first
results = (
    visit.search(KeyPresent("stop"))
    .search(Contains("start.detectors", "panda1"))
    .sort(("start.time", -1))
)

# Which plans have been run here, and how often
visit.distinct("start.plan_name", counts=True)
```

`.values().first()` raises `IndexError` if nothing matched.

Sort with `1` for ascending and `-1` for descending. The `DESCENDING` constant
raises a `TypeError` in the 0.2.21 client.

Two things from Bluesky tutorials don't work on Diamond's server:

- `visit[-1]` or `visit[42]`, which need Bluesky's catalog client. Use the recipes
  above.
- `ScanID`, `PartialUID`, `ScanIDRange` and `TimeRange` from
  `bluesky_tiled_plugins.queries`, which need the plugin on the server, and the
  `dls-tiled` image doesn't include it. Use `Key("start.scan_id")` and
  `Key("start.time")` instead.

### Searching over HTTP

Queries are URL parameters on the search endpoint, in the form
`filter[<query>][condition][<field>]`. Values are JSON, so strings keep their
quotes:

```
GET /api/v1/search/p51/cm44254/cm44254-1
    ?filter[eq][condition][key]=start.plan_name
    &filter[eq][condition][value]="scan"
```

## GraphQL with Glazed

[Glazed](https://github.com/DiamondLightSource/glazed) is a read-only GraphQL
service in front of Tiled. It passes your bearer token through, so you see exactly
what you would see in Tiled. It is served at `https://tiled.diamond.ac.uk/glazed/`,
and the paths below are relative to that address. To try queries in your browser,
open the playground at <https://tiled.diamond.ac.uk/glazed/graphiql>.

| Path                                 | Purpose                                                           |
| ------------------------------------ | ----------------------------------------------------------------- |
| `POST /graphql`                      | Queries                                                           |
| `GET /graphiql`                      | Interactive playground, with the schema browsable in your browser |
| `/subscribe`                         | WebSocket subscriptions, using the `graphql-transport-ws` protocol |
| `GET /asset/{run}/{stream}/{det}/{id}` | Download a file                                                  |
| `GET /status`                        | Glazed's version                                                  |

> **Limitation: only runs at the root of Tiled are found.** Glazed searches the
> root node's children, but blueapi now writes runs under
> `beamline/proposal/visit`, so `instrumentSession` and `run` don't find them.
> They work only for older runs that are still stored at the root.
> `appMetadata` and the subscriptions aren't affected.

You can run these queries in the playground. From Python, the Tiled client's
HTTP session already holds your login:

```python
GLAZED = "https://tiled.diamond.ac.uk/glazed/"

def gql(query, **variables):
    response = client.context.http_client.post(
        f"{GLAZED}graphql", json={"query": query, "variables": variables}
    )
    return response.json()["data"]
```

### Runs in a visit

```graphql
query ($session: String!) {
  instrumentSession(name: $session) {
    name
    runs {
      id
      scanNumber
    }
  }
}
```

Variables: `{"session": "cm00000-1"}`.

### One run and its data

`data` is a list of two possible types, so pick fields for each. An unknown ID
returns `null`.

```graphql
query ($id: String!) {
  run(id: $id) {
    id
    scanNumber
    data {
      __typename
      ... on ArrayData {
        name
        files {
          file
          download
        }
      }
      ... on TableData {
        name
        columns
      }
    }
  }
}
```

`file` is the path on disk. `download` is a Glazed URL to fetch it with your
token, and is empty when the file can't be downloaded.

### Table values

Leave out `columns` to get every column:

```graphql
query ($id: String!, $columns: [String!]) {
  run(id: $id) {
    data {
      ... on TableData {
        name
        data(columns: $columns)
      }
    }
  }
}
```

The result maps each column name to a list of values.

### Server information

```graphql
{
  appMetadata {
    apiVersion
    libraryVersion
    queries
  }
}
```

### Live events

`events` watches Tiled's root, and `nodeEvents(node: ...)` watches one node. The
first message is always the node's schema, and new data follows.

```graphql
subscription ($node: String!) {
  nodeEvents(node: $node) {
    __typename
    ... on ContainerSchema { version }
    ... on ChildCreated { key sequence metadata }
    ... on ChildMetadataUpdated { key sequence metadata }
    ... on StreamTableData { sequence append arrowSchema }
    ... on StreamArrayData { sequence shape mimetype }
  }
}
```

The other event types are `ArraySchema`, `TableSchema` and `ArrayRef`. To
authenticate, send your token as `{"Authorization": "Bearer <token>"}` in the
`connection_init` payload, and also on the WebSocket handshake so that it passes
the login proxy.

Glazed isn't the same as Tiled's own GraphQL endpoint at `/api/graphql`, which
serves a separate graph of entities and links, not runs.
