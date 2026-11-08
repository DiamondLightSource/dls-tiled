# Run in a container

Pre-built containers with dls-tiled and its dependencies already
installed are available on [Github Container Registry](https://ghcr.io/DiamondLightSource/dls-tiled).

## Starting the container

To pull the container from github container registry and run:

```
$ docker run ghcr.io/diamondlightsource/dls-tiled:latest tiled --version
```

To get a released version, use a numbered release instead of `latest`.
