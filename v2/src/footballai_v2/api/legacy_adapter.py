"""Transform imported V1 artifacts into stable advisory-only API responses."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any

from footballai_v2.api.models import (
    AdvisoryView,
    DistanceSummary,
    PlayerDetailResponse,
    PlayerListItem,
    PlayerListResponse,
    TeamBlock,
    TeamSummaryResponse,
    TimelinePoint,
)
from footballai_v2.contracts.v1 import AnalysisRun, DataOrigin
from footballai_v2.storage import ManifestConflictError, RunNotFoundError
from footballai_v2.storage.ports import ObjectStorage


class LegacyDataError(ValueError):
    """Raised when registered legacy JSON cannot be adapted safely."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    converted = float(value)
    return converted if math.isfinite(converted) else default


class LegacyRunAdapter:
    """Read integrity-checked legacy or V2 dashboard-compatible artifacts."""

    def __init__(self, object_storage: ObjectStorage, run: AnalysisRun) -> None:
        self.store = object_storage
        self.run = run
        self.summary = self._json_artifact("legacy-player-summary")
        self.advisories = self._optional_json_artifact("workload-advisory") or {}
        if isinstance(self.advisories, dict) and isinstance(self.advisories.get("tracks"), dict):
            self.advisories = self.advisories["tracks"]
        if not isinstance(self.summary, dict) or not isinstance(self.summary.get("players"), dict):
            raise LegacyDataError("legacy player summary has an unsupported shape")
        if not isinstance(self.advisories, dict):
            raise LegacyDataError("legacy advisory artifact has an unsupported shape")

    @property
    def warnings(self) -> list[str]:
        warnings = self.run.parameters.get("quality_warnings", [])
        return [str(item) for item in warnings] if isinstance(warnings, list) else []

    def _json_artifact(self, artifact_id: str) -> Any:
        try:
            content = self.store.read_artifact_bytes(self.run.run_id, artifact_id)
            return json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
        # FileNotFoundError covers the local RunNotFoundError and the object-storage
        # ObjectNotFoundError; ValueError covers the Blob adapter's bounded-read and
        # integrity failures. All map to the same safe, provider-neutral error.
        except (FileNotFoundError, ManifestConflictError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LegacyDataError(f"registered {artifact_id} artifact is unavailable or malformed") from exc

    def _optional_json_artifact(self, artifact_id: str) -> Any | None:
        if not any(item.artifact_id == artifact_id for item in self.run.artifacts):
            return None
        return self._json_artifact(artifact_id)

    def _players(self) -> dict[str, dict[str, Any]]:
        return {
            str(key): value
            for key, value in self.summary["players"].items()
            if isinstance(value, dict)
        }

    def team_summary(self) -> TeamSummaryResponse:
        players = self._players()
        distances = [_number(player.get("total_distance_m")) for player in players.values()]
        advisory_levels = Counter(
            str(value.get("risk_flag", "UNAVAILABLE"))
            for value in self.advisories.values()
            if isinstance(value, dict)
        )
        scored = sum(
            1
            for value in self.advisories.values()
            if isinstance(value, dict) and value.get("risk_score") is not None
        )
        insufficient = advisory_levels.get("INSUFFICIENT", 0)

        speeds: dict[int, list[float]] = defaultdict(list)
        block_distances: dict[int, float] = defaultdict(float)
        for player in players.values():
            timeline = player.get("speed_timeline", {})
            if isinstance(timeline, dict):
                for raw_block, raw_speed in timeline.items():
                    try:
                        block = int(raw_block)
                    except (TypeError, ValueError):
                        continue
                    speeds[block].append(_number(raw_speed))
            blocks_present = player.get("blocks_present", [])
            if isinstance(blocks_present, list) and blocks_present:
                share = _number(player.get("total_distance_m")) / len(blocks_present)
                for raw_block in blocks_present:
                    if isinstance(raw_block, int) and raw_block >= 0:
                        block_distances[raw_block] += share
        block_indexes = sorted(set(speeds) | set(block_distances))
        blocks = [
            TeamBlock(
                block_index=block,
                start_minute=block * 15,
                end_minute=(block + 1) * 15,
                average_speed_ms=(sum(speeds[block]) / len(speeds[block]) if speeds[block] else 0),
                estimated_distance_m=block_distances[block],
            )
            for block in block_indexes
        ]
        return TeamSummaryResponse(
            run_id=self.run.run_id,
            logical_analysis_id=self.run.logical_analysis_id,
            origin=self.run.data_origin.value,
            legacy=self.run.data_origin is DataOrigin.LEGACY_V1,
            match_duration_seconds=_number(
                self.summary.get("match_duration_s", self.summary.get("duration_s", 0))
            ),
            total_tracks=len(players),
            scored_tracks=scored,
            insufficient_tracks=insufficient,
            distance=DistanceSummary(
                total_m=sum(distances),
                average_per_track_m=(sum(distances) / len(distances) if distances else 0),
                maximum_track_m=max(distances, default=0),
            ),
            advisory_distribution=dict(sorted(advisory_levels.items())),
            blocks=blocks,
            warnings=self.warnings,
        )

    def player_list(self) -> PlayerListResponse:
        results = []
        for player_id, player in self._players().items():
            advisory = self.advisories.get(player_id, {})
            if not isinstance(advisory, dict):
                advisory = {}
            score = advisory.get("risk_score")
            results.append(
                PlayerListItem(
                    player_id=player_id,
                    label=(f"Legacy track {player_id}" if self.run.data_origin is DataOrigin.LEGACY_V1 else f"Unverified track {player_id}"),
                    identity_verified=False,
                    total_distance_m=_number(player.get("total_distance_m")),
                    average_speed_ms=_number(player.get("mean_speed_ms")),
                    peak_speed_ms=_number(player.get("peak_speed_ms")),
                    sprint_count=int(_number(player.get("total_sprints"))),
                    active_span_seconds=_number(player.get("active_time_s")),
                    coverage_fraction=_number(player.get("coverage_frac")),
                    advisory_level=str(advisory.get("risk_flag", "UNAVAILABLE")),
                    advisory_score=_number(score) if score is not None else None,
                )
            )
        results.sort(key=lambda item: (-item.total_distance_m, item.player_id))
        return PlayerListResponse(run_id=self.run.run_id, players=results, warnings=self.warnings)

    def player_detail(self, player_id: int) -> PlayerDetailResponse:
        key = str(player_id)
        player = self._players().get(key)
        if player is None:
            raise RunNotFoundError(f"track {player_id} was not found")
        advisory = self.advisories.get(key, {})
        if not isinstance(advisory, dict):
            advisory = {}

        raw_heatmap = player.get("heatmap", [])
        heatmap = []
        if isinstance(raw_heatmap, list):
            for row in raw_heatmap:
                if isinstance(row, list):
                    heatmap.append([_number(value) for value in row])

        raw_speed = player.get("speed_timeline", {})
        speed_points = []
        if isinstance(raw_speed, dict):
            for raw_block, value in raw_speed.items():
                try:
                    block = int(raw_block)
                except (TypeError, ValueError):
                    continue
                speed_points.append(TimelinePoint(block_index=block, minute=block * 15, value=_number(value)))
        speed_points.sort(key=lambda item: item.block_index)

        blocks_present = player.get("blocks_present", [])
        valid_blocks = sorted(
            item for item in blocks_present if isinstance(item, int) and item >= 0
        ) if isinstance(blocks_present, list) else []
        total_distance = _number(player.get("total_distance_m"))
        distance_points = [TimelinePoint(block_index=0, minute=0, value=0)]
        if valid_blocks:
            share = total_distance / len(valid_blocks)
            cumulative = 0.0
            for block in valid_blocks:
                cumulative += share
                distance_points.append(
                    TimelinePoint(block_index=block, minute=(block + 1) * 15, value=cumulative)
                )
        else:
            distance_points.append(TimelinePoint(block_index=1, minute=15, value=total_distance))

        raw_score = advisory.get("risk_score")
        indicators = advisory.get("fatigue_indicators", {})
        breakdown = advisory.get("score_breakdown", {})
        return PlayerDetailResponse(
            run_id=self.run.run_id,
            player_id=key,
            label=f"Unverified player track {key}",
            total_distance_m=total_distance,
            average_speed_ms=_number(player.get("mean_speed_ms")),
            peak_speed_ms=_number(player.get("peak_speed_ms")),
            sprint_count=int(_number(player.get("total_sprints"))),
            active_span_seconds=_number(player.get("active_time_s")),
            coverage_fraction=_number(player.get("coverage_frac")),
            heatmap=heatmap,
            speed_timeline=speed_points,
            distance_timeline=distance_points,
            advisory=AdvisoryView(
                level=str(advisory.get("risk_flag", "UNAVAILABLE")),
                score=_number(raw_score) if raw_score is not None else None,
                reason=str(advisory["reason"]) if advisory.get("reason") else None,
                indicators=indicators if isinstance(indicators, dict) else {},
                breakdown=breakdown if isinstance(breakdown, dict) else {},
            ),
            warnings=self.warnings,
        )
