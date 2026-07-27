FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build

RUN python -m pip install --no-cache-dir "uv==0.11.32"

# Keep dependency resolution cacheable. A frozen lockfile must resolve before
# application code is copied, so dependency failures always fail the build.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY configs ./configs
COPY main.py ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime

ARG VCS_REF=unknown
ARG VERSION=dev
LABEL org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.version=$VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=app:app /build/src ./src
COPY --from=builder --chown=app:app /build/configs ./configs
COPY --from=builder --chown=app:app /build/main.py ./

USER app
EXPOSE 8000
CMD ["python", "main.py", "prod"]
