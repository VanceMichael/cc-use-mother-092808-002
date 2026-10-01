"""内存持久层：发布水位与各领域表，支撑崩溃恢复语义。

所有服务共享同一个 Store；进程重启后用同一个 Store 重建服务，
即可从已确认水位继续发布。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    AcceptedTrade,
    CommodityNode,
    Evidence,
    ForecastScenario,
    FrameEntry,
    ImportResult,
    PublishStage,
    ReconItem,
    Round,
    SampleFrame,
    SurveyResponse,
    TradeBatch,
)


@dataclass
class Store:
    """所有服务共享的持久状态。"""

    rounds: dict[str, Round] = field(default_factory=dict)
    frame_drafts: dict[str, tuple[tuple[FrameEntry, ...], float]] = field(default_factory=dict)
    frames: dict[str, SampleFrame] = field(default_factory=dict)
    responses: dict[str, dict[tuple[str, str], SurveyResponse]] = field(default_factory=dict)
    commodities: dict[str, CommodityNode] = field(default_factory=dict)
    batches: dict[str, tuple[TradeBatch, str]] = field(default_factory=dict)
    import_results: dict[str, ImportResult] = field(default_factory=dict)
    trade_records: dict[str, list[AcceptedTrade]] = field(default_factory=dict)
    recon_items: dict[str, ReconItem] = field(default_factory=dict)
    evidences: dict[str, Evidence] = field(default_factory=dict)
    scenarios: dict[str, ForecastScenario] = field(default_factory=dict)
    round_scenarios: dict[str, list[str]] = field(default_factory=dict)
    watermarks: dict[str, PublishStage] = field(default_factory=dict)
    stage_outputs: dict[str, dict[str, object]] = field(default_factory=dict)

    def round_for_period(self, period: str) -> Round | None:
        for round_ in self.rounds.values():
            if round_.period == period:
                return round_
        return None

    def responses_of(self, round_id: str) -> list[SurveyResponse]:
        return list(self.responses.get(round_id, {}).values())
