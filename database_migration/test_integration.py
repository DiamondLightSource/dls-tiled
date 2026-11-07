"""Migrate a real tiled catalog and read it back through dls-tiled, Keycloak and
the central authorisation OPA.

Needs docker compose, uv and a checkout of the authorisation repo next to this
one (or AUTHORISATION_REPO pointing at it). Runs against a small catalog
seeded in the schema of tiled 0.2.18, with both of the layouts before the
tree (what CI does):

    uv run pytest database_migration/test_integration.py

or against a custom-format dump of the real database, e.g. backup/tiled.dump
from README step 2:

    PROD_DUMP=backup/tiled.dump uv run pytest database_migration/test_integration.py

The stack in integration/compose.yaml is torn down afterwards; set KEEP_STACK=1
to leave it up for poking at. COMPOSE overrides the compose command, e.g.
COMPOSE="podman compose".

What it checks:
  - a dry run changes nothing
  - every run the migration places is at <instrument>/<visit>/<folder>/<run>
    with its metadata and whole subtree, every node in it tagged with the
    folder's tag; runs it can't place are untouched
  - every node above them has exactly the tag of its place
  - nodes_closure and the tags' parent_id still match the tree
  - a second run changes nothing
  - through tiled: an admin reads every run, a user on one visit sees only
    that visit, and can write in its processing folder but not in raw
Array data isn't read back: the dump holds the catalog, not the files.
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from migrate_to_central_authz import FOLDERS, find_occupied, find_runs, plan, tag
from sqlalchemy.ext.asyncio import create_async_engine

HERE = Path(__file__).parent
SCRIPT = HERE / "migrate_to_central_authz.py"
STACK = HERE / "integration"
COMPOSE = [
    *shlex.split(os.environ.get("COMPOSE", "docker compose")),
    "-f",
    str(STACK / "compose.yaml"),
]
URI = "postgresql://postgres:postgres@localhost:5433/tiled"
KEYCLOAK = "http://localhost:8082"
TILED = "http://localhost:8408/api/v1"
DUMP = os.environ.get("PROD_DUMP")
# The tiled release before the access tag tables, which the seed is made with
OLD_TILED = "tiled[server]==0.2.18"


def compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([*COMPOSE, *args], check=True, **kwargs)


def psql(query: str) -> str:
    return compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "tiled",
        "-v",
        "ON_ERROR_STOP=1",
        "-AtXc",
        query,
        capture_output=True,
        text=True,
    ).stdout


def sql(query: str):
    """Run a query that returns one JSON value and decode it."""
    return json.loads(psql(query))


def migrate(*args: str) -> str:
    """Run the script the way the README does, with its own dependencies."""
    out = subprocess.run(
        ["uv", "run", "--script", str(SCRIPT), URI, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    print(out)
    return out


def start(session: str | None, instrument: str = "adsim") -> dict:
    doc = {"instrument": instrument}
    return doc if session is None else {**doc, "instrument_session": session}


OLD = "beamline:adsim,proposal:12345"
# (parent path, key, start document or None for a plain container, old tags)
SEED = [
    # flat
    ("", "run-1", start("cm12345-1"), ["1"]),
    ("", "run-2", start("cm12345-1"), ["1"]),
    ("", "run-3", start("CM12345-2", "ADSIM"), ["2"]),
    ("", "run-4", start("cm12345-2"), ["2"]),
    ("", "no-session", start(None), ["1"]),
    ("", "bad-session", start("x"), ["1"]),
    # the old tree
    ("", "adsim", None, ["beamline:adsim", "visible:beamline:adsim"]),
    ("adsim", "cm12345", None, [OLD]),
    ("adsim/cm12345", "cm12345-2", None, [f"{OLD},session:12345-2"]),
    ("adsim/cm12345", "cm12345-9", None, [f"{OLD},session:12345-9"]),
    # the same key as the flat run-4, which gets there first
    ("adsim/cm12345/cm12345-2", "run-4", start("cm12345-2"), ["x"]),
    # placed by its path
    ("adsim/cm12345/cm12345-2", "run-5", {"uid": "run-5"}, ["x"]),
]
# Where each seeded run should end up; the rest stay put
SEED_PLACES = {
    "run-1": "adsim/cm12345-1/raw/run-1",
    "run-2": "adsim/cm12345-1/raw/run-2",
    "run-3": "adsim/cm12345-2/raw/run-3",
    "run-4": "adsim/cm12345-2/raw/run-4",
    "run-5": "adsim/cm12345-2/raw/run-5",
}


def seed() -> None:
    """A catalog in tiled 0.2.18's schema holding SEED, each run with a tagged
    child and grandchild and an untagged child."""
    subprocess.run(
        ["uv", "run", "--no-project", "--with", OLD_TILED]
        + ["tiled", "catalog", "init", URI],
        check=True,
    )
    columns = "(parent, key, structure_family, metadata, specs, access_blob)"

    def insert(parent: str, key: str, metadata: dict, tags: list | None) -> None:
        blob = json.dumps({"tags": tags} if tags is not None else {})
        # One statement per node, so the closure trigger sees its parent
        psql(
            f"INSERT INTO nodes {columns} SELECT id, '{key}', 'container', "
            f"'{json.dumps(metadata)}', '[]', '{blob}' FROM nodes "
            f"WHERE id = (SELECT descendant FROM nodes_closure c WHERE ancestor = 0 "
            f"AND {path_condition(parent)})"
        )

    for parent, key, start, tags in SEED:
        insert(parent, key, {"start": start} if start else {}, tags)
        if start:
            run = f"{parent}/{key}".strip("/")
            insert(run, "primary", {}, tags)
            insert(f"{run}/primary", "data", {"shape": [1]}, tags)
            insert(run, "untagged", {}, None)


def path_condition(path: str) -> str:
    """SQL on nodes_closure c: c.descendant is the node at `path`."""
    keys = [k for k in path.split("/") if k]
    if not keys:
        return "c.descendant = 0"
    return (
        f"c.depth = {len(keys)} AND (SELECT array_agg(a.key ORDER BY x.depth DESC) "
        "FROM nodes_closure x JOIN nodes a ON a.id = x.ancestor "
        "WHERE x.descendant = c.descendant AND x.ancestor != 0) = "
        f"ARRAY{keys}::varchar[]"
    )


NODES = (
    "SELECT coalesce(json_object_agg(n.id, json_build_object("
    "'path', (SELECT string_agg(a.key, '/' ORDER BY c.depth DESC) "
    "  FROM nodes_closure c JOIN nodes a ON a.id = c.ancestor "
    "  WHERE c.descendant = n.id AND c.ancestor != 0), "
    "'parent', n.parent, 'metadata', n.metadata, "
    "'tags', (SELECT coalesce(json_agg(t.name ORDER BY t.name), '[]') "
    "  FROM node_access_tags_association x JOIN access_tags t ON t.id = x.tag_id "
    "  WHERE x.node_id = n.id))), '{}') FROM nodes n WHERE n.id != 0"
)


def nodes() -> dict[int, dict]:
    """Every node but the root, by id, with its path, metadata and tags."""
    return {int(k): v for k, v in sql(NODES).items()}


async def _plan():
    engine = create_async_engine(URI.replace("postgresql", "postgresql+asyncpg"))
    try:
        async with engine.connect() as conn:
            return plan(await find_runs(conn), await find_occupied(conn))
    finally:
        await engine.dispose()


def make_client(admin: httpx.Client, fedid: str) -> None:
    """A service account whose tokens carry fedid and the audience tiled wants."""
    client_id = f"migration-test-{fedid}"
    if admin.get("/clients", params={"clientId": client_id}).json():
        return
    mapper = {"protocol": "openid-connect", "consentRequired": False}
    admin.post(
        "/clients",
        json={
            "clientId": client_id,
            "secret": "secret",
            "standardFlowEnabled": False,
            "serviceAccountsEnabled": True,
            "protocolMappers": [
                {
                    **mapper,
                    "name": "fedid",
                    "protocolMapper": "oidc-hardcoded-claim-mapper",
                    "config": {
                        "claim.name": "fedid",
                        "claim.value": fedid,
                        "jsonType.label": "String",
                        "access.token.claim": "true",
                    },
                },
                {
                    **mapper,
                    "name": "audience",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {
                        "included.custom.audience": "account",
                        "access.token.claim": "true",
                    },
                },
            ],
        },
    ).raise_for_status()


def tiled_client(fedid: str) -> httpx.Client:
    token = httpx.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": f"migration-test-{fedid}",
            "client_secret": "secret",
            "grant_type": "client_credentials",
        },
    )
    token.raise_for_status()
    return httpx.Client(
        base_url=TILED,
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        timeout=30,
    )


def wait_for_tiled() -> None:
    for _ in range(90):
        try:
            if httpx.get(f"{TILED}/").status_code < 500:
                return
        except httpx.TransportError:
            pass
        subprocess.run(["sleep", "2"])
    compose("logs", "tiled")
    raise TimeoutError("tiled didn't come up")


@pytest.fixture(scope="module")
def migrated():
    """Restore the dump or seed, migrate it, bring up tiled. Yields the nodes
    before, the plan, the nodes after and the output of each run."""
    compose("up", "-d", "--wait", "postgres", "keycloak")
    try:
        compose(
            *("exec", "-T", "postgres", "dropdb", "-U", "postgres"),
            *("--if-exists", "--force", "tiled"),
        )
        compose("exec", "-T", "postgres", "createdb", "-U", "postgres", "tiled")
        if DUMP:
            with open(DUMP, "rb") as f:
                # Production roles don't exist here, so drop ownership and grants.
                compose(
                    *("exec", "-T", "postgres", "pg_restore", "-U", "postgres"),
                    *("-d", "tiled", "--no-owner", "--no-acl", "--exit-on-error"),
                    stdin=f,
                )
        else:
            seed()
        # A dry run needs the schema upgraded already
        subprocess.run(
            [sys.executable, "-m", "tiled", "catalog", "upgrade-database", URI],
            check=True,
        )
        before = nodes()
        result = asyncio.run(_plan())
        assert result.placed, "the catalog has no runs to place"

        dry = migrate("--dry-run")
        assert nodes() == before, "the dry run changed something"
        first = migrate()
        after = nodes()
        second = migrate()
        assert nodes() == after, "the second run changed something"

        with httpx.Client(base_url=f"{KEYCLOAK}/admin/realms/master") as admin:
            token = httpx.post(
                f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
                data={
                    "client_id": "admin-cli",
                    "username": "admin",
                    "password": "admin",
                    "grant_type": "password",
                },
            ).json()["access_token"]
            admin.headers["Authorization"] = f"Bearer {token}"
            for fedid in ("admin", "alice"):
                make_client(admin, fedid)

        compose("up", "-d", "--build", "opa", "tiled")
        wait_for_tiled()
        yield before, result, after, (dry, first, second)
    finally:
        if not os.environ.get("KEEP_STACK"):
            compose("down", "-v")


def by_path(nodes: dict[int, dict]) -> dict[str, dict]:
    return {node["path"]: node for node in nodes.values()}


def subtree(nodes: dict[int, dict], path: str) -> list[dict]:
    return [n for n in nodes.values() if f"{n['path']}/".startswith(f"{path}/")]


def test_dry_run_says_what_the_run_does(migrated):
    *_, (dry, first, _) = migrated
    assert "Dry run: rolled back" in dry
    assert dry.replace("Dry run: rolled back, nothing changed.\n", "") in first


def test_second_run_changes_nothing(migrated):
    *_, (_, _, second) = migrated
    assert "0 runs to move" in second
    assert "Moved 0 runs, removed 0 tags and added 0, deleted 0" in second


def test_runs_placed_with_subtrees_and_tags(migrated):
    before, result, after, _ = migrated
    for run, place in result.placed:
        old_path = before[run.id]["path"]
        path = "/".join((*place, run.key))
        assert after[run.id]["path"] == path
        assert after[run.id]["metadata"] == before[run.id]["metadata"]
        old = subtree(before, old_path)
        new = subtree(after, path)
        # The same nodes, at the same places below the run
        assert {n["path"].removeprefix(path): n["metadata"] for n in new} == {
            n["path"].removeprefix(old_path): n["metadata"] for n in old
        }
        assert all(n["tags"] == [tag(*place)] for n in new), path


def test_runs_not_placed_untouched(migrated):
    before, result, after, _ = migrated
    for run, _ in result.skipped:
        for node in subtree(before, before[run.id]["path"]):
            assert node in after.values()


def test_containers_tagged_for_their_place(migrated):
    _, result, after, _ = migrated
    paths = by_path(after)
    for instrument, visit in result.visits():
        assert paths[instrument]["tags"] == [tag(instrument)]
        assert paths[f"{instrument}/{visit}"]["tags"] == [tag(instrument, visit)]
        for folder in FOLDERS:
            node = paths[f"{instrument}/{visit}/{folder}"]
            assert node["tags"] == [tag(instrument, visit, folder)]


def test_tree_tables_consistent(migrated):
    # nodes_closure holds exactly each node's chain of parents
    assert (
        psql(
            "WITH RECURSIVE chain(ancestor, descendant, depth) AS ("
            "  SELECT id, id, 0 FROM nodes"
            "  UNION ALL SELECT n.parent, c.descendant, c.depth + 1"
            "  FROM chain c JOIN nodes n ON n.id = c.ancestor"
            "  WHERE n.parent IS NOT NULL"
            ") SELECT count(*) FROM ("
            "  (SELECT * FROM chain EXCEPT SELECT * FROM nodes_closure) UNION ALL"
            "  (SELECT * FROM nodes_closure EXCEPT SELECT * FROM chain)) d"
        ).strip()
        == "0"
    )
    assert (
        psql(
            "SELECT count(*) FROM node_access_tags_association a "
            "JOIN nodes n ON n.id = a.node_id "
            "WHERE a.parent_id IS DISTINCT FROM n.parent"
        ).strip()
        == "0"
    )


@pytest.mark.skipif(bool(DUMP), reason="checks the seeded catalog")
def test_seeded_catalog(migrated):
    before, result, after, _ = migrated
    placed = {run.key: "/".join((*p, run.key)) for run, p in result.placed}
    # One of the two run-4s moves
    assert placed == SEED_PLACES
    paths = by_path(after)
    # Left where they were
    for path in ("no-session", "bad-session", "adsim/cm12345/cm12345-2/run-4"):
        assert path in paths
        assert by_path(before)[path] == paths[path]
    # Old containers deleted once empty
    assert "adsim/cm12345/cm12345-9" not in paths
    assert "adsim/cm12345/cm12345-2" in paths
    # Untagged nodes get the folder's tag too
    assert paths["adsim/cm12345-1/raw/run-1/untagged"]["tags"] == ["adsim,cm12345-1"]


def test_admin_reads_every_placed_run(migrated):
    _, result, _, _ = migrated
    with tiled_client("admin") as client:
        for run, place in result.placed:
            path = "/".join((*place, run.key))
            response = client.get(f"/metadata/{path}")
            assert response.status_code == 200, response.text


@pytest.mark.skipif(bool(DUMP), reason="the test OPA data only knows the seed's")
def test_user_sees_only_their_visit(migrated):
    # alice is on cm12345-1 only
    with tiled_client("alice") as client:
        top = client.get("/search/adsim")
        assert top.status_code == 200, top.text
        assert [n["id"] for n in top.json()["data"]] == ["cm12345-1"]
        for path in SEED_PLACES.values():
            response = client.get(f"/metadata/{path}")
            visible = path.startswith("adsim/cm12345-1/")
            assert response.status_code == (200 if visible else 404), path


@pytest.mark.skipif(bool(DUMP), reason="the test OPA data only knows the seed's")
@pytest.mark.parametrize("folder,status", [("processing", 200), ("raw", 403)])
def test_user_writes_only_in_processing(migrated, folder, status):
    with tiled_client("alice") as client:
        response = client.post(
            f"/metadata/adsim/cm12345-1/{folder}",
            json={
                "id": "analysis",
                "structure_family": "container",
                "metadata": {},
                "specs": [],
                "data_sources": [],
            },
        )
        assert response.status_code == status, response.text
