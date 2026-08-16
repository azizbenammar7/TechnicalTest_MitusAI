# Football Match Analysis Platform
**eSteps / Mitus.AI — AI Intern Technical Test**

## V1 and V2

- **V1 — original technical-test prototype:** the historical YOLOv8 pipeline,
  committed outputs, heuristic scoring, and Streamlit dashboard documented
  below. Tag `technical-test-v1.0` preserves this baseline unchanged.
- **V2 — professional platform under active development:** streaming video
  ingestion, versioned immutable attempts, an atomic local job queue, a
  separate execution worker, a safe legacy importer, and a React workflow for
  upload, live progress, cancellation, retry, and results.

Run the complete local V2 demo (the generated `demo_fast` profile requires no
ML model or GPU):

```bash
make v2-demo
```

Open the dashboard URL printed by the command. Uploaded bytes, run artifacts,
and local queue records are ignored by Git. Imported legacy views prominently identify
unverified tracks, approximate movement data, and advisory-only workload
indicators; generated workflow values are persistently labeled synthetic. See
[`docs/v2/LOCAL_DEMO.md`](docs/v2/LOCAL_DEMO.md) and
[`docs/v2/ANALYSIS_EXECUTION.md`](docs/v2/ANALYSIS_EXECUTION.md).

For genuine local YOLOv8m + ByteTrack compatibility execution:

```bash
make v2-v1-compat-setup
make v2-v1-compat-smoke
make v2-demo-v1-compat
```

See [`docs/v2/V1_COMPAT_SETUP.md`](docs/v2/V1_COMPAT_SETUP.md). `v1_compat`
is a preserved-algorithm compatibility profile. It is not the future
detector-neutral V2 production engine.

Production Platform status: **P1 containerized service boundaries are
implemented and locally validated** for the compiled React frontend, FastAPI
API, and long-lived worker. Start the local stack with `make p1-build && make
p1-up`; see
[`docs/v2/production-containerization.md`](docs/v2/production-containerization.md).

**P2 cloud adapters are implemented behind the provider-neutral ports**:
`PostgreSQLAnalysisRepository` (control plane, emulator-validated against a
PostgreSQL container), `AzureBlobObjectStorage` with a direct-upload boundary
(data plane, emulator-validated against Azurite), and `AzureServiceBusQueue`
(implemented; validated against a faithful fake broker — real-Azure validation
pending). A fail-fast composition root selects backends via
`FOOTBALLAI_{DATABASE,OBJECT_STORAGE,QUEUE}_BACKEND`; the local adapters remain
the default.

**The real coordinator, worker, executor, and API read path now run entirely
through the ports** (`AnalysisRepository` + `ObjectStorage` + `JobQueue`), with
no shared filesystem between the API and the worker. The same execution code
runs locally or split across PostgreSQL + Blob-compatible storage + queue; a
split-plane end-to-end test drives `demo_fast`, retry, cancellation,
deterministic failure, and duplicate-delivery idempotency against PostgreSQL +
Azurite (`make p2-split-up && make p2-split-test`). No Azure resources were
created and no Azure credit consumed. See
[`docs/v2/production-cloud-adapters.md`](docs/v2/production-cloud-adapters.md)
and run the emulator-backed suite with `make p2-db-up && make p2-test`.

**P3 Azure discovery and the Terraform staging foundation are complete.** The
selected low-cost France Central topology represents Blob Storage, Service Bus,
private PostgreSQL, ACR, a VNet-integrated Container Apps environment, separate
managed identities, frontend/API Container Apps and an event-driven worker Job.
Terraform formatting, validation and no-refresh planning pass, but no Azure
resources or images have been created. Workloads remain gated off pending P4
immutable images and an explicitly approved apply. See
[`docs/v2/azure-staging-architecture.md`](docs/v2/azure-staging-architecture.md)
and [`docs/v2/terraform.md`](docs/v2/terraform.md).

The remaining README is the preserved V1 technical-test documentation.

End-to-end pipeline: detect & track players in a full 90-minute match →
compute per-player stats → derive fatigue & injury-risk indicators →
display in a Streamlit dashboard built for coaches and physios.

---

## Quick start

> **Python version:** use **3.11–3.13** in a virtual environment. The ML stack
> (torch / ultralytics / pyarrow / scipy) does not yet ship prebuilt wheels for
> 3.14, so a default `python3` that is 3.14 will try to build from source and
> hang. On macOS: `python3.13 -m venv .venv && source .venv/bin/activate`.

