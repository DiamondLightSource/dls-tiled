"""Diamond Light Source tiled access policy, backed by Open Policy Agent.

Ported from Zoheb Shaikh's diamond-tiled
(https://github.com/DiamondLightSource/diamond-tiled).
"""

import base64
import json
import logging
import time

from fastapi import HTTPException
from pydantic import BaseModel, HttpUrl, TypeAdapter, ValidationError
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
)
from tiled.access_control.access_policies import (
    ALL_ACCESS,
    NO_ACCESS,
    ExternalPolicyDecisionPoint,
    ResultHolder,
)
from tiled.adapters.protocols import BaseAdapter
from tiled.queries import AccessBlobFilter
from tiled.server.schemas import Principal, PrincipalType
from tiled.type_aliases import AccessBlob, AccessTags, Filters, Scopes

logger = logging.getLogger(__name__)

# How long a caller's user_sessions answer is reused before OPA is asked again,
# so a change in authz takes up to this long to reach tiled. Service accounts
# (tokens without a fedid, as tiled.rego tells them apart) change rarely.
USER_TAGS_TTL = 60  # seconds
SERVICE_ACCOUNT_TAGS_TTL = 450  # seconds, 7.5 minutes


class DiamondAccessBlob(BaseModel):
    """What blueapi asks for when creating a node at each level of the tree.

    beamline alone for a beamline node, plus proposal for a proposal node,
    plus visit for a session node.
    """

    beamline: str
    # The full proposal code, e.g. "cm12345" - tiled.rego strips the leading
    # letters itself where it needs the bare number.
    proposal: str | None = None
    visit: int | None = None


# Maps a composite access tag's "key" (as produced by tiled.rego's
# beamline_tag/proposal_tag/session_tag, e.g.
# "beamline:i22,proposal:12345,session:12345-1") to the corresponding OPA
# input field. All of these are strings on an existing node's tag - "session"
# here is the "12345-1" proposal-visit pair, not the internal numeric session
# id used by modify_session.
_TAG_KEY_TO_INPUT_FIELD = {
    "beamline": "beamline",
    "proposal": "proposal",
    "session": "session",
}


