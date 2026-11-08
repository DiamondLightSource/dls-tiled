# Request a custom endpoint

If a beamline needs something Tiled doesn't do, such as server-side processing for a
live display, we can add a custom endpoint for it. Custom endpoints are plugins that
ship in the `dls-tiled` image.

To ask for one, create a ticket in the
[ACQP Jira project](https://jira.diamond.ac.uk/projects/ACQP/issues/). Include:

- the beamline that needs it
- what the endpoint should take and return
- what will call it, and how often
- why this can't be done on the client, for example because it would mean downloading
  a lot of data for each update

A beamline's endpoint can be added for **that beamline only**, so it doesn't affect any
other beamline.

## Example: ViSR on b01-1

ViSR (b01-1) has its own endpoint, `dls_tiled.visr:visr_router`. It bins a scan into a
2-D image on the server, so that a display doesn't have to fetch every point on each
update:

```
GET /api/v1/binned/<path to the run>
```

Other beamlines can ask to use it. Its parameters are described in the
[project README](https://github.com/DiamondLightSource/dls-tiled#dls_tiledvisrvisr_router).

## How an endpoint is added

For developers: an endpoint is a FastAPI router in `src/dls_tiled`, listed under
`routers:` in the Tiled server config. If it addresses nodes by path, it must also be
added to `envoy.pathPrefixes` in the Helm values. Otherwise its requests will
[reach `tiled-default`](../explanations/architecture.md#request-routing), which can't
read any beamline's files.
