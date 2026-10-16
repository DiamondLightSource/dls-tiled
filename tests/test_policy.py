import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import HttpUrl, SecretStr
from tiled.access_control import access_policies
from tiled.access_control.access_policies import ALL_ACCESS, NO_ACCESS
from tiled.config import parse_configs
from tiled.queries import AccessBlobFilter
from tiled.server.schemas import Principal, PrincipalType

from dls_tiled.policy import (
    DiamondOpenPolicyAgentAuthorizationPolicy,
    _check_principal,
)

OPA = "http://opa.example:8181/v1/data/"
AUDIENCE = "tiled-audience"
BLOB_TAG = '{"proposal": "cm1234", "visit": 5, "beamline": "i22"}'
BLOB_FIELDS = {"proposal": "cm1234", "visit": 5, "beamline": "i22"}

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
    body = json.loads(policy.build_input(user, None, set(), {"tags": [BLOB_TAG]}))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token", **BLOB_FIELDS}


@pytest.mark.parametrize(
    "tag, fields",
    [
        ('{"beamline": "i22"}', {"beamline": "i22"}),
        (
            '{"beamline": "i22", "proposal": "cm1234"}',
            {"beamline": "i22", "proposal": "cm1234"},
        ),
    ],
)
def test_build_input_with_container_blob(policy, user, tag, fields):
    # Beamline and proposal containers have no visit; absent fields aren't sent.
    body = json.loads(policy.build_input(user, None, set(), {"tags": [tag]}))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token", **fields}


def test_build_input_with_session_int(policy, user):
    body = json.loads(policy.build_input(user, None, set(), {"tags": ["98765"]}))

    assert body["input"] == {
        "audience": AUDIENCE,
        "token": "the-token",
        "session": "98765",
    }


def test_build_input_uses_first_tag_only(policy, user):
    blob = {"tags": ["98765", BLOB_TAG]}

    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"]["session"] == "98765"
    assert "proposal" not in body["input"]


@pytest.mark.parametrize("blob", [{}, {"tags": []}, {"user": "someone"}])
def test_build_input_ignores_blob_without_tags(policy, user, blob):
    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


@pytest.mark.parametrize(
    "tag, fields",
    [
        ("beamline:i22", {"beamline": "i22"}),
        ("beamline:i22,proposal:12345", {"beamline": "i22", "proposal": "12345"}),
        (
            "beamline:i22,proposal:12345,session:12345-1",
            {"beamline": "i22", "proposal": "12345", "session": "12345-1"},
        ),
        ("beamline:i22,unknown:x", {"beamline": "i22"}),
    ],
)
def test_build_input_with_composite_tag(policy, user, tag, fields):
    body = json.loads(policy.build_input(user, None, set(), {"tags": [tag]}))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token", **fields}


def test_build_input_ignores_unparseable_tag(policy, user):
    blob = {"tags": ['{"proposal": 1}']}

    body = json.loads(policy.build_input(user, None, set(), blob))

    assert body["input"] == {"audience": AUDIENCE, "token": "the-token"}


async def test_allowed_scopes_on_migrated_node(policy, user, opa):
    # tiled sends a node's own tag when reading it; this used to raise.
    opa.responses[f"{OPA}tiled/scopes"] = {"result": ["read:metadata"]}
    node = SimpleNamespace(
        access_blob={"tags": ["beamline:i22,proposal:12345,session:12345-1"]}
    )

    scopes = await policy.allowed_scopes(node, user, None, set())

    assert scopes == {"read:metadata"}
    assert opa.requests[0][1]["input"]["session"] == "12345-1"


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
        await policy.init_node(None, None, set(), {"tags": [BLOB_TAG]})  # type: ignore

    assert exc.value.status_code == 401
    assert opa.requests == []


# --- filters -------------------------------------------------------------------