```bash
# 1. Create and activate a virtual environment (Python 3.11–3.13)
python3.13 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3a. Run on a real match video
#     - you must supply your own video at data/raw/match.mp4 (gitignored)
#     - the first run auto-downloads the YOLOv8m weights (~50 MB)
python pipeline/01_track.py --video data/raw/match.mp4   # ~90–110 min on M4 (yolov8m @ 1280)
python pipeline/02_stats.py
python pipeline/03_fatigue.py

# 3b. OR generate synthetic demo data instantly (no video / no GPU needed)
#     This is the quickest way to verify the dashboard end-to-end locally.
python scripts/gen_dummy_data.py

# 4. Launch dashboard
streamlit run dashboard/app.py
```

---

## Repository structure

```
├── pipeline/
│   ├── 01_track.py        # YOLOv8m + ByteTrack → raw_tracks.parquet
│   ├── 02_stats.py        # tracking data → player stats + heatmaps
│   ├── 03_fatigue.py      # fatigue indicators + injury-risk scores
│   └── bytetrack_custom.yaml  # tracker config (longer buffer = fewer ID switches)
├── dashboard/
│   └── app.py             # Streamlit dashboard (reads precomputed files)
├── scripts/
│   └── gen_dummy_data.py  # generates synthetic match data for dashboard dev
├── data/
│   ├── raw/               # place your video here (gitignored)
│   └── processed/         # pipeline outputs (gitignored)
├── requirements.txt
└── README.md
```

---

## Architecture

```
match.mp4
   │
   ▼  01_track.py  (YOLOv8m, ~5 effective FPS, device=mps)
raw_tracks.parquet   [frame, time_sec, track_id, cx, cy, w, h, conf]
   │
   ▼  02_stats.py  (pandas + NumPy)
player_stats.parquet [track_id, block, half, distance_m, mean_speed, sprints, …]
player_summary.json  [per-player totals + heatmap + speed timeline]
   │
   ▼  03_fatigue.py  (scipy linregress + heuristic scoring)
risk_scores.json     [risk_score 0–100, risk_flag LOW/MEDIUM/HIGH, breakdown]
   │
   ▼  dashboard/app.py  (Streamlit + Plotly + Matplotlib)
```

**Why the steps are separated.** Each stage has a single, distinct
responsibility, so each can be re-run, tested, or replaced independently:

- **`01_track.py`** — the only *expensive* step (GPU computer vision). Run once
  per video; everything downstream reads its cached `raw_tracks.parquet`.
- **`02_stats.py`** — pure, deterministic movement/statistics (smoothing,
  speed/distance, sprints, heatmaps). No ML, no randomness.
- **`03_fatigue.py`** — small, readable heuristic scoring (≈5-minute read).
- **`dashboard/app.py`** — presentation only; it **never runs any ML** and
  reads the three precomputed files (< 5 MB total for a 90-minute match).

---

## Write-up: answers to §7

### 1. How did you get a full 90-minute match to process in reasonable time?

**Downsampling to ~5 effective FPS.** `01_track.py` derives the stride
automatically from the source FPS (`stride = round(src_fps / 5)`). This match
is 30 FPS / 169 264 frames → stride 6 → ~28 200 frames processed instead of the
full ~169 000. On Apple MPS (`device="mps"`) with `yolov8m` at `imgsz=1280`,
this runs at ~4–5 it/s on the M4, giving a **~90–110 minute wall-clock time**
for tracking (the stats and fatigue steps then take seconds).

Trade-offs:
- ~5 FPS misses very short events (< 0.2 s). Fast turns look slightly slower.
- `yolov8m` (medium) is chosen over `yolov8n` for noticeably better recall on
  small/distant players in broadcast footage; the cost is slower inference.
- No multi-processing: a single Python process keeps implementation simple
  and avoids VRAM contention on shared MPS memory.

### 2. Pixel → real-world distance/speed conversion

A standard pitch is 105 m × 68 m. We estimate the pitch length in pixels from
the video (`PITCH_PIXEL_WIDTH = 1216`, ~95 % of this 1280-wide video) and
derive a single scale factor:

```
M_PER_PX = 105 / PITCH_PIXEL_WIDTH  ≈ 0.086 m/px
```

Speed = Euclidean pixel displacement between consecutive frames × M_PER_PX ÷ Δt.