def _parse_composite_tag(tag: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in tag.split(","):
        key, _, value = part.partition(":")
        field = _TAG_KEY_TO_INPUT_FIELD.get(key)
        if field is not None:
            fields[field] = value
    return fields


def _tags_ttl(token: str) -> int:
    """How long to cache this token's tags. Reads the JWT payload unverified,
    since tiled has already verified the token; one that can't be read gets the
    shorter user TTL rather than the service-account one."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        is_user = "fedid" in claims
    except (IndexError, ValueError, TypeError):
        is_user = True
    return USER_TAGS_TTL if is_user else SERVICE_ACCOUNT_TAGS_TTL


def _check_principal(principal: Principal | None):
    if not isinstance(principal, Principal):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Principal is None",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if principal.type != PrincipalType.user:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail=f"Principal of type {PrincipalType.user}"
            f" required but given {principal.type}",
            headers={"WWW-Authenticate": "Bearer"},
        )


class DiamondOpenPolicyAgentAuthorizationPolicy(ExternalPolicyDecisionPoint):
    def __init__(
        self,
        authorization_provider: HttpUrl,
        token_audience: str,
        create_node_endpoint: str = "tiled/user_session",
        allowed_tags_endpoint: str = "tiled/user_sessions",
        scopes_endpoint: str = "tiled/scopes",
        modify_node_endpoint: str = "tiled/modify_session",
        empty_access_blob_public: bool = True,
        provider: str | None = None,
    ):
        self._token_audience = token_audience
        self._type_adapter = TypeAdapter(DiamondAccessBlob | int)
        # access token -> (monotonic expiry, filters)
        self._filters_cache: dict[str, tuple[float, Filters]] = {}

        super().__init__(
            authorization_provider=authorization_provider,
            create_node_endpoint=create_node_endpoint,
            allowed_tags_endpoint=allowed_tags_endpoint,
            scopes_endpoint=scopes_endpoint,
            provider=provider,
            modify_node_endpoint=modify_node_endpoint,
            empty_access_blob_public=empty_access_blob_public,
        )

    async def init_node(
        self,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        access_blob: AccessBlob | None = None,
    ) -> tuple[bool, AccessBlob | None]:
        _check_principal(principal)
        if access_blob is None and self._empty_access_blob_public is not None:
            return self._empty_access_blob_public, access_blob
        decision = await self._get_external_decision(
            self._create_node,
            self.build_input(principal, authn_access_tags, authn_scopes, access_blob),
            ResultHolder[list[str]],
        )
        if decision and decision.result is not None:
            # The node's own tag first (build_input reads tags[0] back), then
            # its ancestors' grant tags; see Node Tags in tiled.rego.
            return (True, {"tags": decision.result})
        raise ValueError("Permission denied not able to add the node")

    async def modify_node(
        self,
        node: BaseAdapter,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        access_blob: AccessBlob | None,
    ) -> tuple[bool, AccessBlob | None]:
        _check_principal(principal)
        if access_blob == node.access_blob:  # type: ignore
            logger.info(
                "Node access_blob not modified;"
                f" access_blob is identical: {access_blob}"
            )
            return (False, node.access_blob)  # type: ignore
        decision = await self._get_external_decision(
            self._modify_node,
            self.build_input(principal, authn_access_tags, authn_scopes, access_blob),
            ResultHolder[bool],
        )
        if decision:
            return (decision.result, access_blob)
        raise ValueError("Permission denied not able to add the node")

    def build_input(
        self,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        access_blob: AccessBlob | None = None,
    ) -> str:
        _input: dict[str, str | int] = {"audience": self._token_audience}

        if (
            isinstance(principal, Principal)
            and principal.type is PrincipalType.user
            and principal.access_token is not None
        ):
            _input["token"] = principal.access_token.get_secret_value()

        if (
            access_blob is not None
            and "tags" in access_blob
            and len(access_blob["tags"]) > 0
        ):
            tag = access_blob["tags"][0]
            try:
                blob = self._type_adapter.validate_json(tag)
            except ValidationError:
                # Not a create-request JSON blob - it's a composite tag
                # already assigned to an existing node (e.g. tiled checking
                # scopes on a node before reading it).
                blob = None
                _input.update(_parse_composite_tag(tag))
            if isinstance(blob, DiamondAccessBlob):
                _input.update(blob.model_dump(exclude_none=True))
            elif isinstance(blob, int):
                _input["session"] = str(blob)

        return json.dumps({"input": _input})

    async def filters(
        self,
        node: BaseAdapter,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        scopes: Scopes,
    ) -> Filters:
        _check_principal(principal)
        token = (
            principal.access_token.get_secret_value()
            if principal.access_token
            else None
        )
        now = time.monotonic()
        if token and (cached := self._filters_cache.get(token)) and cached[0] > now:
            return cached[1]
        tags = await self._get_external_decision(
            self._user_tags,
            self.build_input(principal, authn_access_tags, authn_scopes),
            ResultHolder[list[str]],
        )
        if tags is None:
            # Not cached, so OPA recovering takes effect at once.
            return NO_ACCESS  # type: ignore
        if tags.result == ["*"]:
            filters = ALL_ACCESS
        else:
            filters = [AccessBlobFilter(tags=tags.result, user_id=None)]  # type: ignore
        if token:
            # ponytail: drops expired entries on every miss, O(cached tokens); fine
            # for hundreds of callers, use a TTL cache (e.g. cachetools) if that grows.
            self._filters_cache = {
                k: v for k, v in self._filters_cache.items() if v[0] > now
            }
            self._filters_cache[token] = (now + _tags_ttl(token), filters)
        return filters