async def test_filters_with_tags(policy, user, opa):
    opa.responses[OPA + "tiled/user_sessions"] = {"result": ["1", "2"]}

    filters = await policy.filters(None, user, None, set(), {"read:metadata"})

    assert filters == [AccessBlobFilter(tags=["1", "2"], user_id=None)]
    assert opa.requests == [
        (
            OPA + "tiled/user_sessions",
            {"input": {"audience": AUDIENCE, "token": "the-token"}},
        )
    ]


async def test_filters_wildcard_is_all_access(policy, user, opa):
    opa.responses[OPA + "tiled/user_sessions"] = {"result": ["*"]}

    assert await policy.filters(None, user, None, set(), set()) is ALL_ACCESS


async def test_filters_without_result_is_no_access(policy, user, opa):
    opa.responses[OPA + "tiled/user_sessions"] = {}

    assert await policy.filters(None, user, None, set(), set()) is NO_ACCESS


# --- init_node -----------------------------------------------------------------


async def test_init_node_empty_blob_public_skips_opa(policy, user, opa):
    assert await policy.init_node(user, None, set(), None) == (True, None)
    assert opa.requests == []


async def test_init_node_empty_blob_not_public(user, opa):
    policy = DiamondOpenPolicyAgentAuthorizationPolicy(
        authorization_provider=HttpUrl(OPA),
        token_audience=AUDIENCE,
        empty_access_blob_public=False,
    )

    assert await policy.init_node(user, None, set(), None) == (False, None)
    assert opa.requests == []


async def test_init_node_returns_tag_from_opa(policy, user, opa):
    opa.responses[OPA + "tiled/user_session"] = {"result": "12345"}

    result = await policy.init_node(user, None, set(), {"tags": [BLOB_TAG]})

    assert result == (True, {"tags": ["12345"]})
    assert opa.requests == [
        (
            OPA + "tiled/user_session",
            {"input": {"audience": AUDIENCE, "token": "the-token", **BLOB_FIELDS}},
        )
    ]


async def test_init_node_denied(policy, user, opa):
    opa.responses[OPA + "tiled/user_session"] = {}

    with pytest.raises(ValueError, match="Permission denied"):
        await policy.init_node(user, None, set(), {"tags": [BLOB_TAG]})


# --- modify_node ---------------------------------------------------------------


async def test_modify_node_identical_blob_skips_opa(policy, user, opa):
    node = SimpleNamespace(access_blob={"tags": ["1"]})

    result = await policy.modify_node(node, user, None, set(), {"tags": ["1"]})

    assert result == (False, {"tags": ["1"]})
    assert opa.requests == []


@pytest.mark.parametrize("allowed", [True, False])
async def test_modify_node_asks_opa(policy, user, opa, allowed):
    opa.responses[OPA + "tiled/modify_session"] = {"result": allowed}
    node = SimpleNamespace(access_blob={"tags": ["1"]})

    result = await policy.modify_node(node, user, None, set(), {"tags": ["2"]})

    assert result == (allowed, {"tags": ["2"]})
    assert opa.requests == [
        (
            OPA + "tiled/modify_session",
            {"input": {"audience": AUDIENCE, "token": "the-token", "session": "2"}},
        )
    ]


async def test_modify_node_denied(policy, user, opa):
    opa.responses[OPA + "tiled/modify_session"] = {"result": "not-a-bool"}
    node = SimpleNamespace(access_blob={"tags": ["1"]})

    with pytest.raises(ValueError, match="Permission denied"):
        await policy.modify_node(node, user, None, set(), {"tags": ["2"]})


# --- allowed_scopes (inherited from ExternalPolicyDecisionPoint) ---------------


async def test_allowed_scopes_sends_node_blob(policy, user, opa):
    opa.responses[OPA + "tiled/scopes"] = {"result": ["read:metadata", "read:data"]}
    node = SimpleNamespace(access_blob={"tags": [BLOB_TAG]})

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
    empty_access_blob_public: false
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
    assert policy._empty_access_blob_public is False
    assert policy._provider == "diamond"
