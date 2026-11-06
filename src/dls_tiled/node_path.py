"""Tell the access policy where in the tree a request creates or changes a node.

Envoy routes on the tiled path (the instrument is the first segment), so a
node's access tag has to name the instrument and visit its path is under.
tiled does not pass the path, or the new node's key, to the access policy's
init_node and modify_node, so this middleware records them in
``request_node_path`` for the policy to read. ``dls-tiled serve`` installs it.
"""

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from contextvars import ContextVar
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The path segments of the node the current request creates or changes. The
# last segment is None when a request creates a node without saying its key
# (tiled then makes one up).
request_node_path: ContextVar[tuple[str | None, ...] | None] = ContextVar(
    "request_node_path", default=None
)

_ROUTE_RE = re.compile(r"/api/v1/(?P<route>metadata|register)/(?P<path>.*)")

# (method, route) -> whether the request creates a child of the path
_NODE_REQUESTS = {
    ("POST", "metadata"): True,
    ("POST", "register"): True,
    ("PATCH", "metadata"): False,
    ("PUT", "metadata"): False,
}


def _body_key(body: bytes) -> str | None:
    """The key a create request asks for, if any."""
    try:
        key = json.loads(body).get("id")
    except (ValueError, AttributeError):
        return None
    return key if isinstance(key, str) and key else None


async def _read_body(receive: Receive) -> tuple[bytes, Receive]:
    """The request body, and a receive that replays it to the app."""
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":  # the client went away
            first = message
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            first = {"type": "http.request", "body": b"".join(chunks)}
            break
    replayed = False

    async def replay() -> Message:
        nonlocal replayed
        if replayed:
            return await receive()
        replayed = True
        return first

    return b"".join(chunks), replay


def _segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


class NodePathMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        match = _ROUTE_RE.fullmatch(path)
        creates = (
            _NODE_REQUESTS.get((scope["method"], match["route"])) if match else None
        )
        if match is None or creates is None:
            return await self.app(scope, receive, send)

        node_path: tuple[str | None, ...] = _segments(match["path"])
        if creates:
            body, receive = await _read_body(receive)
            node_path = (*node_path, _body_key(body))
        token = request_node_path.set(node_path)
        try:
            await self.app(scope, receive, send)
        finally:
            request_node_path.reset(token)


def install(app):
    """Add the middleware to a tiled app, outside tiled's own middleware."""
    app.add_middleware(NodePathMiddleware)
    return app


def serve(args: Sequence[str]) -> None:
    """``tiled serve <args>`` with the middleware installed in the app."""
    import tiled.server.app as tiled_app
    from tiled.commandline.main import cli_app

    build = tiled_app.build_app_from_config

    def build_app_from_config(*a, **kw):
        return install(build(*a, **kw))

    # tiled serve config imports this from tiled.server.app when it runs
    tiled_app.build_app_from_config = build_app_from_config
    cli_app(["serve", *args], prog_name="dls-tiled")
