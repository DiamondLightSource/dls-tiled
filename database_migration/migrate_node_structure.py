#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tiled[server]==0.2.18"]
# ///
"""Move runs from the flat catalog layout into the beamline/proposal/session tree.

Before:  root/<run uid>
After:   root/<beamline>/<proposal>/<instrument session>/<run uid>
         e.g. root/i22/cm12345/cm12345-1/<run uid>

This is the layout blueapi's begin_task now writes into. The tiled schema is
upgraded first (to --revision, default head) so the move runs against the
nodes/nodes_closure layout this script expects.

The new containers get the same access tags tiled.rego stamps on nodes it
creates (its Node Tags: the node's own tag, then each ancestor's grant tag),
and every tagged node under a moved run is retagged with its session's tags.
Runs written under the old policy carry a bare numeric session id, which the
new policy's user_sessions never returns, so they would become invisible to
users otherwise.

Nodes already in the tree tagged with only their own composite tag (made by
blueapi under an earlier version of the policy, or by an earlier run of this
script) get the full tag list too; without the ancestor tags a beamline or
proposal grant would not reach them.

Runs at the root without start.instrument / start.instrument_session, or whose
instrument_session doesn't parse, are left where they are and listed. Their
bare numeric session id would make them invisible to everyone but admins, so
with --authz-data (the authz bundle's data.json) any node still tagged with one
is retagged in place from that session's beamline, proposal and visit. They
can't be moved: the data has the proposal number but not its code ("cm"), which
the tree's keys use. Ids the data doesn't know are listed. Safe to
rerun: anything already moved is no longer a child of the root.

Postgres only.

Example:
    uv run --script database_migration/migrate_node_structure.py \\
        postgresql://user:pass@host:5432/tiled --revision head --dry-run
"""

import argparse
import asyncio
import json
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tiled.alembic_utils import get_current_revision, upgrade
from tiled.catalog.alembic_constants import ALEMBIC_DIR, ALEMBIC_INI_TEMPLATE_PATH
from tiled.catalog.core import ALL_REVISIONS
from tiled.utils import ensure_specified_sql_driver

ROOT_ID = 0
# The closure table migration adds the explicit root node (id 0) and
# nodes_closure, both of which the move below relies on.
CLOSURE_REVISION = "e05e918092c3"
# Same as blueapi's INSTRUMENT_SESSION_RE, e.g. "cm12345-1"
INSTRUMENT_SESSION_RE = re.compile(
    r"^(?P<proposal>[a-z]{2}(?P<proposal_number>\d+))-(?P<visit>\d+)$"
)


# A node's own tag as tiled.rego's beamline_tag/proposal_tag/session_tag build it
COMPOSITE_TAG_RE = re.compile(
    r"^beamline:(?P<beamline>[^,]+)"
    r"(?:,proposal:(?P<proposal>\d+)(?:,session:(?P=proposal)-(?P<visit>\d+))?)?$"
)


def tags(beamline: str, proposal_number: int | None = None, visit=None) -> dict:
    """The access blob tiled.rego stamps on a node at this level (its Node Tags):
    the node's own tag first, then each ancestor's grant tag so a beamline or
    proposal grant covers the subtree, and on the two container levels a
    visible: tag so callers granted something below can still list them."""
    beamline_tag = f"beamline:{beamline}"
    if proposal_number is None:
        return {"tags": [beamline_tag, f"visible:{beamline_tag}"]}
    proposal_tag = f"{beamline_tag},proposal:{proposal_number}"
    if visit is None:
        return {"tags": [proposal_tag, beamline_tag, f"visible:{proposal_tag}"]}
    session_tag = f"{proposal_tag},session:{proposal_number}-{visit}"
    return {"tags": [session_tag, proposal_tag, beamline_tag]}


