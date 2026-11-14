# Use a service account

Scripts and services that run without a person logging in, such as Unattended Data
Collection (UDC), authenticate to Tiled with a Keycloak service account.

The account's token carries a `beamline` claim, and it can see that beamline's
data. Its audience decides what else it can do:

| Audience       | Access                                 | Typical use  |
| -------------- | -------------------------------------- | ------------ |
| `tiled`        | Read-only                              | UDC          |
| `tiled-writer` | Read and write on its beamline         | blueapi      |

## Read Tiled from UDC

This example uses a client called `p46UDC`, with `beamline` set to `p46` and the
audience `tiled`. Keep the secret out of your code and pass it in through the
environment, for example from a `.env` file that isn't committed.

The deployed Tiled is version 0.2.18, so install the matching client, which uses
`httpx`:

```sh
pip install "tiled[client]==0.2.18" httpx
```

```python
import os

import httpx
from tiled.client import from_uri
from tiled.queries import Key

TOKEN_URL = "https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/token"


class ServiceAccountAuth(httpx.Auth):
    def __init__(self, client_id, client_secret):
        self.form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }

    def sync_auth_flow(self, request):
        response = httpx.post(TOKEN_URL, data=self.form).raise_for_status()
        request.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield request


client = from_uri(
    "https://tiled.diamond.ac.uk",
    auth=ServiceAccountAuth("p46UDC", os.environ["TILED_CLIENT_SECRET"]),
)

visit = client["p46/cm44194/cm44194-1"]
finished = visit.search(Key("stop.exit_status") == "success")
print(len(finished))
```

From version 0.2.21 the Tiled client uses `httpx2`. When the deployment moves to it,
change the import to `import httpx2 as httpx`.

From here, everything in [Searching](search.md) works as it does for a person.

## Create a processed folder

:::{warning}
This is a workaround until the
[central authorization policy](../explanations/authorization.md#new-central-authorization)
is released, when a dedicated processing service account will replace it. It uses
**blueapi's `tiled-writer` secret**, which can write anywhere on the beamline. Only
use it on a machine you trust, read it from the environment, and never commit it or
paste it into a notebook.
:::

Add this to the script above. It creates the folder and writes one result into it.

```python
import json

import numpy as np


def access_tags_for(beamline, visit):
    proposal, number = visit.split("-")
    blob = {"beamline": beamline, "proposal": proposal, "visit": int(number)}
    return [json.dumps(blob)]


def get_or_create(parent, key, access_tags):
    try:
        return parent[key]
    except KeyError:
        return parent.create_container(key=key, access_tags=access_tags)


def processed_folder(client, beamline, visit):
    access_tags = access_tags_for(beamline, visit)
    visit_node = get_or_create(client[beamline], visit, access_tags)
    return get_or_create(visit_node, "processed", access_tags)


def new_result(folder, name, beamline, visit):
    return folder.create_container(
        key=name,
        metadata={"start": {"instrument": beamline, "instrument_session": visit}},
        access_tags=access_tags_for(beamline, visit),
    )


BEAMLINE = "p46"
VISIT = "cm44194-1"

writer = from_uri(
    "https://tiled.diamond.ac.uk",
    auth=ServiceAccountAuth(
        os.environ["TILED_WRITER_CLIENT_ID"], os.environ["TILED_WRITER_CLIENT_SECRET"]
    ),
)

processed = processed_folder(writer, BEAMLINE, VISIT)
result = new_result(processed, "fit-1", BEAMLINE, VISIT)
result.write_array(
    np.linspace(0, 1, 100), key="fit", access_tags=access_tags_for(BEAMLINE, VISIT)
)
```

The account's `beamline` claim must match `BEAMLINE`, and the visit must be on that
beamline.

To let your data move to the new policy without further work, keep to these rules:

- **Use `<beamline>/<visit>/processed`, as the script does.** This is deliberately
  not under `<beamline>/<proposal>/<visit>`, because it is the layout that the new
  policy uses. The migration leaves it in place. A `processed` folder in the old tree
  would not migrate correctly: runs in it would move to `raw`, and anything else would
  be left behind where nobody but admins could see it.
- **Put everything in a result container from `new_result`.** The `start` metadata is
  what makes the migration treat the result as a run and retag everything inside it.
  A node written directly into `processed` isn't retagged.
- **Pass `access_tags` every time you create a node**, including arrays and tables
  inside a result. A node created without tags isn't tied to the visit's permissions.

After the migration, paths stay the same and each result has the tag
`<beamline>,<visit>,p`, so everyone on the visit can read it.
