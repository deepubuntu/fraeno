FROM python:3.10-slim@sha256:c1e4e6c01eb489c422288b2de34b0761ca316f7a2d98e2c33f47659a73ed108a AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --target /runtime .

FROM ros:humble-ros-base-jammy@sha256:afb40d6be65331c20a114d4e229a7ef099fed1b17bf6370daee193514b32aa16

ARG FRAENO_VERSION=0.1.0
LABEL org.opencontainers.image.title="Fraeno runner" \
      org.opencontainers.image.description="Isolated robot integration validation runner" \
      org.opencontainers.image.version="${FRAENO_VERSION}" \
      org.opencontainers.image.source="https://github.com/deepubuntu/fraeno"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/fraeno/runtime \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 65532 fraeno-sandbox \
    && useradd \
        --uid 65532 \
        --gid 65532 \
        --no-create-home \
        --shell /usr/sbin/nologin \
        fraeno-sandbox

COPY --from=builder /runtime /opt/fraeno/runtime
RUN test "$(python3 -m fraeno.cli --version)" = "${FRAENO_VERSION}"

WORKDIR /work
ENTRYPOINT ["python3", "-m", "fraeno.cli"]
