# Container image that runs run_pipeline.py (COMPASS, dolphin, or both).
#
# Build:  docker build -t compass-dolphin .
# Run:    see "Run with Docker" in Readme.md.
#
# The base image ships micromamba, a small conda installer, so the image uses the
# same environment.yaml as a local setup instead of a separate requirements list.
FROM mambaorg/micromamba:2.0.5

# Activate the environment during RUN steps as well. environment.yaml installs
# COMPASS and dolphin from GitHub with pip, which needs the environment's git.
ARG MAMBA_DOCKERFILE_ACTIVATE=1

# Install the dependencies first. This layer is cached and only rebuilt when
# environment.yaml changes, not on every code change.
COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yaml /tmp/environment.yaml
RUN micromamba install --yes --name base --file /tmp/environment.yaml \
    && micromamba clean --all --yes

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER run_pipeline.py ./
COPY --chown=$MAMBA_USER:$MAMBA_USER pipeline ./pipeline
COPY --chown=$MAMBA_USER:$MAMBA_USER config ./config
COPY --chown=$MAMBA_USER:$MAMBA_USER tests ./tests

# Print log lines immediately instead of buffering them.
ENV PYTHONUNBUFFERED=1

# The base image's entrypoint activates the conda environment, then runs this.
# Arguments given to `docker run` are appended, e.g. `compass --compass-config ...`.
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "/app/run_pipeline.py"]
