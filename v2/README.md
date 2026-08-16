# FootballAi V2

V2 is the professional platform generation. It is developed alongside the
preserved V1 technical-test implementation. V2 does not import from
or write into `../pipeline/`, `../dashboard/`, `../scripts/`, or
`../data/processed/`.

The production-like container boundary and cloud-neutral P2 preparation are
documented in [`../docs/v2/production-containerization.md`](../docs/v2/production-containerization.md).

## Analysis-run contract

The first published analysis-run contract is:

```text
footballai.analysis-run/v1
```

The platform generation is V2; the contract version is independently v1. The
authoritative dependency-free Python model is under
`src/footballai_v2/contracts/v1/`. The generated JSON Schema and safe examples
are under `contracts/analysis-run/`.

Run lifecycle values are exactly `queued`, `running`, `succeeded`, `partial`,
`failed`, and `cancelled`. The terminal values (`succeeded`, `partial`,
`failed`, `cancelled`) are immutable. A partial attempt ended before all
mandatory work completed but produced valid, reviewable artifacts.

Data origins are explicit:

- `real`: genuine user-provided or operational footage;
- `synthetic`: generated development or demonstration data;
- `evaluation`: licensed benchmark or validation footage;
- `legacy_v1`: imported historical V1 artifacts. This label does not certify
  that those artifacts meet V2 quality requirements.

## Logical analysis and attempt chain

One logical input can have multiple isolated attempts:

```text
Logical analysis
├── Attempt 1 — failed
├── Attempt 2 — partial
└── Attempt 3 — succeeded
```

Each manifest records UUID-v4 `logical_analysis_id` and `run_id` values,
`attempt_number`, and `previous_attempt_run_id`. The first attempt uses number
1 and a null previous ID. A retry is allowed only from `failed` or `partial`;
it receives a new run ID and directory, retains logical input identity and data
origin, increments the attempt number, links to the previous run, and never
rewrites historical manifests or artifacts.

Code revision, dirty state, pipeline version, parameters, and model versions
are attempt-specific. A retry may deliberately change them, and the new
manifest must expose those differences. Within one attempt, all relationship
and provenance fields are immutable after namespace creation.

## Stage execution records

Runs may schedule any subset of the stable initial stages: `ingestion`,
`video_validation`, `detection`, `tracking`, `identity_resolution`,
`pitch_calibration`, `metrics`, `workload_advisory`, and
`artifact_publication`.

Each stage records its ID, stable name, whether it is required, status,
progress percentage, attempt number, start and finish timestamps, produced
artifact IDs, structured safe error, JSON-compatible finite performance
metrics, and an optional safe message. Stage statuses are `queued`, `running`,
`succeeded`, `partial`, `failed`, `cancelled`, and `skipped`.

Progress is bounded from 0 to 100. Running stages require a start time;
terminal stages require a finish time; time cannot move backwards; succeeded
stages require 100%; and failed stages require sanitized structured error
information. Artifact IDs must resolve to artifacts registered in the same
manifest. Stage IDs and names cannot be duplicated within one attempt.

A running manifest may temporarily have no active stage while work has not yet
been scheduled or while the local coordinator is between stages. Terminal run
validation is stricter: success requires terminal records and all required
stages to succeed; partial requires a useful artifact, a safe reason, and an
incomplete required stage; failed requires a structured run error; and no
terminal run may contain a running stage.

## Artifacts and advisory terminology

Artifacts have stable IDs, categories, relative paths, media types, byte sizes,
SHA-256 hashes, and optional schema versions. The public serialized category
and stage name is `workload_advisory`. Documentation and user interfaces call
it **Workload and Fatigue Advisory**.

This advisory is not a medical diagnosis, validated injury prediction, or
clinical advice. V1 terminology remains unchanged for historical compatibility.

## Configurable local storage

`LocalAnalysisRunStore` always receives its root from the caller:

```text
<configured-root>/<run-id>/
├── manifest.json
├── input/source.<ext>
├── artifacts/...
├── logs/
└── tmp/
```

Tests use temporary directories. A future local application configuration may
default to `data/runs`. A configured worker can mount any suitable root. A
future Azure Blob adapter may map an attempt to `runs/<run-id>/...`; Azure is
conceptual only and is not implemented in this milestone.

Directories and artifacts are created exclusively, traversal and symlink
escapes are rejected, manifest replacement is atomic, and registered artifact
bytes are verified against size and SHA-256 metadata before terminal success
or partial completion.

## Regenerate and verify

From the repository root:

```bash
PYTHONPATH=v2/src .venv-test/bin/python -m footballai_v2.contracts.v1.schema \
  v2/contracts/analysis-run/v1.schema.json

PYTHONPYCACHEPREFIX=/tmp/footballai-v2-pycache \
PYTHONPATH=v2/src \
.venv-test/bin/python -m pytest -q -ra
```

The schema drift test compares the committed schema with fresh Python output,
and every committed example is checked against both JSON Schema and the Python
contract. Tests use tiny generated media only, access no cloud services,
download no model weights, and write no V1 data.

## Import the preserved V1 demo artifacts

The Milestone 3 importer reads the committed V1 artifacts without modifying or
recomputing them, then copies supported files into a new isolated
`legacy_v1` run:

```bash
PYTHONPATH=v2/src .venv-test/bin/python -m footballai_v2.cli.import_legacy_v1 \
  --source data/processed \
  --output-root data/runs
```

The command prints the generated run ID, terminal status, and manifest path.
`data/runs/` is ignored because it is reproducible local runtime state. The
imported manifest and warning artifact explain that track identities,
calibration, movement, coverage, workload, and execution provenance remain
approximate legacy data. No video, detector, tracking, or metric computation is
performed by the importer.

## Run the local execution workflow

The one-command demo starts FastAPI, a durable local queue worker, and React:

```bash
make v2-demo
```

For separate process development:

```bash
FOOTBALLAI_V2_RUN_ROOT=data/runs \
FOOTBALLAI_V2_QUEUE_ROOT=data/job-queue \
FOOTBALLAI_V2_CORS_ORIGINS=http://localhost:5173 \
PYTHONPATH=v2/src \
.venv-test/bin/python -m uvicorn footballai_v2.api.main:app \
  --host 127.0.0.1 --port 8000

FOOTBALLAI_V2_RUN_ROOT=data/runs \
FOOTBALLAI_V2_QUEUE_ROOT=data/job-queue \
PYTHONPATH=v2/src \
.venv-test/bin/python -m footballai_v2.worker
```

The API is available at `http://localhost:8000/api/health`. It exposes upload,
profile, progress, cancellation, retry, clone, manifest, artifact, team, and
unverified-track models. Registered artifact bytes are integrity-checked.
Internal absolute paths are redacted, UUID-v4 run IDs are validated, and CORS
accepts only explicit HTTP localhost origins.
