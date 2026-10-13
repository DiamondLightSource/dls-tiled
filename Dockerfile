# The upstream tiled release that the runtime image extends
ARG TILED_VERSION=0.2.18

# The devcontainer should use the developer target and run as root with podman
# or docker with user namespaces.
FROM ghcr.io/diamondlightsource/ubuntu-devcontainer:resolute AS developer

# Add any system dependencies for the developer/build environment here
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    graphviz \
    && apt-get dist-clean

# The build stage makes a wheel of the plugins
FROM developer AS build

WORKDIR /app
COPY . /app
RUN uv build --wheel --out-dir /dist

# The runtime stage is the upstream tiled image with the plugins added to its venv
FROM ghcr.io/bluesky/tiled:${TILED_VERSION} AS runtime
ARG TILED_VERSION

USER root
RUN --mount=from=ghcr.io/astral-sh/uv:0.13,source=/uv,target=/bin/uv \
    --mount=from=build,source=/dist,target=/dist \
    uv pip install --python /app/bin/python --no-cache /dist/*.whl \
    && chown -R app:app /app
USER app

# Logging config that also shows the plugin loggers: use with
# tiled serve config ... --log-config /deploy/log_config.yml
COPY log_config.yml /deploy/log_config.yml

# Check the plugins import and that upstream tiled was left at its release
RUN python -c "import dls_tiled.visr, tiled; \
assert tiled.__version__ == '${TILED_VERSION}', tiled.__version__"
