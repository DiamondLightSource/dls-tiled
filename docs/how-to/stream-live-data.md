# Streaming live data

Tiled can push data to you as a scan runs, over WebSockets. blueapi writes each
event to Tiled as soon as it is taken (`TiledWriter(..., batch_size=1)`), so you
get each point as it arrives and don't need to poll.

Upstream reference:
[Subscribe to live Bluesky stream data](https://blueskyproject.io/bluesky-tiled-plugins/tutorials/streaming.html#subscribe-to-live-bluesky-stream-data).

## Subscribe to a visit

`subscribe_to_streams` watches a container for new runs and delivers the data keys
you choose. The container must be the **direct parent of the runs**, which at
Diamond is the visit.

It needs a recent client: `bluesky-tiled-plugins` 2.0.13 and `tiled` 0.2.21 are
known to work. Older Tiled clients try to create an API key for the connection,
which fails without the `create:apikeys` scope.

```python
from bluesky_tiled_plugins import BlueskyStreamUpdate, subscribe_to_streams
from tiled.client import from_uri

client = from_uri("https://tiled.diamond.ac.uk")
visit = client["p51/cm44254/cm44254-1"]


def on_update(update: BlueskyStreamUpdate) -> None:
    print(update.run_uid, update.stream_name, update.data_keys, update.sequence)
    print(update.data())


with subscribe_to_streams(visit, on_update, streams={"primary": "det"}):
    input("Listening. Press Enter to stop.\n")
```

Leaving the `with` block disconnects everything. If a context manager doesn't suit
your code, keep the returned object and call `disconnect()` on it.

The subscription stays active across scans, so it picks up each new run in the
visit as it starts.

### Choosing what to receive

`streams` is required.

| Value                                         | You receive                                            |
| --------------------------------------------- | ------------------------------------------------------ |
| `{"primary": "det"}`                          | One key                                                |
| `{"primary": ("det", "motor")}`               | Several keys                                           |
| `{"primary": None, "baseline": "x"}`          | Every key in `primary`, and one key in `baseline`      |
| `None`                                        | Every key in every stream of every run (can be a lot)  |

Streams you don't name are ignored. Ragged and bytes data aren't delivered.

### Choosing which runs

Filters are checked before anything is connected. A run must pass all of them.

```python
subscribe_to_streams(
    visit,
    on_update,
    streams={"primary": None},
    metadata_filter=lambda start: start.get("plan_name") == "spec_scan",
    required_specs=("BlueskyRun",),
)
```

`run_filter` is also available. It receives the raw `LiveChildCreated` update, for
conditions that the other two can't express.

To get a run's start document in your callback:

```python
start = visit[update.run_uid].metadata_copy()[0].get("start", {})
```

### Things to design for

- **Updates aren't ordered across data nodes.** Arrays and table columns in
  different nodes arrive as separate callbacks, with no alignment between them.
  Scalars stored in the same table arrive together in one batch.
- **Callbacks can run concurrently.** Protect any shared state with a lock.
- **Use `update.sequence` to detect gaps or duplicates** within one node. It
  doesn't compare across nodes.
- **Replay is limited.** By default (`start=0`) you first get whatever is still in
  the streaming cache, not the whole history. Use [search](search.md) for
  history.
- **The end of a stream doesn't mean the data can be read yet.** A REST read made
  at the same moment may not see the last writes.

## Lower-level subscriptions

To be told about new runs and their completion, without any data, subscribe to the
container itself. Use the `key` to find the run:

```python
sub = visit.subscribe()
sub.child_created.add_callback(lambda u: print("new run", u.key))
sub.child_metadata_updated.add_callback(
    lambda u: print("finished", u.key) if "stop" in u.metadata else None
)
sub.start_in_thread()
```

## Raw WebSockets

For clients without the Python library, connect to `stream/single/<path>` under the
API, then send your access token as the first message:

```python
import json

from websockets.sync.client import connect

path = "p51/cm44254/cm44254-1"
url = f"wss://tiled.diamond.ac.uk/api/v1/stream/single/{path}?envelope_format=json"

with connect(url) as ws:
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    schema = json.loads(ws.recv())    # bad credentials close the socket with code 4003
    for message in ws:
        print(json.loads(message))
```

Add `&start=1` to replay from the first update still in the cache.

| Message `type`                     | Meaning                                                    |
| ---------------------------------- | ---------------------------------------------------------- |
| `container-schema`, `array-schema`, `table-schema` | The first message, describing the node     |
| `container-child-created`          | A new child: on a visit, a new run                         |
| `container-child-metadata-updated` | A child's metadata changed: a run's `stop` document arrives this way |
| `table-data`                       | New rows                                                   |
| `array-data`, `array-ref`          | New array data, or a reference to it                       |

Watching a single table gives you rows as they arrive:
`<run>/primary/internal` holds a run's scalar values.

[Glazed subscriptions](search.md#live-events) offer the same events over
GraphQL.

## Running a server with streaming

This matters only if you run your own Tiled. Streaming needs a Redis cache in the
server config:

```yaml
streaming_cache:
  uri: "redis://:password@redis-host:6379"
```

Without it, or with Tiled's embedded `SimpleTiledServer`, subscriptions aren't
available.
