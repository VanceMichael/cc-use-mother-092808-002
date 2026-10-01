"""报告查询：把增长区间、置信指数、主要市场与高价值货品贡献连在一起。

审核者可用审计段的聚合原料复算指数与归因；报告不含任何企业标识，
少于 MIN_CELL 家企业的分布单元必须合并，避免暴露单家企业。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .computations import (
    Attribution,
    CommodityContribution,
    IndexPair,
    MarketContribution,
    compute_indices,
)
from .errors import PrivacyError, RoleError, RoundStateError
from .models import Actor, ForecastScenario, Role, Round, RoundState, SampleFrame, SurveyResponse
from .store import Store

MIN_CELL = 3
_MERGED_LABEL = "已合并（小样本）"


@dataclass(frozen=True)
class DistributionCell:
    label: str
    enterprise_count: int


@dataclass(frozen=True)
class AuditTrail:
    """审核者复算所需的聚合原料，不含任何企业标识。"""

    enterprise_count: int
    unit_count: int
    total_weight: float
    current_weighted_score: float
    expected_weighted_score: float
    current_distribution: tuple[DistributionCell, ...]
    expected_distribution: tuple[DistributionCell, ...]
    base_total_value: float
    current_total_value: float


@dataclass(frozen=True)
class RoundReport:
    round_id: str
    period: str
    base_period: str
    growth_low: float
    growth_high: float
    applicable_period: str
    drivers: tuple[str, ...]
    contrary_signals: tuple[str, ...]
    threshold: float
    current_index: float
    expected_index: float
    current_sentiment: str
    expected_sentiment: str
    major_markets: tuple[MarketContribution, ...]
    high_value_goods: tuple[CommodityContribution, ...]
    audit: AuditTrail


@dataclass(frozen=True)
class IndexCheck:
    published: IndexPair | None
    recomputed: IndexPair
    matches: bool


def sentiment_of(index: float, threshold: float) -> str:
    return "好" if index >= threshold else "淡"


def build_report(
    round_: Round,
    frame: SampleFrame,
    indices: IndexPair,
    attribution: Attribution,
    scenario: ForecastScenario,
    responses: Iterable[SurveyResponse],
) -> RoundReport:
    if indices.enterprise_count < MIN_CELL:
        raise PrivacyError("样本量不足，报告无法匿名化")
    audit = AuditTrail(
        enterprise_count=indices.enterprise_count,
        unit_count=indices.unit_count,
        total_weight=indices.total_weight,
        current_weighted_score=indices.current_weighted_score,
        expected_weighted_score=indices.expected_weighted_score,
        current_distribution=_distribution(responses, frame, "current"),
        expected_distribution=_distribution(responses, frame, "expected"),
        base_total_value=attribution.base_total,
        current_total_value=attribution.current_total,
    )
    return RoundReport(
        round_id=round_.round_id,
        period=round_.period,
        base_period=round_.base_period,
        growth_low=scenario.growth_low,
        growth_high=scenario.growth_high,
        applicable_period=scenario.applicable_period,
        drivers=scenario.drivers,
        contrary_signals=scenario.contrary_signals,
        threshold=frame.threshold,
        current_index=round(indices.current_index, 2),
        expected_index=round(indices.expected_index, 2),
        current_sentiment=sentiment_of(indices.current_index, frame.threshold),
        expected_sentiment=sentiment_of(indices.expected_index, frame.threshold),
        major_markets=attribution.markets,
        high_value_goods=attribution.high_value_goods,
        audit=audit,
    )


def _distribution(
    responses: Iterable[SurveyResponse], frame: SampleFrame, kind: str
) -> tuple[DistributionCell, ...]:
    """按企业归并回答分布；不足 MIN_CELL 的类别合并，避免暴露单家企业。"""
    weights = {(entry.enterprise_id, entry.unit_id): entry.weight for entry in frame.entries}
    score_by_enterprise: dict[str, list[float]] = {}
    for response in responses:
        weight = weights.get((response.enterprise_id, response.unit_id))
        if weight is None:
            continue
        answer = response.current if kind == "current" else response.expected
        bucket = score_by_enterprise.setdefault(response.enterprise_id, [0.0, 0.0])
        bucket[0] += weight * answer.score
        bucket[1] += weight
    counts = {"上升": 0, "持平": 0, "下降": 0}
    for weighted, total in score_by_enterprise.values():
        score = weighted / total
        label = "上升" if score > 0.5 + 1e-9 else "下降" if score < 0.5 - 1e-9 else "持平"
        counts[label] += 1
    merged = sum(count for count in counts.values() if 0 < count < MIN_CELL)
    cells = tuple(
        DistributionCell(label, counts[label])
        for label in ("上升", "持平", "下降")
        if counts[label] >= MIN_CELL
    )
    if merged:
        cells += (DistributionCell(_MERGED_LABEL, merged),)
    return cells


class ReportService:
    """发布后的报告查询与复核。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def report(self, round_id: str) -> RoundReport:
        round_ = self._round(round_id)
        if round_.state is not RoundState.PUBLISHED:
            raise RoundStateError("报告尚未发布")
        report = self._store.stage_outputs[round_id]["report"]
        if report.audit.enterprise_count < MIN_CELL:
            raise PrivacyError("样本量不足，报告无法匿名化")
        return report

    def recompute_indices(self, round_id: str, actor: Actor) -> IndexCheck:
        """审核者用原始聚合重算指数，与发布值核对。"""
        if actor.role in (Role.ASSOCIATION, Role.ENTERPRISE):
            raise RoleError("该角色不得复核个体层面指数")
        frame = self._store.frames[round_id]
        recomputed = compute_indices(frame.entries, self._store.responses_of(round_id))
        published = self._store.stage_outputs.get(round_id, {}).get("index")
        matches = published is not None and _close(published, recomputed)
        return IndexCheck(published=published, recomputed=recomputed, matches=matches)

    def _round(self, round_id: str) -> Round:
        round_ = self._store.rounds.get(round_id)
        if round_ is None:
            raise RoundStateError("发布轮次不存在")
        return round_


def _close(left: IndexPair, right: IndexPair, tolerance: float = 1e-6) -> bool:
    return (
        abs(left.current_index - right.current_index) < tolerance
        and abs(left.expected_index - right.expected_index) < tolerance
        and abs(left.total_weight - right.total_weight) < tolerance
    )
