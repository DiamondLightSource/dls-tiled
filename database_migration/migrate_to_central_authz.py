#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tiled[server]==0.2.21"]
# ///
"""Move runs into the tree the central authorisation tiled rules expect.

After:  <instrument>/<visit>/raw/<run>      e.g. i22/cm12345-1/raw/<run uid>
        <instrument>/<visit>/processed/
        <instrument>/<visit>/processing/

This is the layout blueapi writes into. Runs are found anywhere in the tree,
so this moves both of the layouts before it:

  - flat:      <run>                               (blueapi before the tree)
  - old tree:  <beamline>/<proposal>/<session>/<run>, e.g. i22/cm12345/cm12345-1

A run is a node with a start document that isn't inside another run. It goes
to the raw folder of the visit in its start document's instrument and
instrument_session, both lower case. A run in the old tree without them goes
by its path. Runs already in a folder of a visit stay there.

Every node gets exactly the one tag tiled.rego gives its place: the
instrument node "<instrument>", the visit and raw "<instrument>,<visit>",
processed and processing ",p" and ",t" on that, and everything in a run its
folder's tag. Envoy routes on the path, and dls-tiled refuses to write to a
node whose tag doesn't name the instrument and visit its path is under, so
the tags are replaced rather than added to: the old policy's tags
(bare session ids, "beamline:...", "visible:...") grant nothing now.

The old tree's proposal and session containers are deleted once empty.
Runs that can't be placed (no instrument or instrument_session to go by, or
a run of the same key already in the folder) are left where they are, tags
and all, and listed.

The tiled schema is upgraded first (to --revision, default head). Stop tiled
while this runs; it also locks the nodes table against writes. Postgres only.
Safe to rerun: a second run changes nothing.

Example:
    uv run --script database_migration/migrate_to_central_authz.py \\
        postgresql://user:pass@host:5432/tiled --dry-run
"""

import argparse
import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from tiled.access_control.protocols import ApprovedAccessTags
from tiled.alembic_utils import get_current_revision, upgrade
from tiled.catalog.alembic_constants import ALEMBIC_DIR, ALEMBIC_INI_TEMPLATE_PATH
from tiled.catalog.core import ALL_REVISIONS, register_tag_rows
from tiled.utils import ensure_specified_sql_driver

ROOT_ID = 0
# Access tags in their own tables, with each association's parent_id kept in
# step with nodes.parent by a trigger, which the moves below rely on.
TAGS_REVISION = "0d4e1f2a3b4c"

# Same as tiled.rego's _tag_suffix
FOLDERS = {"raw": "", "processed": ",p", "processing": ",t"}
INSTRUMENT_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# A lower case instrument session, e.g. "cm12345-1"
VISIT_RE = re.compile(r"^(?P<proposal>[a-z]+[0-9]+)-[0-9]+$")
# The old tree's proposal level, e.g. "cm12345"
PROPOSAL_RE = re.compile(r"^[a-z]+[0-9]+$")

# (instrument, visit, folder)
Place = tuple[str, str, str]


def tag(instrument: str, visit: str | None = None, folder: str = "raw") -> str:
    """The tag tiled.rego gives a node, e.g. tag("i22", "cm12345-1",
    "processing") is "i22,cm12345-1,t"."""
    if visit is None:
        return instrument
    return f"{instrument},{visit}{FOLDERS[folder]}"


@dataclass(frozen=True)
class Run:
    id: int
    key: str
    # Keys of the nodes above the run, below the root
    parents: tuple[str, ...]
    instrument: str | None
    instrument_session: str | None


def place(run: Run) -> Place | str:
    """Where the run goes, or why it can't be placed."""
    parents = run.parents
    if (
        len(parents) == 3
        and INSTRUMENT_RE.match(parents[0])
        and VISIT_RE.match(parents[1])
        and parents[2] in FOLDERS
    ):
        return parents  # type: ignore[return-value]  # already in place
    instrument = (run.instrument or "").lower()
    visit = (run.instrument_session or "").lower()
    if INSTRUMENT_RE.match(instrument) and VISIT_RE.match(visit):
        return instrument, visit, "raw"
    if len(parents) == 3 and (match := VISIT_RE.match(parents[2])):
        beamline, proposal, session = parents
        if INSTRUMENT_RE.match(beamline) and match["proposal"] == proposal:
            return beamline, session, "raw"
    return (
        f"start.instrument={run.instrument!r} "
        f"start.instrument_session={run.instrument_session!r}"
    )


