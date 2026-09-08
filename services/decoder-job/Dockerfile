# Decode container, built for AWS Fargate.
#
# Two stages so the runtime image carries no compiler and no build cache.
# Fargate charges for vCPU-seconds from task start, and a smaller image
# starts sooner, so the size is not vanity.
#
# Pinned by digest rather than tag: an image that decodes flight data has to
# be reproducible, and "python:3.12-slim" is a moving target.

# ---------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The decoder itself, installed from the monorepo package rather than copied
# as loose files, so the version in the image is the version in the registry
# row it stamps onto every output.
COPY packages/qar-decode /build/qar-decode
RUN pip install --no-cache-dir /build/qar-decode

# ---------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin decoder

COPY --from=build /opt/venv /opt/venv
COPY services/decoder-job/src /app

WORKDIR /app
USER 10001

# A FAP is unpacked to a temporary directory at startup, so the task needs
# writable scratch. Fargate gives every task 20 GB of ephemeral storage at
# /tmp; nothing else in the filesystem needs to be writable, and the task
# definition should set readonlyRootFilesystem with a tmpfs mount here.
VOLUME ["/tmp"]

# Fails fast and offline: synthesises a recording and puts it through the
# real pipeline -- container, sync, framing, parameters -- without touching
# S3. Exercising the live modules is the point: a health check that tests a
# reference implementation nothing calls lets a broken image report itself
# healthy.
HEALTHCHECK --interval=30s --timeout=10s --retries=2 \
    CMD ["python", "-m", "qar_decode.selftest"]

# No arguments: the container reads SOURCE_BUCKET and SOURCE_KEY from the
# environment, which ECS injects via containerOverrides.
ENTRYPOINT ["python", "/app/main.py"]
