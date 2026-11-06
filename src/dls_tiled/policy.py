"""Diamond Light Source tiled access policy, backed by Open Policy Agent.

Ported from Zoheb Shaikh's diamond-tiled
(https://github.com/DiamondLightSource/diamond-tiled).
"""

import base64
import json
import logging
import re
import time
from typing import Literal, Self

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    HttpUrl,
    ValidationError,
    model_validator,
)
from starlette.status import (
    HTTP_401_UNAUTHORIZED,
)
from tiled.access_control.access_policies import (
    ALL_ACCESS,
    NO_ACCESS,
    ExternalPolicyDecisionPoint,
    ResultHolder,
)
from tiled.access_control.protocols import normalize_access_tags
from tiled.adapters.protocols import BaseAdapter
from tiled.queries import AccessTagsFilter
from tiled.server.schemas import Principal, PrincipalType
from tiled.type_aliases import AccessTags, Filters, Scopes

from .node_path import request_node_path

logger = logging.getLogger(__name__)

# How long a caller's user_tags answer is reused before OPA is asked again,
# so a change in authz takes up to this long to reach tiled. Service accounts
# (tokens without a fedid, as tiled.rego tells them apart) change rarely.
USER_TAGS_TTL = 60  # seconds
SERVICE_ACCOUNT_TAGS_TTL = 450  # seconds, 7.5 minutes


Folder = Literal["raw", "processed", "processing"]


class DiamondAccessBlob(BaseModel):
    """What a client (e.g. blueapi) asks for when creating a node: the
    instrument node, or a folder of a visit (raw by default, which is also the
    visit node itself).
    """

    model_config = ConfigDict(extra="forbid")

    instrument: str
    # The full instrument session, e.g. "cm12345-1"
    instrument_session: str | None = None
    folder: Folder | None = None

    @model_validator(mode="after")
    def _folder_needs_session(self) -> Self:
        if self.folder and not self.instrument_session:
            raise ValueError("folder needs an instrument_session")
        return self


# The tags tiled.rego gives nodes: "<instrument>" for the instrument node,
# "<instrument>,<visit>" for raw (and the visit node), plus ",p" for processed
# and ",t" for processing, e.g. "i22,cm12345-1,t".
_TAG_SUFFIX_TO_FOLDER: dict[str, Folder] = {"p": "processed", "t": "processing"}


_TAG_RE = re.compile(
    r"^(?P<instrument>[A-Za-z][A-Za-z0-9-]*)"
    r"(?:,(?P<session>[A-Za-z]+[0-9]+-[0-9]+)(?:,(?P<suffix>[pt]))?)?$"
)


def _parse_tag(tag: str) -> dict[str, str]:
    """The OPA input fields for a tag already on a node, or nothing for a tag
    that isn't one of ours."""
    if not (match := _TAG_RE.match(tag)):
        return {}
    fields = {"instrument": match["instrument"]}
    if session := match["session"]:
        fields["instrument_session"] = session
    if suffix := match["suffix"]:
        fields["folder"] = _TAG_SUFFIX_TO_FOLDER[suffix]
    return fields


def _tag_fields(tag: str) -> dict[str, str]:
    try:
        # A JSON blob a client sends when creating a node
        blob = DiamondAccessBlob.model_validate_json(tag)
        return blob.model_dump(exclude_none=True)
    except ValidationError:
        # A tag OPA already gave an existing node, e.g. tiled checking scopes
        # on a node before writing to it
        return _parse_tag(tag)


def _node_fields(access_tags: AccessTags | tuple[()]) -> dict[str, str]:
    """The OPA input fields for a node's access tags. tiled keeps tags as an
    unordered set, so this needs exactly one of them to be ours (as tiled.rego
    gives each node a single tag); anything else sends no node fields and OPA
    denies writes to the node."""
    fields = [f for tag in access_tags if (f := _tag_fields(tag))]
    if len(fields) == 1:
        return fields[0]
    if fields:
        logger.warning("Node has more than one Diamond access tag: %s", access_tags)
    return {}


def _canonical(fields: dict[str, str]) -> dict[str, str]:
    """Node fields as a tag records them: raw is the default folder and the
    visit is lower case."""
    fields = dict(fields)
    if fields.get("folder") == "raw":
        del fields["folder"]
    if session := fields.get("instrument_session"):
        fields["instrument_session"] = session.lower()
    return fields


_FOLDER_TO_TAG_SUFFIX = {folder: s for s, folder in _TAG_SUFFIX_TO_FOLDER.items()}


def _tag_for(fields: dict[str, str]) -> str:
    """The tag tiled.rego gives a node with these canonical fields."""
    tag = fields["instrument"]
    if session := fields.get("instrument_session"):
        tag += f",{session}"
    if folder := fields.get("folder"):
        tag += f",{_FOLDER_TO_TAG_SUFFIX[folder]}"
    return tag


def _show(path: tuple[str | None, ...]) -> str:
    return "/" + "/".join(segment or "<new key>" for segment in path)


