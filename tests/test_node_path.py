import json
import subprocess
import sys

import pytest
from tiled.access_control.access_policies import ALL_ACCESS
from tiled.access_control.protocols import normalize_access_tags
from tiled.client import Context, from_context
from tiled.server.app import build_app_from_config

from dls_tiled import node_path
from dls_tiled.node_path import (
    NodePathMiddleware,
    install,
    request_node_path,
    request_seen,
)

pytestmark = pytest.mark.anyio


async def call(app, method: str, path: str, body: bytes = b"", chunks: int = 1):
    """Send one request through an ASGI app, the body split into chunks."""
    size = max(1, -(-len(body) // chunks))
    parts = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    messages = [
        {"type": "http.request", "body": part, "more_body": i < len(parts) - 1}
        for i, part in enumerate(parts)
    ]
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        pass

    await app(scope, receive, send)


class Recorder:
    """An app recording the node path it sees and the body it receives."""

    def __init__(self):
        self.path = "unset"
        self.body = b""

    async def __call__(self, scope, receive, send):
        self.path = request_node_path.get()
        while True:
            message = await receive()
            self.body += message.get("body", b"")
            if not message.get("more_body"):
                break


@pytest.mark.parametrize(
    "method,url,body,path",
    [
        ("POST", "/api/v1/metadata/", {"id": "i22"}, ("i22",)),
        ("POST", "/api/v1/metadata/i22/cm1-1", {"id": "raw"}, ("i22", "cm1-1", "raw")),
        ("POST", "/api/v1/register/i22", {"id": "cm1-1"}, ("i22", "cm1-1")),
        # tiled makes up a key when the request has none
        ("POST", "/api/v1/metadata/i22", {"metadata": {}}, ("i22", None)),
        ("POST", "/api/v1/metadata/i22", {"id": ""}, ("i22", None)),
        ("PATCH", "/api/v1/metadata/i22/cm1-1", {"id": "x"}, ("i22", "cm1-1")),
        ("PUT", "/api/v1/metadata/i22/cm1-1/", {}, ("i22", "cm1-1")),
        # Not a request that creates or changes a node
        ("GET", "/api/v1/metadata/i22", {}, None),
        ("POST", "/api/v1/search/i22", {"id": "x"}, None),
        ("PUT", "/api/v1/array/full/i22", {}, None),
    ],
)
async def test_middleware_records_node_path(method, url, body, path):
    app = Recorder()
    raw = json.dumps(body).encode()

    await call(NodePathMiddleware(app), method, url, raw)

    assert app.path == path
    assert app.body == raw  # the body still reaches the app


async def test_middleware_reads_chunked_body():
    app = Recorder()
    raw = json.dumps({"metadata": {"x": "y" * 100}, "id": "cm1-1"}).encode()

    await call(NodePathMiddleware(app), "POST", "/api/v1/metadata/i22", raw, 7)

    assert app.path == ("i22", "cm1-1")
    assert app.body == raw


async def test_middleware_ignores_body_that_is_not_json():
    app = Recorder()

    await call(NodePathMiddleware(app), "POST", "/api/v1/metadata/i22", b"\x81\xa2")

    assert app.path == ("i22", None)


async def test_middleware_leaves_path_unset_after_request():
    await call(NodePathMiddleware(Recorder()), "POST", "/api/v1/metadata/", b"{}")

    assert request_node_path.get() is None


class RecordingPolicy:
    """Lets anyone do anything, recording the node path at init_node."""

    paths: list = []

    async def init_node(self, principal, authn_access_tags, authn_scopes, access_tags):
        self.paths.append(request_node_path.get())
        return True, normalize_access_tags(access_tags or [])

    async def allowed_scopes(self, node, principal, authn_access_tags, authn_scopes):
        return {"read:metadata", "read:data", "write:metadata", "create:node"}

    async def filters(self, node, principal, authn_access_tags, authn_scopes, scopes):
        return ALL_ACCESS


def test_path_reaches_policy_through_tiled(tmp_path):
    """The path set by the middleware is still there when tiled, beneath its
    own middleware, asks the access policy about a new node."""
    RecordingPolicy.paths = []
    config = {
        "authentication": {"single_user_api_key": "secret"},
        "access_control": {
            "access_policy": f"{__name__}:RecordingPolicy",
            "args": {},
        },
        "trees": [
            {
                "path": "/",
                "tree": "tiled.catalog:in_memory",
                "args": {"writable_storage": str(tmp_path / "data")},
            }
        ],
    }
    app = install(build_app_from_config(config))
    with Context.from_app(app, api_key="secret") as context:  # type: ignore[arg-type]
        client = from_context(context)
        client.create_container("i22").create_container("cm1-1")

    assert RecordingPolicy.paths == [("i22",), ("i22", "cm1-1")]


def test_serve_installs_middleware(monkeypatch):
    import tiled.commandline.main
    import tiled.server.app as tiled_app

    built = []
    original = tiled_app.build_app_from_config

    def fake_cli(args, prog_name):
        # What tiled serve config does with the patched builder
        tree = {"path": "/", "tree": "tiled.examples.generated_minimal:tree"}
        config = {"trees": [tree]}
        built.append(tiled_app.build_app_from_config(config))

    monkeypatch.setattr(tiled.commandline.main, "cli_app", fake_cli)
    monkeypatch.setattr(tiled_app, "build_app_from_config", original)

    node_path.serve(["config", "config.yml"])

    [app] = built
    assert any(m.cls is NodePathMiddleware for m in app.user_middleware)


@pytest.mark.parametrize("args", [["config", "--help"], ["--help"]])
def test_cli_serve_passes_options_to_tiled(args):
    cmd = [sys.executable, "-m", "dls_tiled", "serve", *args]
    out = subprocess.check_output(cmd).decode()

    assert "Usage: dls-tiled serve" in out


def test_app_factory_installs_middleware(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(
        "trees:\n  - path: /\n    tree: tiled.examples.generated_minimal:tree\n"
    )
    monkeypatch.setenv("TILED_CONFIG", str(config))

    app = node_path.app_factory()

    assert any(m.cls is NodePathMiddleware for m in app.user_middleware)


async def test_middleware_marks_every_request_seen():
    seen = []

    async def app(scope, receive, send):
        seen.append(request_seen.get())

    await call(NodePathMiddleware(app), "POST", "/api/v1/graphql", b"{}")

    assert seen == [True]
    assert request_seen.get() is False
