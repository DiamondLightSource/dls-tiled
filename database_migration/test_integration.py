"""Migrate a real tiled dump and read it back through dls-tiled, Keycloak and OPA.

Needs docker compose and a custom-format dump of the tiled database, e.g.
backup/tiled.dump from README step 2:

    PROD_DUMP=backup/tiled.dump uv run pytest database_migration/test_integration.py

Skipped without PROD_DUMP. The stack in integration/compose.yaml is torn down
afterwards; set KEEP_STACK=1 to leave it up for poking at.

What it checks, for every run the migration moves:
  - it is at <beamline>/<proposal>/<session>/<run> with its metadata unchanged
  - its whole subtree came with it and is tagged with its session tag
  - an admin can read it through tiled, a user on one session sees only that
    session's runs, and runs the migration skips stay at the root
Array data isn't read back: the dump holds the catalog, not the files.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from migrate_node_structure import INSTRUMENT_SESSION_RE, ROOT_ID, plan_moves

HERE = Path(__file__).parent
STACK = HERE / "integration"
COMPOSE = ["docker", "compose", "-f", str(STACK / "compose.yaml")]
URI = "postgresql://postgres:postgres@localhost:5433/tiled"
KEYCLOAK = "http://localhost:8081"
TILED = "http://localhost:8407/api/v1"
DUMP = os.environ.get("PROD_DUMP")

pytestmark = pytest.mark.skipif(not DUMP, reason="set PROD_DUMP to a tiled pg_dump")


def compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([*COMPOSE, *args], check=True, **kwargs)


def sql(query: str):
    """Run a query that returns one JSON value and decode it."""
    out = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "tiled",
        "-AtXc",
        query,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def snapshot() -> list[dict]:
    """Every root child with its start doc fields, metadata and subtree size."""
    return sql(
        "SELECT coalesce(json_agg(json_build_object("
        "'id', n.id, 'key', n.key, 'metadata', n.metadata, "
        "'beamline', n.metadata #>> '{start,instrument}', "
        "'session', n.metadata #>> '{start,instrument_session}', "
        "'subtree', (SELECT count(*) FROM nodes_closure c WHERE c.ancestor = n.id)"
        ")), '[]') FROM nodes n WHERE n.parent = 0"
    )


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


def opa_data(sessions: list[tuple[str, int, int]]) -> dict:
    """OPA data giving admin everything and alice the first session only."""
    ids = {s: i for i, s in enumerate(sessions, start=1)}
    beamlines: dict = {}
    proposals: dict = {}
    for (beamline, number, visit), i in ids.items():
        beamlines.setdefault(beamline, {"sessions": []})["sessions"].append(i)
        proposals.setdefault(str(number), {"sessions": {}})["sessions"][str(visit)] = i
    subject = {"permissions": [], "proposals": [], "sessions": []}
    return {
        "diamond": {
            "data": {
                "beamlines": beamlines,
                "proposals": proposals,
                "sessions": {
                    str(i): {
                        "beamline": beamline,
                        "proposal_number": number,
                        "visit_number": visit,
                    }
                    for (beamline, number, visit), i in ids.items()
                },
                "subjects": {
                    "admin": {**subject, "permissions": ["super_admin"]},
                    "alice": {**subject, "sessions": [1]},
                },
            }
        }
    }


def wait_for_tiled() -> None:
    for _ in range(60):
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
    """Restore the dump, migrate it, bring up tiled; yield before and plan."""
    compose("up", "-d", "--wait", "postgres", "keycloak")
    try:
        compose(
            "exec",
            "-T",
            "postgres",
            "dropdb",
            "-U",
            "postgres",
            "--if-exists",
            "--force",
            "tiled",
        )
        compose("exec", "-T", "postgres", "createdb", "-U", "postgres", "tiled")
        with open(DUMP, "rb") as f:
            # Production roles don't exist here, so drop ownership and grants.
            compose(
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "postgres",
                "-d",
                "tiled",
                "--no-owner",
                "--no-acl",
                stdin=f,
            )
        # Upgrade first so the snapshot can count subtrees with nodes_closure.
        subprocess.run(
            [sys.executable, "-m", "tiled", "catalog", "upgrade-database", URI],
            check=True,
        )
        before = snapshot()
        moves, skipped = plan_moves(
            [(r["id"], r["key"], r["beamline"], r["session"]) for r in before]
        )
        assert moves, "the dump has no runs to move"

        subprocess.run(
            [sys.executable, str(HERE / "migrate_node_structure.py"), URI],
            check=True,
        )

        sessions = sorted(
            {
                (path[0][0], int(m["proposal_number"]), int(m["visit"]))
                for _, path in moves
                if (m := INSTRUMENT_SESSION_RE.match(path[2][0]))
            }
        )
        data_dir = STACK / "opa" / "opa_data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "data.json").write_text(json.dumps(opa_data(sessions)))
        # Without a root the data bundle claims everything and OPA refuses to
        # activate the policy bundle alongside it.
        (data_dir / ".manifest").write_text(json.dumps({"roots": ["diamond/data"]}))

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
        yield {r["id"]: r for r in before}, moves, skipped, sessions
    finally:
        if not os.environ.get("KEEP_STACK"):
            compose("down", "-v")


def path_of(path: list) -> str:
    return "/".join(key for key, _ in path)


def test_runs_moved_with_subtrees_and_tags(migrated):
    before, moves, _, _ = migrated
    after = {
        r["id"]: r
        for r in sql(
            "SELECT json_agg(json_build_object('id', n.id, 'parent', n.parent, "
            "'metadata', n.metadata, "
            "'subtree', (SELECT count(*) FROM nodes_closure c "
            "WHERE c.ancestor = n.id), "
            "'ancestors', (SELECT count(*) FROM nodes_closure c "
            "WHERE c.descendant = n.id), "
            "'tags', (SELECT json_agg(DISTINCT d.access_blob -> 'tags') "
            "FROM nodes_closure c JOIN nodes d ON d.id = c.descendant "
            "WHERE c.ancestor = n.id AND d.access_blob -> 'tags' IS NOT NULL))) "
            "FROM nodes n WHERE n.id = ANY(ARRAY["
            + ",".join(str(node_id) for node_id, _ in moves)
            + "])"
        )
    }
    for node_id, path in moves:
        run = after[node_id]
        assert run["parent"] != ROOT_ID
        assert run["metadata"] == before[node_id]["metadata"]
        assert run["subtree"] == before[node_id]["subtree"]
        # itself, session, proposal, beamline, root
        assert run["ancestors"] == 5
        assert run["tags"] in (None, [path[-1][1]["tags"]])


def test_admin_reads_every_moved_run(migrated):
    before, moves, _, _ = migrated
    with tiled_client("admin") as client:
        for node_id, path in moves:
            response = client.get(f"/metadata/{path_of(path)}/{before[node_id]['key']}")
            assert response.status_code == 200, response.text
            assert (
                response.json()["data"]["attributes"]["metadata"]
                == (before[node_id]["metadata"])
            )
        expected: dict[str, int] = {}
        for _, path in moves:
            expected[path_of(path)] = expected.get(path_of(path), 0) + 1
        for where, count in expected.items():
            response = client.get(f"/search/{where}", params={"page[limit]": 0})
            assert response.status_code == 200, response.text
            assert response.json()["meta"]["count"] == count, where


def test_skipped_runs_stay_at_root(migrated):
    before, _, skipped, _ = migrated
    root = set(sql("SELECT coalesce(json_agg(key), '[]') FROM nodes WHERE parent = 0"))
    moved_from_root = {r["key"] for r in before.values()} - {k for k, *_ in skipped}
    assert {k for k, *_ in skipped} <= root
    assert not moved_from_root & root


def test_user_sees_only_their_session(migrated):
    before, moves, _, sessions = migrated
    beamline, number, visit = sessions[0]
    mine = f"beamline:{beamline},proposal:{number},session:{number}-{visit}"
    with tiled_client("alice") as client:
        top = client.get("/search/", params={"page[limit]": 300})
        assert top.status_code == 200, top.text
        beamlines = {path[0][0] for _, path in moves}
        assert {n["id"] for n in top.json()["data"]} & beamlines == {beamline}
        for node_id, path in moves:
            response = client.get(f"/metadata/{path_of(path)}/{before[node_id]['key']}")
            if path[-1][1]["tags"] == [mine]:
                assert response.status_code == 200, response.text
            else:
                assert response.status_code == 404, path_of(path)