def tags_from_own_tag(tag: str, sessions: dict | None = None) -> dict | None:
    """The full access blob for a node tagged with just its own composite tag,
    or with a bare numeric session id found in `sessions` (authz data's
    sessions, keyed by id); None if `tag` is neither."""
    if tag.isdigit():
        if (session := (sessions or {}).get(tag)) is None:
            return None
        return tags(
            session["beamline"], session["proposal_number"], session["visit_number"]
        )
    if (match := COMPOSITE_TAG_RE.match(tag)) is None:
        return None
    number, visit = match["proposal"], match["visit"]
    return tags(
        match["beamline"],
        int(number) if number else None,
        int(visit) if visit else None,
    )


def plan_moves(runs):
    """Split root-level runs into (moves, skipped).

    moves: (node_id, [(key, access_blob) for beamline, proposal, session])
    """
    moves, skipped = [], []
    for node_id, key, beamline, session in runs:
        match = INSTRUMENT_SESSION_RE.match(session or "")
        if not beamline or match is None:
            skipped.append((key, beamline, session))
            continue
        number, visit = int(match["proposal_number"]), int(match["visit"])
        path = [
            (beamline, tags(beamline)),
            (match["proposal"], tags(beamline, number)),
            (session, tags(beamline, number, visit)),
        ]
        moves.append((node_id, path))
    return moves, skipped


async def current_revision(uri: str) -> str | None:
    engine = create_async_engine(uri)
    try:
        return await get_current_revision(engine, ALL_REVISIONS)
    finally:
        await engine.dispose()


async def get_or_create(conn, parent: int, key: str, access_blob: dict) -> int:
    # Containers blueapi already made are kept as they are.
    node_id = await conn.scalar(
        text(
            "INSERT INTO nodes (parent, key, structure_family, metadata, specs, "
            "access_blob) VALUES (:parent, :key, 'container', '{}', '[]', "
            "CAST(:blob AS jsonb)) ON CONFLICT ON CONSTRAINT "
            "key_parent_unique_constraint DO NOTHING RETURNING id"
        ),
        {"parent": parent, "key": key, "blob": json.dumps(access_blob)},
    )
    if node_id is None:
        node_id = await conn.scalar(
            text("SELECT id FROM nodes WHERE parent = :parent AND key = :key"),
            {"parent": parent, "key": key},
        )
    return node_id


