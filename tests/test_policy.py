import base64
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import HTTPException
from pydantic import HttpUrl, SecretStr
from tiled.access_control import access_policies
from tiled.access_control.access_policies import ALL_ACCESS, NO_ACCESS
from tiled.config import parse_configs
from tiled.queries import AccessTagsFilter
from tiled.server.schemas import Principal, PrincipalType

from dls_tiled import policy as policy_module
from dls_tiled.policy import (
    SERVICE_ACCOUNT_TAGS_TTL,
    USER_TAGS_TTL,
    DiamondOpenPolicyAgentAuthorizationPolicy,
    _check_principal,
)

OPA = "http://opa.example:8181/v1/data/"
AUDIENCE = "tiled-audience"
BLOB_TAG = '{"instrument": "i22", "instrument_session": "cm1234-5"}'
BLOB_FIELDS = {"instrument": "i22", "instrument_session": "cm1234-5"}

# async tests run on anyio's pytest plugin, which ships with tiled's httpx
pytestmark = pytest.mark.anyio


def make_principal(
    type: PrincipalType = PrincipalType.user, token: str | None = "the-token"
) -> Principal:
    return Principal(
        uuid=uuid.uuid4(),
        type=type,
        access_token=SecretStr(token) if token is not None else None,
    )


@pytest.fixture
def policy() -> DiamondOpenPolicyAgentAuthorizationPolicy:
    return DiamondOpenPolicyAgentAuthorizationPolicy(
        authorization_provider=HttpUrl(OPA), token_audience=AUDIENCE
    )


@pytest.fixture
def user() -> Principal:
    return make_principal()


