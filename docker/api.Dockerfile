FROM python:3.13.11-alpine3.23 AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
COPY v2/pyproject.toml v2/README.md ./
COPY v2/src ./src
COPY v2/requirements-api.txt ./requirements-api.txt
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels -r requirements-api.txt \
 && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels . \
 && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --no-compile --no-deps /wheels/*.whl

FROM python:3.13.11-alpine3.23 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FOOTBALLAI_ENVIRONMENT=production \
    FOOTBALLAI_V2_RUN_ROOT=/var/lib/footballai/runs \
    FOOTBALLAI_V2_QUEUE_ROOT=/var/lib/footballai/queue \
    FOOTBALLAI_QUEUE_BACKEND=local \
    FOOTBALLAI_OBJECT_STORAGE_BACKEND=local \
    FOOTBALLAI_DATABASE_BACKEND=local_manifest \
    FOOTBALLAI_API_HOST=0.0.0.0 \
    FOOTBALLAI_API_PORT=8000 \
    FOOTBALLAI_API_WORKERS=1 \
    PATH=/opt/venv/bin:$PATH

RUN apk add --no-cache ffmpeg \
 && addgroup -S -g 10001 footballai \
 && adduser -S -D -H -u 10001 -G footballai footballai \
 && install -d -o footballai -g footballai /var/lib/footballai/runs /var/lib/footballai/queue
COPY --from=build /opt/venv /opt/venv

USER footballai:footballai
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -m footballai_v2.healthcheck api || exit 1
CMD ["python", "-m", "footballai_v2.api.server"]
