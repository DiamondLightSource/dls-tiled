"""

Example:
    python scripts/acqp568_migrate_node_structure.py \
        postgresql+asyncpg://postgres:<password>@localhost:5432/tiled-db --dry-run
"""

import argparse
import asyncio
from collections import defaultdict

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

        result = await session.execute(
            select(
                Node.id,
                Node.key,
                Node.metadata_[["start", "instrument"]].as_string().label("instrument"),
                Node.metadata_[["start", "instrument_session"]]
                .as_string()
                .label("session"),
            ).where(Node.parent == root_id)
        )
        runs = result.all()

        missing = [r for r in runs if r.instrument is None or r.session is None]
        if missing:
            raise ValueError(
                f"{len(missing)} run(s) missing instrument/instrument_session "
                f"metadata, refusing to proceed: {[r.key for r in missing][:10]}"
            )

        # instrument_session is "<proposal>-<visit>" (e.g. "cm44254-3"); split
        # on the last '-' so a proposal code containing '-' is handled correctly.
        splits = {}
        malformed = []
        for r in runs:
            proposal, sep, visit = r.session.rpartition("-")
            if not sep or not proposal or not visit:
                malformed.append(r)
            else:
                splits[r.id] = (proposal, visit)
        if malformed:
            raise ValueError(
                f"{len(malformed)} run(s) have an instrument_session that doesn't "
                f"split into '<proposal>-<visit>', refusing to proceed: "
                f"{[r.key for r in malformed][:10]}"
            )

        by_target = defaultdict(list)
        for run in runs:
            proposal, visit = splits[run.id]
            by_target[(run.instrument, proposal, visit)].append(run)

        print(
            f"Found {len(runs)} runs across {len({r.instrument for r in runs})} "
            f"instruments and {len(by_target)} instrument/proposal/visit triples."
        )
        for (instrument, proposal, visit), group in sorted(by_target.items()):
            print(f"  root/{instrument}/{proposal}/{visit}: {len(group)} runs")

        if dry_run:
            print("Dry run -- no changes made.")
            return

        before_count = (
            await session.execute(select(func.count()).select_from(Node))
        ).scalar_one()

        instrument_ids = {}
        proposal_ids = {}
        visit_ids = {}
        containers_created = 0
        for instrument, proposal, visit in by_target:
            if instrument not in instrument_ids:
                instrument_ids[instrument], created = await get_or_create_container(
                    session, root_id, instrument
                )
                if created:
                    containers_created += 1
                    print(f"Created container root/{instrument}")
            if (instrument, proposal) not in proposal_ids:
                (
                    proposal_ids[(instrument, proposal)],
                    created,
                ) = await get_or_create_container(
                    session, instrument_ids[instrument], proposal
                )
                if created:
                    containers_created += 1
                    print(f"Created container root/{instrument}/{proposal}")
            (
                visit_ids[(instrument, proposal, visit)],
                created,
            ) = await get_or_create_container(
                session, proposal_ids[(instrument, proposal)], visit
            )
            if created:
                containers_created += 1
                print(f"Created container root/{instrument}/{proposal}/{visit}")

        # Build the move list once, then operate on all of it in a handful
        # of set-based statements instead of looping node-by-node.
        moves = [(run.id, visit_ids[(run.instrument, *splits[run.id])]) for run in runs]

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

        # Batched safety checks (structurally these should never fire, since
        # every target is a container that didn't exist a moment ago, and
        # every run has a unique key -- but cheap insurance regardless).
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
        # to its subtree. Rows internal to each run's own subtree are
        # untouched, since none of a run's descendants are themselves being
        # moved.
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

        # Reattach: connect each run's NEW ancestor chain to its subtree.
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

        after_count = (
            await session.execute(select(func.count()).select_from(Node))
        ).scalar_one()
        expected = before_count + containers_created
        if after_count != expected:
            raise ValueError(
                f"Post-move node count ({after_count}) does not match expected "
                f"({expected} = {before_count} before + {containers_created} new "
                "containers) -- rolling back rather than committing."
            )

        await session.commit()
        print(
            f"Moved {len(moves)} runs into {containers_created} new container(s). "
            f"Node count: {before_count} -> {after_count}."
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