@dataclass
class Plan:
    # (run, its place), for runs that move and runs already in place
    placed: list[tuple[Run, Place]]
    # (run, why it stays where it is)
    skipped: list[tuple[Run, str]]

    @property
    def moves(self) -> list[tuple[Run, Place]]:
        return [(run, p) for run, p in self.placed if run.parents != p]

    def visits(self) -> set[tuple[str, str]]:
        return {(instrument, visit) for _, (instrument, visit, _) in self.placed}


def plan(runs: list[Run], occupied: set[tuple[Place, str]]) -> Plan:
    """Place each run. `occupied` holds (folder, key) for every node already
    in a folder of a visit; a run can't move into a folder that has a node
    of its key."""
    occupied = set(occupied)
    result = Plan([], [])
    # Runs already in place first, then by id, so reruns pick the same runs
    for run in sorted(runs, key=lambda r: (place(r) != r.parents, r.id)):
        where = place(run)
        if isinstance(where, str):
            result.skipped.append((run, where))
        elif where == run.parents:
            result.placed.append((run, where))
        elif (where, run.key) in occupied:
            folder = "/".join(where)
            result.skipped.append((run, f"{folder}/{run.key} already exists"))
        else:
            occupied.add((where, run.key))
            result.placed.append((run, where))
    return result


async def current_revision(uri: str) -> str | None:
    engine = create_async_engine(uri)
    try:
        return await get_current_revision(engine, ALL_REVISIONS)
    finally:
        await engine.dispose()


async def find_runs(conn: AsyncConnection) -> list[Run]:
    rows = await conn.execute(
        text(
            "SELECT n.id, n.key, ARRAY("
            "  SELECT a.key FROM nodes_closure c JOIN nodes a ON a.id = c.ancestor"
            "  WHERE c.descendant = n.id AND c.depth > 0 AND a.id != :root"
            "  ORDER BY c.depth DESC), "
            "n.metadata #>> '{start,instrument}', "
            "n.metadata #>> '{start,instrument_session}' "
            "FROM nodes n "
            "WHERE n.metadata ? 'start' AND n.id != :root AND NOT EXISTS ("
            "  SELECT 1 FROM nodes_closure c JOIN nodes a ON a.id = c.ancestor"
            "  WHERE c.descendant = n.id AND c.depth > 0 AND a.metadata ? 'start')"
        ),
        {"root": ROOT_ID},
    )
    return [
        Run(id, key, tuple(parents), instrument, session)
        for id, key, parents, instrument, session in rows
    ]


async def find_occupied(conn: AsyncConnection) -> set[tuple[Place, str]]:
    rows = await conn.execute(
        text(
            "SELECT i.key, v.key, f.key, n.key FROM nodes i "
            "JOIN nodes v ON v.parent = i.id "
            "JOIN nodes f ON f.parent = v.id "
            "JOIN nodes n ON n.parent = f.id "
            "WHERE i.parent = :root AND f.key IN ('raw', 'processed', 'processing')"
        ),
        {"root": ROOT_ID},
    )
    return {((i, v, f), key) for i, v, f, key in rows}


async def get_or_create(conn: AsyncConnection, parent: int, key: str) -> int:
    """The container `key` under `parent`, made if missing. Containers that
    already exist (the old tree's beamline nodes, visits blueapi made) are
    kept."""
    row = (
        await conn.execute(
            text(
                "SELECT id, structure_family = 'container' AND NOT metadata ? 'start' "
                "FROM nodes WHERE parent = :parent AND key = :key"
            ),
            {"parent": parent, "key": key},
        )
    ).first()
    if row is not None:
        node_id, is_container = row
        if not is_container:
            raise SystemExit(
                f"Node {node_id} ({key!r}) is in the way of a container: it is a "
                "run or not a container. Move it, then run this again."
            )
        return node_id
    return await conn.scalar(
        text(
            "INSERT INTO nodes (parent, key, structure_family, metadata, specs) "
            "VALUES (:parent, :key, 'container', '{}', '[]') RETURNING id"
        ),
        {"parent": parent, "key": key},
    )


