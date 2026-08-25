"""The local manifest store must satisfy the AnalysisRepository contract."""

from __future__ import annotations

import pytest

from footballai_v2.storage import LocalAnalysisRunStore
from contracts.analysis_repository_contract import AnalysisRepositoryContract


class TestLocalRepositoryContract(AnalysisRepositoryContract):
    @pytest.fixture
    def repository(self, tmp_path):
        return LocalAnalysisRunStore(tmp_path / "runs")