def _path_fields(path: tuple[str | None, ...]) -> dict[str, str]:
    """The node fields a node's place in the tree gives it:

        <instrument>                        {instrument}
        <instrument>/<visit>                {instrument, instrument_session}
        <instrument>/<visit>/<folder>/...   {..., folder} (raw left out)

    Envoy routes requests on the instrument in the path, so a node's tag has
    to name the instrument and visit it is under. Raises ValueError for any
    other place, e.g. the root, an unknown folder or an upper case visit.
    """
    head = path[:3]
    if not head or None in head:
        raise ValueError(f"No Diamond node can go at {_show(path)}")
    if len(head) == 3 and head[2] not in _FOLDER_TO_TAG_SUFFIX | {"raw": ""}:
        raise ValueError(
            f"No Diamond node can go at {_show(path)}: the folders of a"
            " visit are raw, processed and processing"
        )
    names = ("instrument", "instrument_session", "folder")
    fields = _canonical({name: str(s) for name, s in zip(names, head, strict=False)})
    # The path must read exactly as the tag will, e.g. a lower case visit
    if _parse_tag(_tag_for(fields)) != fields or (
        len(head) > 1 and fields["instrument_session"] != head[1]
    ):
        raise ValueError(f"No Diamond node can go at {_show(path)}")
    return fields


def _request_path_fields(access_tags: AccessTags | None) -> dict[str, str]:
    """The node fields of the node this request creates or changes, from its
    path, checking that any Diamond tag the caller sent agrees with them."""
    path = request_node_path.get()
    if path is None:
        raise ValueError("The node's path is unknown: serve with `dls-tiled serve`")
    fields = _path_fields(path)
    asked = [f for tag in access_tags or () if (f := _tag_fields(tag))]
    if any(_canonical(f) != fields for f in asked):
        raise ValueError(
            f"Access tags {sorted(access_tags or ())} do not match the node's"
            f" place in the tree, {_show(path)}, which needs {_tag_for(fields)!r}"
        )
    return fields


def _checked_tags(result: list[str], fields: dict[str, str]) -> AccessTags:
    """OPA's tags for a node, which must be the one tag its path needs."""
    tags = normalize_access_tags(result)
    if tags != {_tag_for(fields)}:
        raise ValueError(
            f"Authorisation gave the node tags {sorted(tags)},"
            f" not {_tag_for(fields)!r} as its place in the tree needs"
        )
    return tags


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
        create_node_endpoint: str = "tiled/node_tags",
        allowed_tags_endpoint: str = "tiled/user_tags",
        scopes_endpoint: str = "tiled/scopes",
        # Same rule as creating: it returns the tags for the node's new place,
        # or nothing if the caller may not put it there
        modify_node_endpoint: str = "tiled/node_tags",
        empty_access_tags_public: bool = True,
        provider: str | None = None,
    ):
        self._token_audience = token_audience
        # access token -> (monotonic expiry, filters)
        self._filters_cache: dict[str, tuple[float, Filters]] = {}

        super().__init__(
            authorization_provider=authorization_provider,
            create_node_endpoint=create_node_endpoint,
            allowed_tags_endpoint=allowed_tags_endpoint,
            scopes_endpoint=scopes_endpoint,
            provider=provider,
            modify_node_endpoint=modify_node_endpoint,
            empty_access_tags_public=empty_access_tags_public,
        )

    async def init_node(
        self,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        access_tags: AccessTags | None = None,
    ) -> tuple[bool, AccessTags]:
        _check_principal(principal)
        if access_tags is None and self._empty_access_tags_public is not None:
            return self._empty_access_tags_public, normalize_access_tags()
        # Ask OPA about the node's place in the tree, not just what it was
        # sent, so the tag always matches the path envoy routes on
        fields = _request_path_fields(access_tags)
        decision = await self._get_external_decision(
            self._create_node,
            self._input(principal, fields),
            ResultHolder[list[str]],
        )
        if decision and decision.result is not None:
            # The node's own tag, which build_input reads back
            return (True, _checked_tags(decision.result, fields))
        raise ValueError("Permission denied not able to add the node")

    async def modify_node(
        self,
        node: BaseAdapter,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        access_tags: AccessTags | None,
    ) -> tuple[bool, AccessTags]:
        _check_principal(principal)
        current = node.access_tags  # type: ignore
        if access_tags == current:
            logger.info(
                "Node access_tags not modified;"
                f" access_tags is identical: {access_tags}"
            )
            return (False, current)
        # A node's tag is fixed by its place in the tree, so this can only
        # restore that tag, e.g. on a node tagged by hand
        fields = _request_path_fields(access_tags)
        decision = await self._get_external_decision(
            self._modify_node,
            self._input(principal, fields),
            ResultHolder[list[str]],
        )
        # tiled stores whatever tags come back, whatever the bool says, so a
        # refusal has to raise (tiled turns it into a 403), and an approval
        # stores the tags OPA gives rather than the ones the caller sent.
        if decision and decision.result:
            new = _checked_tags(decision.result, fields)
            return (new != current, new)
        raise ValueError("Permission denied: not able to change the node's access")

    def build_input(
        self,
        principal: Principal,
        authn_access_tags: AccessTags | None,
        authn_scopes: Scopes,
        access_tags: AccessTags | None = None,
    ) -> str:
        return self._input(principal, _node_fields(access_tags or ()))

    def _input(self, principal: Principal, fields: dict[str, str]) -> str:
        """The OPA input for the caller and a node's fields."""
        _input: dict[str, str] = {"audience": self._token_audience}

        if (
            isinstance(principal, Principal)
            and principal.type is PrincipalType.user
            and principal.access_token is not None
        ):
            _input["token"] = principal.access_token.get_secret_value()

        _input.update(fields)
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
            filters = [AccessTagsFilter(normalize_access_tags(tags.result))]  # type: ignore
        if token:
            # ponytail: drops expired entries on every miss, O(cached tokens); fine
            # for hundreds of callers, use a TTL cache (e.g. cachetools) if that grows.
            self._filters_cache = {
                k: v for k, v in self._filters_cache.items() if v[0] > now
            }
            self._filters_cache[token] = (now + _tags_ttl(token), filters)
        return filters