async def make_containers(
    conn: AsyncConnection, visits: set[tuple[str, str]]
) -> tuple[dict[Place, int], list[tuple[int, str]]]:
    """Make every visit's three folders. Returns each folder's id, and
    (node id, tag) for every container made or kept."""
    folders: dict[Place, int] = {}
    tags: dict[int, str] = {}
    instruments: dict[str, int] = {}
    for instrument, visit in sorted(visits):
        if instrument not in instruments:
            instruments[instrument] = await get_or_create(conn, ROOT_ID, instrument)
            tags[instruments[instrument]] = tag(instrument)
        visit_id = await get_or_create(conn, instruments[instrument], visit)
        tags[visit_id] = tag(instrument, visit)
        for folder in FOLDERS:
            folder_id = await get_or_create(conn, visit_id, folder)
            folders[instrument, visit, folder] = folder_id
            tags[folder_id] = tag(instrument, visit, folder)
    return folders, list(tags.items())


async def move(conn: AsyncConnection, moves: list[tuple[int, int]]) -> None:
    """Reparent each (node, new parent)."""
    if not moves:
        return
    await conn.execute(
        text(
            "CREATE TEMP TABLE moves (node_id integer PRIMARY KEY, "
            "new_parent integer NOT NULL) ON COMMIT DROP"
        )
    )
    await conn.execute(
        text("INSERT INTO moves VALUES (:node_id, :new_parent)"),
        [{"node_id": n, "new_parent": p} for n, p in moves],
    )
    # nodes_closure is only kept up to date by an insert trigger, so a
    # reparent has to fix it by hand: drop the links from each run's subtree
    # to its old ancestors, then link the subtree to every ancestor of its new
    # parent. A trigger keeps node_access_tags_association.parent_id in step.
    await conn.execute(
        text(
            "DELETE FROM nodes_closure nc USING moves m, nodes_closure sub "
            "WHERE sub.ancestor = m.node_id AND nc.descendant = sub.descendant "
            "AND nc.ancestor IN (SELECT ancestor FROM nodes_closure "
            "WHERE descendant = m.node_id AND ancestor != m.node_id)"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO nodes_closure (ancestor, descendant, depth) "
            "SELECT p.ancestor, c.descendant, p.depth + c.depth + 1 "
            "FROM moves m "
            "JOIN nodes_closure c ON c.ancestor = m.node_id "
            "JOIN nodes_closure p ON p.descendant = m.new_parent"
        )
    )
    await conn.execute(
        text(
            "UPDATE nodes n SET parent = m.new_parent "
            "FROM moves m WHERE n.id = m.node_id"
        )
    )


async def retag(
    conn: AsyncConnection, nodes: list[tuple[int, str]], subtrees: list[tuple[int, str]]
) -> tuple[int, int]:
    """Give each of `nodes`, and every node in each of `subtrees`, exactly the
    tag paired with it. Returns (tags removed, tags added)."""
    if not nodes + subtrees:
        return 0, 0
    names = {name for _, name in nodes + subtrees}
    await register_tag_rows(conn, ApprovedAccessTags(names))
    await conn.execute(
        text(
            "CREATE TEMP TABLE wanted (node_id integer PRIMARY KEY, "
            "tag_id integer NOT NULL) ON COMMIT DROP"
        )
    )
    await conn.execute(
        text(
            "CREATE TEMP TABLE wanted_roots (node_id integer PRIMARY KEY, "
            "name text NOT NULL, subtree boolean NOT NULL) ON COMMIT DROP"
        )
    )
    await conn.execute(
        text("INSERT INTO wanted_roots VALUES (:node_id, :name, :subtree)"),
        [{"node_id": n, "name": name, "subtree": False} for n, name in nodes]
        + [{"node_id": n, "name": name, "subtree": True} for n, name in subtrees],
    )
    await conn.execute(
        text(
            "INSERT INTO wanted "
            "SELECT c.descendant, t.id FROM wanted_roots r "
            "JOIN access_tags t ON t.name = r.name "
            "JOIN nodes_closure c ON c.ancestor = r.node_id "
            "AND (r.subtree OR c.depth = 0)"
        )
    )
    removed = await conn.execute(
        text(
            "DELETE FROM node_access_tags_association a USING wanted w "
            "WHERE a.node_id = w.node_id AND a.tag_id != w.tag_id"
        )
    )
    added = await conn.execute(
        text(
            "INSERT INTO node_access_tags_association (node_id, tag_id) "
            "SELECT node_id, tag_id FROM wanted "
            "ON CONFLICT ON CONSTRAINT node_access_tags_association_pkey DO NOTHING"
        )
    )
    return removed.rowcount, added.rowcount