async def migrate(uri: str, dry_run: bool, sessions: dict | None = None) -> None:
    engine = create_async_engine(uri)
    try:
        async with engine.begin() as conn:
            runs = (
                await conn.execute(
                    text(
                        "SELECT id, key, metadata #>> '{start,instrument}', "
                        "metadata #>> '{start,instrument_session}' "
                        "FROM nodes WHERE parent = :root"
                    ),
                    {"root": ROOT_ID},
                )
            ).all()
            moves, skipped = plan_moves(runs)

            # Root children with no start doc are the new beamline containers
            # (or anything else that isn't a run) - not worth reporting.
            for key, beamline, session in skipped:
                if beamline or session:
                    print(f"Skipping {key}: {beamline=} {session=}")
            counts: dict[str, int] = {}
            for _, path in moves:
                where = "/".join(key for key, _ in path)
                counts[where] = counts.get(where, 0) + 1
            for where, n in sorted(counts.items()):
                print(f"  root/{where}: {n} runs")
            print(f"{len(moves)} runs to move into {len(counts)} sessions.")

            # Nodes still carrying only their own composite tag, by that tag.
            single = (
                await conn.execute(
                    text(
                        "SELECT access_blob -> 'tags' ->> 0, count(*) FROM nodes "
                        "WHERE jsonb_typeof(access_blob -> 'tags') = 'array' "
                        "AND jsonb_array_length(access_blob -> 'tags') = 1 "
                        "GROUP BY 1"
                    )
                )
            ).all()
            retags, to_retag, unknown = [], 0, 0
            for tag, n in single:
                if (blob := tags_from_own_tag(tag, sessions)) is not None:
                    retags.append({"old": json.dumps([tag]), "blob": json.dumps(blob)})
                    to_retag += n
                elif tag.isdigit():
                    unknown += n
            print(f"{to_retag} nodes to give their full tags.")
            if unknown:
                # Includes the runs moved below, which get their tags from the move.
                where = "not in --authz-data" if sessions else "(pass --authz-data)"
                print(f"{unknown} nodes carry a bare session id {where}.")
            if dry_run or not (moves or retags):
                print("Nothing changed.")
                return

            if retags:
                # One join, so nodes is scanned once rather than once per tag
                # (the GIN index on access_blob can't serve -> 'tags' = ...).
                await conn.execute(
                    text(
                        "CREATE TEMP TABLE retags (old jsonb PRIMARY KEY, "
                        "blob jsonb NOT NULL) ON COMMIT DROP"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO retags VALUES "
                        "(CAST(:old AS jsonb), CAST(:blob AS jsonb))"
                    ),
                    retags,
                )
                await conn.execute(
                    text(
                        "UPDATE nodes n SET access_blob = r.blob FROM retags r "
                        "WHERE n.access_blob -> 'tags' = r.old"
                    )
                )
                print(f"Gave {to_retag} nodes their full tags.")
            if not moves:
                return

            containers: dict[tuple, int] = {}
            rows = []
            for node_id, path in moves:
                parent = ROOT_ID
                for depth in range(1, len(path) + 1):
                    keys = tuple(key for key, _ in path[:depth])
                    if keys not in containers:
                        key, blob = path[depth - 1]
                        containers[keys] = await get_or_create(conn, parent, key, blob)
                    parent = containers[keys]
                rows.append(
                    {
                        "node_id": node_id,
                        "new_parent": parent,
                        "blob": json.dumps(path[-1][1]),
                    }
                )

            await conn.execute(
                text(
                    "CREATE TEMP TABLE moves (node_id integer PRIMARY KEY, "
                    "new_parent integer NOT NULL, blob jsonb NOT NULL) "
                    "ON COMMIT DROP"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO moves VALUES "
                    "(:node_id, :new_parent, CAST(:blob AS jsonb))"
                ),
                rows,
            )
            # nodes_closure is only kept up to date by an insert trigger, so a
            # reparent has to fix it by hand: drop the links from each run's
            # subtree to its old ancestors (just the root), then link the
            # subtree to every ancestor of its new parent.
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
            # Untagged ({}) nodes are public under the DLS policy; leave them.
            retagged = await conn.execute(
                text(
                    "UPDATE nodes n SET access_blob = m.blob "
                    "FROM moves m JOIN nodes_closure c ON c.ancestor = m.node_id "
                    "WHERE n.id = c.descendant AND n.access_blob -> 'tags' IS NOT NULL"
                )
            )
            print(
                f"Moved {len(rows)} runs, {len(containers)} containers in place, "
                f"retagged {retagged.rowcount} nodes."
            )
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
        "--authz-data",
        type=argparse.FileType(),
        help="the authz bundle's data.json, to retag nodes still carrying a bare "
        "numeric session id (e.g. runs left at the root) from that session",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the upgrade and only print what would move",
    )
    args = parser.parse_args()
    uri = ensure_specified_sql_driver(args.database_uri)

    if (revision := asyncio.run(current_revision(uri))) is None:
        raise SystemExit("Database is not a tiled catalog; run `tiled catalog init`.")
    if not args.dry_run:
        # Alembic runs its own event loop, so this has to sit outside asyncio.run.
        upgrade(ALEMBIC_INI_TEMPLATE_PATH, ALEMBIC_DIR, uri, args.revision)
        revision = asyncio.run(current_revision(uri))
    print(f"Catalog schema at revision {revision}")
    if revision not in ALL_REVISIONS or ALL_REVISIONS.index(
        revision
    ) > ALL_REVISIONS.index(CLOSURE_REVISION):
        raise SystemExit(
            f"Revision {revision} predates the closure table ({CLOSURE_REVISION}); "
            "upgrade to it or later before migrating."
        )
    sessions = None
    if args.authz_data:
        data = json.load(args.authz_data)
        sessions = data.get("diamond", {}).get("data", data)["sessions"]
    asyncio.run(migrate(uri, args.dry_run, sessions))


if __name__ == "__main__":
    main()