**Main error sources:**
1. Camera pan / zoom: the scale varies across the frame and over time. A homography
   (pitch-line detection → perspective transform) would fix this but takes 4–6 hours
   to implement reliably. For order-of-magnitude distance estimates it is not worth it.
2. Players near the edges of the frame are stretched (barrel distortion).
3. The bounding-box centre is the foot position; it jumps when a player crouches
   or is partially occluded.

**How we control error (`02_stats.py`, `compute_kinematics`):**
- **Position smoothing** — a rolling-median window (≈1 s) on the box centres
  removes per-frame jitter before any kinematics are computed.
- **Speed cap** — any step implying > 12 m/s is treated as an artefact and clipped.
- **Consistent distance** — distance is reconstructed from the *capped* speed, so
  speed and distance never disagree. (An earlier version capped speed but summed raw
  displacement, which inflated totals to 20–40 km/player.)
- **Gap handling** — if a track is lost for > 3 s (`MAX_GAP_S`, occlusion /
  re-acquisition), the re-appearance jump contributes zero distance instead of
  a teleport.

**Honest note on absolute values.** A single global scale plus a moving,
zooming broadcast camera, plus sparse/fragmented tracking, means **absolute
distances are under-estimates** — on this real match the median tracked
distance is well below the 6–12 km a player actually runs, because most track
IDs only cover a fraction of the match. The numbers are reliable for *relative*
comparison and for demonstrating the pipeline, **not** as GPS-grade
measurements. See "What works / What doesn't" below.

### 3. Fatigue model

Four indicators, each scored 0–25, summed to a 0–100 risk score:

| Indicator | Calculation | Rationale |
|-----------|-------------|-----------|
| **Speed decay** | Linear slope of mean_speed per 15-min block | Bradley et al. (2010): ~12% speed drop across 90 min |
| **Sprint drop** | (H1 sprints − H2 sprints) / H1 sprints | Mohr et al. (2005): significant 2nd-half sprint reduction |
| **Distance drop** | (H1 dist − H2 dist) / H1 dist | Classic fatigue marker in GPS-based load monitoring |
| **HSR load** | Total distance relative to team 75th percentile | Malone et al. (2017): high running volume predicts next-day injury risk |

The **full 90-minute timeline is essential**: speed_decay and sprint_drop are
meaningless without at least 4–6 time-blocks to fit a trend. A 30-second clip
would produce a flat slope.

### 4. Injury-risk flag

Scores map directly:
- **LOW** < 40 — normal load, no significant fatigue signal
- **MEDIUM** 40–69 — one or two indicators elevated; monitor closely
- **HIGH** ≥ 70 — multiple fatigue signals; consider substitution
- **INSUFFICIENT** — track does not span both halves (see below)

The score is fully decomposable (the dashboard shows each sub-score). A coach
can understand at a glance *why* a player is flagged, which is more actionable
than a black-box probability.

**Handling ID switches honestly.** ByteTrack produces ID switches, so a 90-min
match yields many short-lived track IDs, not 22 clean players. The half-vs-half
indicators (sprint drop, distance drop) would otherwise flag every substituted
player or ID fragment as HIGH risk just because they only appear in one half.
We therefore only score fatigue for tracks present in **both halves** with
≥ 5 % match coverage (`MIN_COVERAGE_FRAC = 0.05` in `03_fatigue.py`);
everything else is surfaced as **INSUFFICIENT** rather than as a false positive.
The threshold is deliberately low because broadcast tracks are heavily
fragmented — a stricter gate would mark almost every track INSUFFICIENT. The
dashboard defaults to hiding these partial tracks but lets you toggle them on.

**5-minute read path (for evaluators).** All fatigue logic lives in
[`pipeline/03_fatigue.py`](pipeline/03_fatigue.py):
- Scoring functions: `speed_decay_score`, `sprint_drop_score`,
  `dist_drop_score`, `hsr_load_score` (each returns 0–25 pts).
- Gating rule: only tracks present in **both halves** (with ≥ 5 % coverage)
  are scored; the rest become `INSUFFICIENT`.
- Output: `data/processed/risk_scores.json` (score 0–100 + decomposed breakdown).

### 5. Approximate time per part

| Part | Time |
|------|------|
| Reading test, architecture design | 1 h |
| Pipeline (01–03) | 4 h |
| Dashboard | 3 h |
| Dummy data + testing | 1 h |
| README + demo recording | 1 h |
| **Total** | **~10 h** |

