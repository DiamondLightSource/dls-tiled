# Authorization

Three separate layers control access to data:

1. **Authentication.** You log in with your Diamond account through Keycloak.
   Services use a Keycloak service account (client credentials).
2. **Access policy.** Tiled asks [Open Policy Agent](https://www.openpolicyagent.org/)
   (OPA) what your token may see and do, using
   [`dls_tiled.policy`](../src/dls_tiled/policy.py).
3. **Filesystem ACLs.** Each beamline's pod can only read that beamline's files.
   See [Architecture](architecture.md#why-one-deployment-per-beamline).

This page describes the policy that is [running now](#current-policy) and the
[central authorization](#new-central-authorization) work that will replace it.

## How the policy works

Tiled stores **access tags** on every node. When you list or read nodes, the
policy asks OPA for the tags your token grants, and Tiled returns only nodes
carrying one of them. OPA makes every decision. `dls-tiled` translates between
Tiled and OPA.

| Tiled asks                 | OPA answers                                 |
| -------------------------- | ------------------------------------------- |
| What can this user see?    | A list of tags, or `["*"]` for everything   |
| What may they do here?     | A set of scopes, such as `read:data`        |
| May they create this node? | The tags to store on it, or a refusal       |
| May they retag this node?  | See the rules for each policy below         |

Both policies share this behaviour:

- **A user principal is required.** Anonymous requests get a `401`, and so do
  Tiled's own `service` principals. A Keycloak service account is unaffected: to
  Tiled its token is an ordinary user login, and OPA recognises it by the
  missing `fedid` claim.
- **Answers are cached per token**: for 60 seconds for users and 7.5 minutes for
  service accounts. A change of permissions can take that long to reach Tiled.
- **If OPA can't be reached**, the request gets no access. That result isn't
  cached, so access returns as soon as OPA does.

## Current policy

The rules are in the [`diamond.policy.tiled`](https://github.com/DiamondLightSource/authz/blob/main/policy/diamond/policy/tiled/tiled.rego)
package of the `authz` repository. The tree has three container levels:

```
<beamline>/<proposal>/<visit>/<run>
```

### Tags

A node carries its own tag, the grant tags of its ancestors (so one grant covers
a whole subtree), and on the two upper levels a `visible:` tag that lets people
with access further down list the node without gaining access to its contents.

| Level    | Tags stored on the node                                                                                  |
| -------- | -------------------------------------------------------------------------------------------------------- |
| Beamline | `beamline:i22`, `visible:beamline:i22`                                                                   |
| Proposal | `beamline:i22,proposal:12345`, `beamline:i22`, `visible:beamline:i22,proposal:12345`                     |
| Visit    | `beamline:i22,proposal:12345,session:12345-1`, `beamline:i22,proposal:12345`, `beamline:i22`             |

### Who can read what

| Caller                               | Granted tags                                              | Result                                       |
| ------------------------------------ | --------------------------------------------------------- | -------------------------------------------- |
| Admin                                | `*`                                                       | Everything                                   |
| Beamline admin, or service account   | The beamline tag                                          | Everything on the beamline                   |
| Proposal member                      | The proposal tag, plus the beamline's `visible:` tag      | All visits in the proposal                   |
| Visit member                         | The visit tag, plus `visible:` tags for the levels above  | That visit, and can list the path down to it |

### Who can write

Everyone who passes the checks above gets `read:metadata` and `read:data`. A
token whose audience includes `tiled-writer` also gets `write:metadata`,
`write:data`, `create:node` and `register`.

Creating a node needs one of the following, and the beamline must match the
proposal or visit:

- a named user with access to that beamline, proposal or visit
- a service account whose token's `beamline` claim matches, which is how blueapi
  writes runs

The client asks for a node by sending one access tag containing JSON, and OPA
replaces it with the real tags. blueapi sends this for each run:

```json
{"beamline": "i22", "proposal": "cm12345", "visit": 1}
```

A node created with no access tags at all is allowed without asking OPA
(`empty_access_blob_public`, on by default).

### Configuration

```yaml
access_control:
  access_policy: dls_tiled.policy:DiamondOpenPolicyAgentAuthorizationPolicy
  args:
    authorization_provider: http://opa:8181/v1/data/diamond/policy/
    token_audience: account
```

The OPA rule paths default to `tiled/user_sessions`, `tiled/user_session`,
`tiled/modify_session` and `tiled/scopes`. They are appended to
`authorization_provider`, so it needs its trailing `/`. The
[project README](../README.md#dls-access-policy) lists every argument.

## New central authorization

> **In development.** This is on the
> [`move-to-central-authz`](https://github.com/DiamondLightSource/dls-tiled/tree/move-to-central-authz)
> branch and hasn't been released. Details may change before it is.

The rules move to `diamond.tiled` in Diamond's central authorisation service
([`auth-auth/authorisation`](https://gitlab.diamond.ac.uk/auth-auth/authorisation)
on GitLab). The tree loses the proposal level, and each visit gains three
folders with different write permissions:

```
<instrument>/                i22                lets callers see the instrument node only
  <visit>/                   i22,cm12345-1
    raw/                     i22,cm12345-1      written by the instrument's tiled-writer
    processed/               i22,cm12345-1,p    written by the processing service account
    processing/              i22,cm12345-1,t    written by anyone on the visit
```

Anyone on the visit can read all three folders, and can see the instrument node
above them. So there is now somewhere for visit members to write their own
results: `processing/`.

### What changes

|                       | Current                                                     | Central                                                                 |
| --------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------- |
| Rules                 | `diamond.policy.tiled`, in `authz` on GitHub                | `diamond.tiled`, in `authorisation` on GitLab                           |
| Tree                  | `beamline/proposal/visit/run`                               | `instrument/visit/{raw,processed,processing}/run`                       |
| Tags on a node        | Several per node (`beamline:i22,proposal:...`)              | Exactly one (`i22,cm12345-1,t`)                                         |
| Create request        | `{"beamline", "proposal", "visit"}`                         | `{"instrument", "instrument_session", "folder"}`                        |
| Node tag decided by   | The client's request                                        | The node's **path**, which requests must agree with                     |
| Untagged create       | Allowed, and left untagged                                  | Given the tag its path needs, if OPA allows it                          |
| Changing tags         | Yes or no from OPA                                          | Can only restore the tag the path needs                                 |
| Server command        | `tiled serve`                                               | `dls-tiled serve` (required)                                            |
| Tiled version         | 0.2.18 (`access_blob`)                                      | 0.2.21 (`access_tags`)                                                  |
| OPA rules             | `user_session`, `user_sessions`, `modify_session`, `scopes` | `node_tags` (create and modify), `user_tags`, `scopes`                  |

### A node's tag follows its path

Envoy routes on the instrument in the path, so a node's tag must always match
where the node is. The policy works out the instrument, visit and folder from the
path being written to, and asks OPA about that place. It refuses with a `403`,
without calling OPA:

- tags that name a different instrument, visit or folder from the path
- a node anywhere else: at the root, as a fourth folder of a visit, with an
  upper-case visit key, or with no key
- changing a node's tags to anything other than the tag its path needs, so a node
  can't be moved to another folder by retagging

It also rejects any OPA answer other than the single tag that the path needs.

Tiled doesn't tell an access policy where a node is being created. `dls-tiled
serve` is `tiled serve` plus a middleware that records this, and without it the
policy refuses to create nodes or change their tags. The image runs it by
default. Tiled's GraphQL entities and links aren't in the tree, so they are still
tagged from what the caller sends.

### Creating a node

Send one access tag holding JSON. `folder` defaults to `raw`, and leaving out
`instrument_session` means the instrument node:

```json
{"instrument": "i22", "instrument_session": "cm12345-1", "folder": "processing"}
```

### Configuration

```yaml
access_control:
  access_policy: dls_tiled.policy:DiamondOpenPolicyAgentAuthorizationPolicy
  args:
    authorization_provider: http://opa:8181/v1/data/diamond/
    token_audience: account
```

### Migrating

Existing runs have to move into the new tree and be retagged. The scripts and a
runbook, including how to back up the database first, are in `database_migration/`
on the branch (`migrate_to_central_authz.py` and its `README.md`).
