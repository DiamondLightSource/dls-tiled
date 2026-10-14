"""Variant of tiled_data_restructure_script.py for catalogs that have
already been migrated once to root/<instrument>/<instrument_session>/<run>
(the two-level structure) and now need the extra proposal/visit split:
root/<instrument>/<proposal>/<visit>/<run>.

Unlike the original script (which reads instrument/instrument_session off
each run's own metadata and assumes runs sit flat under root), this one
reads the proposal/visit split from each existing SESSION CONTAINER's own
key (e.g. "cm44254-1") and moves that container's children -- it doesn't
touch run metadata at all.

Example:
    python tiled_data_restructure_split_existing_sessions.py \
        postgresql+asyncpg://postgres:<password>@localhost:5432/tiled-db --dry-run
"""

import argparse
import asyncio

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from tiled.catalog.orm import Node
from tiled.structures.core import StructureFamily


async def get_root_id(session):
    result = await session.execute(select(Node.id).where(Node.parent.is_(None)))
    return result.scalar_one()


async def get_or_create_container(session, parent_id, key):
    """Insert a container node if one doesn't already exist under parent_id
    with this key. Returns (id, created). Uses ON CONFLICT rather than
    check-then-insert, matching the pattern tiled's own create_node uses,
    so this is race-free even if run concurrently.
    """
    stmt = (
        pg_insert(Node)
        .values(
            key=key,
            parent=parent_id,
            metadata_={},
            structure_family=StructureFamily.container,
            specs=[],
            access_blob={},
        )
        .on_conflict_do_nothing(constraint="key_parent_unique_constraint")
        .returning(Node.id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is not None:
        return row.id, True
    result = await session.execute(
        select(Node.id).where(Node.parent == parent_id, Node.key == key)
    )
    return result.scalar_one(), False


async def main(database_uri, dry_run):
    engine = create_async_engine(database_uri)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # noqa: N806

    async with Session() as session:
        root_id = await get_root_id(session)

        instruments = (
            await session.execute(
                select(Node.id, Node.key, Node.structure_family).where(
                    Node.parent == root_id
                )
            )
        ).all()
        non_containers = [
            i for i in instruments if i.structure_family != StructureFamily.container
        ]
        if non_containers:
            raise ValueError(
                f"{len(non_containers)} node(s) directly under root aren't "
                f"containers -- expected only instrument containers here "
                f"(has the two-level migration already run?): "
                f"{[i.key for i in non_containers][:10]}"
            )
        instrument_by_id = {i.id: i.key for i in instruments}

        sessions = (
            await session.execute(
                select(Node.id, Node.key, Node.parent, Node.structure_family).where(
                    Node.parent.in_(list(instrument_by_id))
                )
            )
        ).all()
        non_containers = [
            s for s in sessions if s.structure_family != StructureFamily.container
        ]
        if non_containers:
            raise ValueError(
                f"{len(non_containers)} node(s) directly under an instrument "
                f"aren't containers -- expected only session containers here: "
                f"{[s.key for s in non_containers][:10]}"
            )

        malformed = []
        splits = {}
        for s in sessions:
            proposal, sep, visit = s.key.rpartition("-")
            if not sep or not proposal or not visit:
                malformed.append(s)
            else:
                splits[s.id] = (proposal, visit)
        if malformed:
            raise ValueError(
                f"{len(malformed)} session container(s) don't split into "
                f"'<proposal>-<visit>', refusing to proceed: "
                f"{[s.key for s in malformed][:10]}"
            )

        child_counts = dict(
            (
                await session.execute(
                    select(Node.parent, func.count())
                    .where(Node.parent.in_([s.id for s in sessions]))
                    .group_by(Node.parent)
                )
            ).all()
        )

        to_split = [s for s in sessions if child_counts.get(s.id, 0) > 0]
        empty = [s for s in sessions if child_counts.get(s.id, 0) == 0]

        print(
            f"Found {len(sessions)} session container(s) across "
            f"{len(instrument_by_id)} instrument(s); {len(to_split)} have "
            f"runs to move, {len(empty)} are already empty and will be left "
            "alone."
        )
        for s in sorted(to_split, key=lambda s: (instrument_by_id[s.parent], s.key)):
            proposal, visit = splits[s.id]
            print(
                f"  root/{instrument_by_id[s.parent]}/{s.key} "
                f"-> root/{instrument_by_id[s.parent]}/{proposal}/{visit}: "
                f"{child_counts[s.id]} runs"
            )
        if empty:
            print(f"  (skipping, no children): {[s.key for s in empty][:10]}")

        if dry_run:
            print("Dry run -- no changes made.")
            return

        if not to_split:
            print("Nothing to do.")
            return

        before_count = (
            await session.execute(select(func.count()).select_from(Node))
        ).scalar_one()

        proposal_ids = {}
        visit_ids = {}
        containers_created = 0
        for s in to_split:
            instrument_id = s.parent
            proposal, visit = splits[s.id]
            if (instrument_id, proposal) not in proposal_ids:
                (
                    proposal_ids[(instrument_id, proposal)],
                    created,
                ) = await get_or_create_container(session, instrument_id, proposal)
                if created:
                    containers_created += 1
                    print(
                        f"Created container root/{instrument_by_id[instrument_id]}"
                        f"/{proposal}"
                    )
            visit_ids[s.id], created = await get_or_create_container(
                session, proposal_ids[(instrument_id, proposal)], visit
            )
            if created:
                containers_created += 1
                print(
                    f"Created container root/{instrument_by_id[instrument_id]}"
                    f"/{proposal}/{visit}"
                )

        runs = (
            await session.execute(
                select(Node.id, Node.parent).where(
                    Node.parent.in_([s.id for s in to_split])
                )
            )
        ).all()
        moves = [(r.id, visit_ids[r.parent]) for r in runs]

        await session.execute(
            text(
                "CREATE TEMP TABLE moves "
                "(node_id integer PRIMARY KEY, new_parent_id integer NOT NULL) "
                "ON COMMIT DROP"
            )
        )
        await session.execute(
            text(
                "INSERT INTO moves (node_id, new_parent_id) "
                "VALUES (:node_id, :new_parent_id)"
            ),
            [{"node_id": n, "new_parent_id": p} for n, p in moves],
        )

        # Batched safety checks -- structurally these should never fire
        # (every target is a container that either pre-dates this run or
        # was just created, and every moved node has a unique key) but
        # cheap insurance regardless.
        cycle_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM moves m
                    WHERE EXISTS (
                        SELECT 1 FROM nodes_closure
                        WHERE ancestor = m.node_id AND descendant = m.new_parent_id
                    )
                    """
                )
            )
        ).scalar_one()
        if cycle_count:
            raise ValueError(f"{cycle_count} move(s) would create a cycle -- aborting.")

        collision_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM moves m
                    WHERE EXISTS (
                        SELECT 1 FROM nodes n2
                        WHERE n2.parent = m.new_parent_id
                          AND n2.key = (SELECT key FROM nodes WHERE id = m.node_id)
                          AND n2.id != m.node_id
                    )
                    """
                )
            )
        ).scalar_one()
        if collision_count:
            raise ValueError(
                f"{collision_count} move(s) would collide on key -- aborting."
            )

        # Detach: remove closure rows linking each run's OLD ancestor chain
        # (through the old session container) to its subtree.
        await session.execute(
            text(
                """
                DELETE FROM nodes_closure
                WHERE descendant IN (
                    SELECT nc.descendant FROM nodes_closure nc
                    JOIN moves m ON nc.ancestor = m.node_id
                )
                AND ancestor IN (
                    SELECT nc2.ancestor FROM nodes_closure nc2
                    JOIN moves m ON nc2.descendant = m.node_id
                    WHERE nc2.ancestor != m.node_id
                )
                """
            )
        )

        # Reattach: connect each run's NEW ancestor chain (through the new
        # proposal/visit containers) to its subtree.
        await session.execute(
            text(
                """
                INSERT INTO nodes_closure (ancestor, descendant, depth)
                SELECT p.ancestor, c.descendant, p.depth + c.depth + 1
                FROM moves m
                JOIN nodes_closure c ON c.ancestor = m.node_id
                JOIN nodes_closure p ON p.descendant = m.new_parent_id
                """
            )
        )

        # Update the adjacency pointer itself, for all moved runs at once.
        await session.execute(
            text(
                """
                UPDATE nodes n
                SET parent = m.new_parent_id
                FROM moves m
                WHERE n.id = m.node_id
                """
            )
        )

        # Old session containers should now be empty -- confirm that before
        # deleting them, since a delete cascades to any remaining children.
        session_ids_to_delete = [s.id for s in to_split]
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(Node)
                .where(Node.parent.in_(session_ids_to_delete))
            )
        ).scalar_one()
        if remaining:
            raise ValueError(
                f"{remaining} node(s) still parented under an old session "
                "container after the move -- refusing to delete it, aborting."
            )

        await session.execute(
            text("DELETE FROM nodes WHERE id = ANY(:ids)"),
            {"ids": session_ids_to_delete},
        )

        after_count = (
            await session.execute(select(func.count()).select_from(Node))
        ).scalar_one()
        expected = before_count + containers_created - len(session_ids_to_delete)
        if after_count != expected:
            raise ValueError(
                f"Post-move node count ({after_count}) does not match expected "
                f"({expected} = {before_count} before + {containers_created} new "
                f"containers - {len(session_ids_to_delete)} emptied session "
                "containers removed) -- rolling back rather than committing."
            )

        await session.commit()
        print(
            f"Moved {len(moves)} runs into {containers_created} new container(s) "
            f"and removed {len(session_ids_to_delete)} emptied session "
            f"container(s). Node count: {before_count} -> {after_count}."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database_uri", help="e.g. postgresql+asyncpg://user:pass@host:5432/dbname"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the mapping without making changes",
    )
    args = parser.parse_args()
    asyncio.run(main(args.database_uri, args.dry_run))
