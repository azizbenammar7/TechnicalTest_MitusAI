FROM python:3.14.6-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
COPY v2/pyproject.toml v2/README.md ./
COPY v2/src ./src
COPY v2/requirements-worker-core.txt ./requirements-worker-core.txt
COPY v2/requirements-v1-compat.txt ./requirements-v1-compat.txt
COPY v2/requirements-postgres.txt ./requirements-postgres.txt
COPY v2/requirements-azure.txt ./requirements-azure.txt
# Toolchain + libpq headers so the psycopg-c wheel can compile against libpq.
# Confined to the build stage; the runtime image only carries libpq5 itself.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends gcc libpq-dev python3-dev \
 && rm -rf /var/lib/apt/lists/*
RUN sed '/^torch==/d; /^torchvision==/d' requirements-v1-compat.txt > requirements-worker-v1.txt \
 && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels \
      -r requirements-worker-core.txt -r requirements-postgres.txt -r requirements-azure.txt \
 && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels \
      --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 torchvision==0.28.0 \
 && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels -r requirements-worker-v1.txt \
 && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels . \
 && /opt/venv/bin/python -m pip install --no-cache-dir --no-index --no-compile --no-deps /wheels/*.whl

FROM python:3.14.6-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FOOTBALLAI_ENVIRONMENT=production \
    FOOTBALLAI_APPLICATION_ROOT=/opt/footballai \
    FOOTBALLAI_V2_RUN_ROOT=/var/lib/footballai/runs \
    FOOTBALLAI_V2_QUEUE_ROOT=/var/lib/footballai/queue \
    FOOTBALLAI_QUEUE_BACKEND=local \
    FOOTBALLAI_OBJECT_STORAGE_BACKEND=local \
    FOOTBALLAI_DATABASE_BACKEND=local_manifest \
    FOOTBALLAI_V1_COMPAT_MODEL_PATH=/models/yolov8m.pt \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get upgrade --yes \
 && apt-get install --yes --no-install-recommends ffmpeg libgomp1 libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 footballai \
 && useradd --system --uid 10001 --gid footballai --home-dir /nonexistent --shell /usr/sbin/nologin footballai \
 && install -d -o footballai -g footballai /var/lib/footballai/runs /var/lib/footballai/queue /opt/footballai/pipeline /models
COPY --from=build /opt/venv /opt/venv
COPY --chown=footballai:footballai pipeline/02_stats.py pipeline/03_fatigue.py pipeline/bytetrack_custom.yaml /opt/footballai/pipeline/

ENV YOLO_CONFIG_DIR=/tmp

USER footballai:footballai
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -m footballai_v2.healthcheck worker || exit 1
CMD ["python", "-m", "footballai_v2.worker"]
