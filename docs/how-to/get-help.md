# Get help when you can't read data

Create a ticket in the
[ACQP Jira project](https://jira.diamond.ac.uk/projects/ACQP/issues/). Include:

- the beamline and visit, for example `p51` and `cm44254-1`
- the full node path, or the run's uid
- what you ran, the full error, and roughly when it happened
- your FedID, so access can be checked

Before raising a ticket, these symptoms can help you tell what is wrong:

| Symptom                                                 | Likely cause                                                                                                                                                                                                                  |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `401 Unauthorized`                                      | You are not logged in, or your token has expired. Connect again.                                                                                                                                                              |
| The node isn't there (`KeyError`, `404`)                | You don't have access to that visit, or the path is wrong. If you were only just added to the visit, wait a minute: Tiled remembers your permissions for up to 60 seconds.                                                    |
| The metadata reads, but reading the data gives a `500`  | Usually a problem with the file, not your permissions: Tiled can't open it, or a dataset is missing from it. The beamline's pod may also lack filesystem access to it. Quote the error's correlation ID in the ticket if you have it. |
| `503` with `Retry-After`                                | The file is still catching up with the catalog during a scan. Try again shortly.                                                                                                                                              |
| A whole beamline's data is unreadable                   | The beamline may not have its own Tiled deployment or data ACL yet ([why](../explanations/architecture.md#adding-a-beamline)). Raise a ticket.                                                                                                |

## CORS errors from a web UI

If you are integrating your own web UI with Tiled, the browser will block its
requests with a CORS error until your UI's origin is allowed. Tiled controls
this with the `allow_origins` setting in its
[service configuration](https://blueskyproject.io/tiled/reference/service-configuration.html):

```yaml
allow_origins:
  - https://my-ui.diamond.ac.uk
```

You can't change this yourself. Create a ticket in the
[ACQP Jira project](https://jira.diamond.ac.uk/projects/ACQP/issues/) with the
origin (scheme, host and port) your UI is served from, and the core team will
look into it.