# The old tree's session containers, e.g. i22/cm12345/cm12345-1, and its
# proposal containers, e.g. i22/cm12345, that hold nothing (any more)
_EMPTY = (
    "NOT EXISTS (SELECT 1 FROM nodes c WHERE c.parent = {n}.id) "
    "AND NOT EXISTS (SELECT 1 FROM data_sources d WHERE d.node_id = {n}.id) "
    "AND {n}.structure_family = 'container' AND NOT {n}.metadata ? 'start'"
)
OLD_SESSIONS = (
    "SELECT s.id FROM nodes s JOIN nodes p ON p.id = s.parent "
    "JOIN nodes b ON b.id = p.parent "
    "WHERE b.parent = :root AND p.key ~ '^[a-z]+[0-9]+$' "
    "AND s.key ~ ('^' || p.key || '-[0-9]+$') AND " + _EMPTY.format(n="s")
)
OLD_PROPOSALS = (
    "SELECT p.id FROM nodes p JOIN nodes b ON b.id = p.parent "
    "WHERE b.parent = :root AND p.key ~ '^[a-z]+[0-9]+$' AND " + _EMPTY.format(n="p")
)


async def delete_old_containers(conn: AsyncConnection) -> int:
    deleted = 0
    for query in (OLD_SESSIONS, OLD_PROPOSALS):
        result = await conn.execute(
            text(f"DELETE FROM nodes WHERE id IN ({query})"), {"root": ROOT_ID}
        )
        deleted += result.rowcount
    return deleted


def report(result: Plan) -> None:
    for run, why in result.skipped:
        where = "/".join((*run.parents, run.key))
        print(f"Leaving {where} where it is: {why}")
    counts = Counter("/".join(p) for _, p in result.moves)
    for where, n in sorted(counts.items()):
        print(f"  {where}: {n} runs to move in")
    print(
        f"{len(result.moves)} runs to move, "
        f"{len(result.placed) - len(result.moves)} already in place, "
        f"{len(result.skipped)} left where they are."
    )


async def migrate(uri: str, dry_run: bool) -> None:
    engine = create_async_engine(uri)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            # Reads go on; anything writing to the tree waits for this
            await conn.execute(text("LOCK TABLE nodes IN SHARE ROW EXCLUSIVE MODE"))
            result = plan(await find_runs(conn), await find_occupied(conn))
            report(result)

            folders, containers = await make_containers(conn, result.visits())
            await move(conn, [(run.id, folders[p]) for run, p in result.moves])
            removed, added = await retag(
                conn,
                containers,
                [(run.id, tag(*p)) for run, p in result.placed],
            )
            deleted = await delete_old_containers(conn)
            print(
                f"Moved {len(result.moves)} runs, removed {removed} tags and "
                f"added {added}, deleted {deleted} empty old containers."
            )
            if dry_run:
                await transaction.rollback()
                print("Dry run: rolled back, nothing changed.")
            else:
                await transaction.commit()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("database_uri", help="e.g. postgresql://user:pass@host/db")
    parser.add_argument(
        "--revision",
        default="head",
        help="tiled catalog schema revision to upgrade to first (default: head)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="skip the schema upgrade, and roll the migration back at the end, "
        "printing what it would do",
    )
    args = parser.parse_args()
    uri = ensure_specified_sql_driver(args.database_uri)
    if not uri.startswith("postgresql"):
        raise SystemExit("Only Postgres catalogs can be migrated.")

    if (revision := asyncio.run(current_revision(uri))) is None:
        raise SystemExit("Database is not a tiled catalog; run `tiled catalog init`.")
    if not args.dry_run:
        # Alembic runs its own event loop, so this has to sit outside asyncio.run.
        upgrade(Path(ALEMBIC_INI_TEMPLATE_PATH), Path(ALEMBIC_DIR), uri, args.revision)
        revision = asyncio.run(current_revision(uri))
    print(f"Catalog schema at revision {revision}")
    if revision not in ALL_REVISIONS or ALL_REVISIONS.index(
        revision
    ) > ALL_REVISIONS.index(TAGS_REVISION):
        raise SystemExit(
            f"Revision {revision} predates access tag tables with parents "
            f"({TAGS_REVISION}). Upgrade to it or later first; a dry run "
            "doesn't upgrade, so try it on a restored copy that has been."
        )
    asyncio.run(migrate(uri, args.dry_run))


if __name__ == "__main__":
    main()
