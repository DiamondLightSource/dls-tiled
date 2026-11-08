# NeXus files

[`bluesky-nexus`](https://github.com/bluesky/bluesky-nexus) turns a Bluesky run into
a [NeXus](https://www.nexusformat.org/) file. It is an early-stage project and isn't
on PyPI, so install it from GitHub:

```sh
pip install git+https://github.com/bluesky/bluesky-nexus "tiled[client]" bluesky-tiled-plugins
```

There are three ways to get a file, depending on when you need it:

| When                         | How                                                                 | Status                    |
| ---------------------------- | ------------------------------------------------------------------- | ------------------------- |
| [After the scan](#after-the-scan) | Rebuild the run's documents from Tiled                         | Works today               |
| [As the scan ends](#as-the-scan-ends) | Subscribe a callback to the scan's documents              | Works, if you can subscribe |
| [During the scan](#experimental-during-the-scan-with-streams) | Rewrite a snapshot on each Tiled stream update | Experimental  |

In all of them, the file holds **only the values recorded in events**: motor
positions, scalar signals and configuration. Detector data that Bluesky stores in
external files is listed in the NeXus file with **empty datasets**, and isn't
linked. `bluesky-nexus` doesn't read those files. Fetch such data from Tiled
separately.

Run the code on a machine where you have write access to the output directory.
The Tiled pods themselves can't write to `/dls`.

## After the scan

Build the Bluesky documents from what Tiled stores and pass them to the callback.
This helper needs no server support. It is also what the experimental recipe below
uses.

```python
import time
from pathlib import Path

from bluesky_nexus.callbacks import HdfWriterCallback
from tiled.client import from_uri


def run_documents(run):
    """Yield (name, document) pairs for the event data Tiled holds for a run."""
    start = run.metadata["start"]
    yield "start", start
    for stream_name in run:
        stream = run[stream_name]
        metadata = dict(stream.metadata)
        descriptor_uid = metadata["uid"]
        object_keys = {}
        for key, info in metadata["data_keys"].items():
            object_keys.setdefault(info["object_name"], []).append(key)
        yield "descriptor", {
            **metadata,
            "name": stream_name,
            "run_start": start["uid"],
            "object_keys": object_keys,
        }
        table = stream.base["internal"].read()
        keys = [key for key in metadata["data_keys"] if key in table.columns]
        for row in table.to_dict("records"):
            yield "event", {
                "descriptor": descriptor_uid,
                "uid": f"{descriptor_uid}-{row['seq_num']}",
                "seq_num": int(row["seq_num"]),
                "time": row["time"],
                "data": {key: row[key] for key in keys},
                "timestamps": {key: row[f"ts_{key}"] for key in keys},
            }
    # A running scan has no stop document, but the callback needs one to write.
    yield "stop", run.metadata.get("stop") or {
        "uid": "in-progress",
        "run_start": start["uid"],
        "time": time.time(),
        "exit_status": "unknown",
        "reason": "scan still running",
        "num_events": {},
    }


client = from_uri("https://tiled.diamond.ac.uk")
run = client["p51/cm44254/cm44254-1/9a812aec-b092-4fb4-9c8e-c58beac77fdc"]

writer = HdfWriterCallback(Path("/path/to/output"))
for name, document in run_documents(run):
    writer(name, document)
```

The file is named from the run's start time and plan, such as
`2026-07-02-10-22-15-scan.nxs`. `HdfWriterCallback` won't overwrite an existing
file.

### Using `run.documents()`

Tiled can reconstruct the documents on the server, which also includes the
detector `stream_resource` documents. Where the server supports it, you can replace
`run_documents(run)` with `run.documents()`.

**Diamond's server doesn't support this yet**, so the call fails with
`406: None of the media types requested by the client are supported`. It needs
`bluesky-tiled-plugins` added to the image, and this in the server config:

```yaml
media_types:
  BlueskyRun:
    application/json-seq: bluesky_tiled_plugins.exporters:json_seq_exporter
```

## As the scan ends

`HdfWriterCallback` is a normal Bluesky callback. It collects a run's documents and
writes one file when the stop document arrives.

If you control the `RunEngine`:

```python
RE.subscribe(HdfWriterCallback(Path("/path/to/output")))
```

Or run it as a separate service that receives documents over ZeroMQ or Kafka:

```sh
pip install "bluesky-nexus[zmq] @ git+https://github.com/bluesky/bluesky-nexus"
python -m bluesky_nexus.dispatchers.zmq <output_directory> <zmq_ip> <zmq_port>

pip install "bluesky-nexus[kafka] @ git+https://github.com/bluesky/bluesky-nexus"
python -m bluesky_nexus.dispatchers.kafka <output_directory> <brokers> <group> <topic>
```

The file only appears once the scan has finished. blueapi publishes its documents
over STOMP, which neither service reads, and you can't subscribe to its `RunEngine`
from outside, so for scans run through blueapi use [After the scan](#after-the-scan).

To do something other than write HDF5, subclass `BaseCallback` and implement
`process_nexus_structure(self, generated_structure)`.

## Experimental: during the scan with streams

> **Experimental.** This is a stopgap until
> [bluesky-nexus#1](https://github.com/bluesky/bluesky-nexus/issues/1) adds a proper
> Tiled interface, which will let a callback take a Tiled container directly.
> Expect this recipe to be replaced. Don't use it for anything that depends on the
> file being complete.

This watches a visit with [streaming](streaming.md). While a scan runs, it rebuilds
the NeXus file from Tiled every few seconds. When the stop document arrives, it
writes the file a final time. It uses `run_documents` from
[After the scan](#after-the-scan).

```python
import os
import threading

from bluesky_nexus.callbacks import BaseCallback
from bluesky_tiled_plugins import subscribe_to_streams

OUTPUT = Path("/path/to/output")
SECONDS_BETWEEN_SNAPSHOTS = 5

visit = client["p51/cm44254/cm44254-1"]
lock = threading.Lock()
last_written = {}


class SnapshotWriter(BaseCallback):
    def process_nexus_structure(self, generated_structure):
        start = self._run_data.run_start
        final = OUTPUT / f"{start['scan_id']}-{start['uid'][:8]}.nxs"
        # Readers only ever see a complete file.
        partial = final.with_suffix(".partial.nxs")
        generated_structure.save(partial, mode="w")
        os.replace(partial, final)


def write_snapshot(run_uid, final=False):
    with lock:  # callbacks can run concurrently
        now = time.monotonic()
        if not final and now - last_written.get(run_uid, 0) < SECONDS_BETWEEN_SNAPSHOTS:
            return
        last_written[run_uid] = now
        writer = SnapshotWriter()
        for name, document in run_documents(visit[run_uid]):
            writer(name, document)


def on_update(update):
    write_snapshot(update.run_uid)


def on_metadata(update):
    if "stop" in update.metadata:
        write_snapshot(update.key, final=True)


finished_runs = visit.subscribe()
finished_runs.child_metadata_updated.add_callback(on_metadata)
finished_runs.start_in_thread()

with subscribe_to_streams(visit, on_update, streams={"primary": None}):
    threading.Event().wait()  # until interrupted
```

Limitations:

- Each snapshot re-reads the whole run and rewrites the whole file, so the cost
  grows as the scan does. Increase `SECONDS_BETWEEN_SNAPSHOTS` for long scans.
- Until the scan ends, `entry/run_info/run_stop` holds a placeholder with
  `exit_status` of `unknown`.
- It relies on the private `_run_data` attribute for the file name.
- Only the `primary` stream triggers a snapshot, though each snapshot includes every
  stream that exists by then.
- Runs that fail before recording an event get only the final file.
- On startup, updates still in the streaming cache are replayed, so you may also
  get files for scans that ran shortly before.
- A snapshot may lag the newest points slightly, because streaming doesn't guarantee
  that a read made at the same moment sees the latest writes.