class FakeOPA:
    """Records POSTs to the policy decision point and answers from a URL map."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, json.loads(request.content)))
        return httpx.Response(200, json=self.responses[url])


@pytest.fixture
def opa(monkeypatch) -> FakeOPA:
    fake = FakeOPA({})
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(access_policies.httpx, "AsyncClient", client)
    return fake


# --- build_input: the OPA input document ---------------------------------------


def test_build_input_without_access_blob(policy, user):
    body = json.loads(policy.build_input(user, None, set()))

    assert body == {"input": {"audience": AUDIENCE, "token": "the-token"}}


def test_build_input_with_diamond_access_blob(policy, user):
    body = json.loads(policy.build_input(user, None, set(), frozenset([BLOB_TAG])))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token", **BLOB_FIELDS}


def test_build_input_with_instrument_blob(policy, user):
    body = json.loads(
        policy.build_input(user, None, set(), frozenset(['{"instrument": "i22"}']))
    )

    assert body["input"] == {
        "audience": AUDIENCE,
        "token": "the-token",
        "instrument": "i22",
    }


@pytest.mark.parametrize("folder", ["raw", "processed", "processing"])
def test_build_input_with_folder_blob(policy, user, folder):
    tag = json.dumps({**BLOB_FIELDS, "folder": folder})

    body = json.loads(policy.build_input(user, None, set(), frozenset([tag])))

    assert body["input"] == {
        "audience": AUDIENCE,
        "token": "the-token",
        **BLOB_FIELDS,
        "folder": folder,
    }


@pytest.mark.parametrize(
    "tag",
    [
        '{"instrument": "i22", "folder": "raw"}',  # folder without a session
        '{"instrument": "i22", "instrument_session": "cm1-1", "folder": "other"}',
        '{"instrument": "i22", "instrument_session": "cm1-1", "visit": 1}',
        '{"proposal": "cm1234", "visit": 5, "beamline": "i22"}',  # old blob
        "98765",  # old session id tag
    ],
)
def test_build_input_ignores_invalid_blob(policy, user, tag):
    body = json.loads(policy.build_input(user, None, set(), frozenset([tag])))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


def test_build_input_ignores_other_tags(policy, user):
    blob = frozenset(["i22,cm1-1,t", "user:alice"])

    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"]["instrument_session"] == "cm1-1"
    assert body["input"]["folder"] == "processing"


def test_build_input_sends_no_node_fields_for_two_of_our_tags(policy, user):
    # tiled's tags are unordered, so there is no telling which one to use
    blob = frozenset(["i22,cm1-1,t", BLOB_TAG])

    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


@pytest.mark.parametrize("blob", [None, frozenset()])
def test_build_input_ignores_blob_without_tags(policy, user, blob):
    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


@pytest.mark.parametrize(
    "tag, fields",
    [
        ("i22", {"instrument": "i22"}),
        ("i22,cm12345-1", {"instrument": "i22", "instrument_session": "cm12345-1"}),
        (
            "i22,cm12345-1,p",
            {
                "instrument": "i22",
                "instrument_session": "cm12345-1",
                "folder": "processed",
            },
        ),
        (
            "b07-1,cm12345-1,t",
            {
                "instrument": "b07-1",
                "instrument_session": "cm12345-1",
                "folder": "processing",
            },
        ),
    ],
)
def test_build_input_with_node_tag(policy, user, tag, fields):
    body = json.loads(policy.build_input(user, None, set(), frozenset([tag])))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token", **fields}


@pytest.mark.parametrize(
    "tag",
    [
        "i22,cm12345-1,x",
        "i22,cm12345-1,p,t",
        "i22,12345-1",
        ",cm12345-1",
        "beamline:i22,proposal:12345,session:12345-1",  # old tag
        '{"instrument": "i22", "x": 1}',
    ],
)
def test_build_input_ignores_unknown_tag(policy, user, tag):
    body = json.loads(policy.build_input(user, None, set(), frozenset([tag])))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


def test_build_input_ignores_unparseable_tag(policy, user):
    blob = frozenset(['{"proposal": 1}'])

    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


async def test_allowed_scopes_on_existing_node(policy, user, opa):
    # tiled sends a node's own tag when checking scopes on it
    opa.responses[f"{OPA}tiled/scopes"] = {"result": ["read:metadata"]}
    node = SimpleNamespace(access_tags=frozenset(["i22,cm12345-1,t"]))

    scopes = await policy.allowed_scopes(node, user, None, set())

    assert scopes == {"read:metadata"}
    assert opa.requests[0][1]["input"] == {
        "audience": AUDIENCE,
        "token": "the-token",
        "instrument": "i22",
        "instrument_session": "cm12345-1",
        "folder": "processing",
    }


def test_build_input_omits_token_when_absent(policy):
    body = json.loads(policy.build_input(make_principal(token=None), None, set()))

    assert body["input"] == {"audience": AUDIENCE}


def test_build_input_omits_token_for_service_principal(policy):
    service = make_principal(type=PrincipalType.service)

    body = json.loads(policy.build_input(service, None, set()))

    assert body["input"] == {"audience": AUDIENCE}


# --- principal checks ----------------------------------------------------------


def test_check_principal_none_is_401():
    with pytest.raises(HTTPException) as exc:
        _check_principal(None)

    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def test_check_principal_service_is_401():
    with pytest.raises(HTTPException) as exc:
        _check_principal(make_principal(type=PrincipalType.service))

    assert exc.value.status_code == 401
    assert "service" in exc.value.detail


def test_check_principal_user_passes(user):
    _check_principal(user)


async def test_filters_rejects_service_principal(policy, opa):
    service = make_principal(type=PrincipalType.service)

    with pytest.raises(HTTPException) as exc:
        await policy.filters(None, service, None, set(), {"read:metadata"})

    assert exc.value.status_code == 401
    assert opa.requests == []


async def test_init_node_rejects_missing_principal(policy, opa):
    with pytest.raises(HTTPException) as exc:
        await policy.init_node(None, None, set(), frozenset([BLOB_TAG]))  # type: ignore

    assert exc.value.status_code == 401
    assert opa.requests == []


# --- filters -------------------------------------------------------------------


async def test_filters_with_tags(policy, user, opa):
    opa.responses[OPA + "tiled/user_tags"] = {"result": ["1", "2"]}

    filters = await policy.filters(None, user, None, set(), {"read:metadata"})

    assert filters == [AccessTagsFilter(frozenset(["1", "2"]))]  # pyright: ignore[reportArgumentType]
    assert opa.requests == [
        (
            OPA + "tiled/user_tags",
            {"input": {"audience": AUDIENCE, "token": "the-token"}},
        )
    ]


async def test_filters_wildcard_is_all_access(policy, user, opa):
    opa.responses[OPA + "tiled/user_tags"] = {"result": ["*"]}

    assert await policy.filters(None, user, None, set(), set()) is ALL_ACCESS


async def test_filters_without_result_is_no_access(policy, user, opa):
    opa.responses[OPA + "tiled/user_tags"] = {}

    assert await policy.filters(None, user, None, set(), set()) is NO_ACCESS


def jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(policy_module.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.parametrize(
    "token, ttl",
    [
        (jwt({"fedid": "abc12345"}), USER_TAGS_TTL),
        (jwt({"fedid": "abc12345", "instrument": "i22"}), USER_TAGS_TTL),
        (jwt({"instrument": "i22"}), SERVICE_ACCOUNT_TAGS_TTL),
        ("not-a-jwt", USER_TAGS_TTL),  # unreadable: the shorter TTL
    ],
)
async def test_filters_cached_until_ttl(policy, opa, clock, token, ttl):
    opa.responses[OPA + "tiled/user_tags"] = {"result": ["i22,cm1-1"]}
    user = make_principal(token=token)

    first = await policy.filters(None, user, None, set(), set())
    clock[0] += ttl - 1
    assert await policy.filters(None, user, None, set(), set()) is first
    assert len(opa.requests) == 1
    clock[0] += 2
    await policy.filters(None, user, None, set(), set())
    assert len(opa.requests) == 2


async def test_filters_cache_is_per_token(policy, opa, clock):
    opa.responses[OPA + "tiled/user_tags"] = {"result": ["i22,cm1-1"]}

    for token in ("token-a", "token-b"):
        await policy.filters(None, make_principal(token=token), None, set(), set())

    assert len(opa.requests) == 2


async def test_filters_no_result_not_cached(policy, user, opa, clock):
    opa.responses[OPA + "tiled/user_tags"] = {}
    assert await policy.filters(None, user, None, set(), set()) is NO_ACCESS

    opa.responses[OPA + "tiled/user_tags"] = {"result": ["*"]}
    assert await policy.filters(None, user, None, set(), set()) is ALL_ACCESS


# --- init_node -----------------------------------------------------------------


async def test_init_node_empty_blob_public_skips_opa(policy, user, opa):
    assert await policy.init_node(user, None, set(), None) == (True, frozenset())
    assert opa.requests == []


async def test_init_node_empty_blob_not_public(user, opa):
    policy = DiamondOpenPolicyAgentAuthorizationPolicy(
        authorization_provider=HttpUrl(OPA),
        token_audience=AUDIENCE,
        empty_access_tags_public=False,
    )

    assert await policy.init_node(user, None, set(), None) == (False, frozenset())
    assert opa.requests == []


async def test_init_node_returns_tags_from_opa(policy, user, opa):
    tags = ["i22,cm1234-5"]
    opa.responses[OPA + "tiled/node_tags"] = {"result": tags}

    result = await policy.init_node(user, None, set(), frozenset([BLOB_TAG]))

    assert result == (True, frozenset(tags))
    assert opa.requests == [
        (
            OPA + "tiled/node_tags",
            {"input": {"audience": AUDIENCE, "token": "the-token", **BLOB_FIELDS}},
        )
    ]


async def test_init_node_denied(policy, user, opa):
    opa.responses[OPA + "tiled/node_tags"] = {}

    with pytest.raises(ValueError, match="Permission denied"):
        await policy.init_node(user, None, set(), frozenset([BLOB_TAG]))


# --- modify_node ---------------------------------------------------------------


async def test_modify_node_identical_blob_skips_opa(policy, user, opa):
    node = SimpleNamespace(access_tags=frozenset(["1"]))

    result = await policy.modify_node(node, user, None, set(), frozenset(["1"]))

    assert result == (False, frozenset(["1"]))
    assert opa.requests == []


async def test_modify_node_stores_tags_from_opa(policy, user, opa):
    # The caller sends a create blob; the node gets OPA's tag, not the blob
    opa.responses[OPA + "tiled/node_tags"] = {"result": ["i22,cm1-1,t"]}
    node = SimpleNamespace(access_tags=frozenset(["i22,cm1-1"]))
    new_blob = frozenset([json.dumps({**BLOB_FIELDS, "folder": "processing"})])

    result = await policy.modify_node(node, user, None, set(), new_blob)

    assert result == (True, frozenset(["i22,cm1-1,t"]))
    assert opa.requests == [
        (
            OPA + "tiled/node_tags",
            {
                "input": {
                    "audience": AUDIENCE,
                    "token": "the-token",
                    **BLOB_FIELDS,
                    "folder": "processing",
                }
            },
        )
    ]


async def test_modify_node_to_same_tags_not_modified(policy, user, opa):
    opa.responses[OPA + "tiled/node_tags"] = {"result": ["i22,cm1-1"]}
    node = SimpleNamespace(access_tags=frozenset(["i22,cm1-1"]))

    result = await policy.modify_node(
        node, user, None, set(), frozenset([BLOB_TAG.replace("cm1234-5", "cm1-1")])
    )

    assert result == (False, frozenset(["i22,cm1-1"]))


@pytest.mark.parametrize(
    "response",
    [
        {},  # OPA said nothing: the rule is undefined for this caller
        {"result": []},
        {"result": False},  # e.g. pointed at a boolean rule
    ],
)
async def test_modify_node_denied_raises(policy, user, opa, response):
    # Returning (False, new tags) would still have tiled store the new tags
    opa.responses[OPA + "tiled/node_tags"] = response
    node = SimpleNamespace(access_tags=frozenset(["i22,cm1-1"]))

    with pytest.raises(ValueError, match="Permission denied"):
        await policy.modify_node(node, user, None, set(), frozenset(["i22,cm9-9,t"]))


# --- allowed_scopes (inherited from ExternalPolicyDecisionPoint) ---------------


async def test_allowed_scopes_sends_node_blob(policy, user, opa):
    opa.responses[OPA + "tiled/scopes"] = {"result": ["read:metadata", "read:data"]}
    node = SimpleNamespace(access_tags=frozenset([BLOB_TAG]))

    scopes = await policy.allowed_scopes(node, user, None, set())

    assert scopes == {"read:metadata", "read:data"}
    assert opa.requests == [
        (
            OPA + "tiled/scopes",
            {"input": {"audience": AUDIENCE, "token": "the-token", **BLOB_FIELDS}},
        )
    ]


async def test_allowed_scopes_without_result_is_empty(policy, user, opa):
    opa.responses[OPA + "tiled/scopes"] = {}

    assert await policy.allowed_scopes(None, user, None, set()) == set()


# --- loading through a tiled config --------------------------------------------


def test_loads_from_tiled_config(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        f"""
access_control:
  access_policy: dls_tiled.policy:DiamondOpenPolicyAgentAuthorizationPolicy
  args:
    authorization_provider: {OPA}
    token_audience: {AUDIENCE}
    create_node_endpoint: tiled/create
    allowed_tags_endpoint: tiled/tags
    scopes_endpoint: tiled/node_scopes
    modify_node_endpoint: tiled/modify
    empty_access_tags_public: false
    provider: diamond
trees:
  - path: /
    tree: tiled.examples.generated_minimal:tree
"""
    )

    config = parse_configs(config_file)

    policy = config.access_policy
    assert isinstance(policy, DiamondOpenPolicyAgentAuthorizationPolicy)
    assert policy._token_audience == AUDIENCE
    assert policy._create_node == OPA + "tiled/create"
    assert policy._user_tags == OPA + "tiled/tags"
    assert policy._node_scopes == OPA + "tiled/node_scopes"
    assert policy._modify_node == OPA + "tiled/modify"
    assert policy._empty_access_tags_public is False
    assert policy._provider == "diamond"