### 6. Where AI tools were used

- **Claude (Cursor)**: architecture planning, boilerplate code, this README.
- **Self-written / self-directed**: fatigue heuristic design, sprint/speed
  logic, calibration choices, and debugging tracking edge cases — including
  catching and fixing a calibration bug where the pixel→metre scale and speed
  cap had been mis-set, which had inflated speeds to physically impossible values.
- All generated code was reviewed, tested, and adjusted before committing.

### 7. With two more weeks I would build

1. **OpenCV homography** for frame-by-frame pitch calibration — removes the
   biggest accuracy bottleneck.
2. **Team colour clustering** (K-Means on jersey HSV) → auto-assign team A / B.
3. **Ball tracking** → possession stats, pass map, shooting zones.
4. **Event detection**: sprint starts, physical duels (proximity + deceleration),
   high-speed runs — pushes the dashboard from "load monitoring" to "game intelligence".
5. **Fine-tuned YOLOv8** on a football-specific dataset (SoccerNet-v2 has bounding-box
   labels) to drastically improve recall in crowded scenes.

---

## Robustness

- **Checkpointing** (`01_track.py`): detections are streamed to a CSV checkpoint
  every 3000 processed frames and only converted to the final parquet at the end.
  A crash (or Ctrl-C) at minute 85 keeps everything processed so far instead of
  losing the whole run.

## What works / What doesn't

**Works (end-to-end, reproducible):**
- ✅ **Tracking pipeline** — YOLOv8m + ByteTrack runs over a full 90-minute match
  and produces `raw_tracks.parquet`, with checkpointing for crash recovery.
- ✅ **Stats generation** — deterministic per-player distance, speed, sprint
  counts, 15-minute blocks, and heatmaps.
- ✅ **Fatigue scoring** — transparent, decomposable 0–100 injury-risk score with
  honest `INSUFFICIENT` handling for partial tracks.
- ✅ **Streamlit dashboard** — team overview + per-player detail, served entirely
  from precomputed files (no ML at view time).
- ✅ **Dummy-data path** — `gen_dummy_data.py` reproduces the full dashboard with
  clean synthetic tracks, no video or GPU required.

**Doesn't work yet / out of scope:**
- ❌ **Reliable player identity** — ByteTrack switches IDs (and the halftime break
  wipes them entirely). A "naive halftime stitch" pairs the top-22 most active H1
  and H2 tracks, but identity is not truly persistent.
- ❌ **Calibrated real-world measurements** — a single global pixel→metre scale on
  a panning/zooming broadcast camera, combined with sparse tracking, means
  **absolute distances are under-estimates** (median tracked distance on this
  match is far below the true 6–12 km). Use the values for *relative* comparison,
  not as ground truth. Homography-based calibration would fix this.
- ❌ **Team assignment** — all detections are pooled (referee included); no jersey
  clustering.
- ❌ **Ball tracking** — not implemented; no possession/pass/shot analytics.
- ❌ **Validated medical injury prediction** — the risk score is a transparent
  *heuristic* load indicator, not a clinically validated injury model.

> Tested on MacBook Air M4 16 GB. CUDA users should change `device="mps"` to
> `device="cuda"` in `pipeline/01_track.py`.

---

## Verify it works in 30 seconds

No video or GPU needed — generate synthetic data and launch the dashboard:

```bash
source .venv/bin/activate
python scripts/gen_dummy_data.py     # writes player_summary.json, risk_scores.json, …
streamlit run dashboard/app.py
```

In the browser you should see:
- A **Team Overview** page with KPI tiles (full-match vs. total tracks) and a
  sortable player table colour-coded LOW / MEDIUM / HIGH / INSUFFICIENT.
- A **Player Detail** page with a pitch **heatmap**, a per-block **speed
  timeline**, and the **decomposed fatigue breakdown** (the four 0–25 sub-scores).

> The committed `data/processed/` files are the real-match outputs. Running
> `gen_dummy_data.py` overwrites them locally for the demo — that's expected;
> just re-run `02_stats.py` / `03_fatigue.py` to restore the real numbers.

---

## Data sources

Any publicly available full-match football video works.
Options with open licensing:
- [SoccerNet](https://www.soccer-net.org/) — annotated broadcast matches
- [Roboflow Football datasets](https://universe.roboflow.com/search?q=football)
- YouTube tactical-cam uploads (check licence before use)
