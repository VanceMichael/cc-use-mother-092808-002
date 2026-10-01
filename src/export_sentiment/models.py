"""出口景气后端的领域实体与值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(Enum):
    """参与方角色。"""

    RESEARCHER = "贸易发展研究人员"
    ASSOCIATION = "行业协会"
    ENTERPRISE = "出口企业"
    REVIEWER = "数据复核人员"
    APPROVER = "报告审批人员"


@dataclass(frozen=True)
class Actor:
    """一次操作的执行者。"""

    actor_id: str
    role: Role


class RoundState(Enum):
    """发布轮次状态机：筹备→收集→截止→发布中→已发布。"""

    PREPARING = "筹备"
    COLLECTING = "收集"
    CLOSED = "截止"
    PUBLISHING = "发布中"
    PUBLISHED = "已发布"


@dataclass
class Round:
    """发布轮次：答卷、批次与预测情景都按轮次组织。"""

    round_id: str
    period: str
    base_period: str
    state: RoundState = RoundState.PREPARING


class Answer(Enum):
    """现状与预期回答。"""

    UP = "上升"
    FLAT = "持平"
    DOWN = "下降"

    @property
    def score(self) -> float:
        return _ANSWER_SCORE[self]


_ANSWER_SCORE = {Answer.UP: 1.0, Answer.FLAT: 0.5, Answer.DOWN: 0.0}


@dataclass(frozen=True)
class FrameEntry:
    """样本框条目：锁定后权重已按企业配额归一。"""

    enterprise_id: str
    unit_id: str
    weight: float


@dataclass(frozen=True)
class SampleFrame:
    """每期开始前锁定的样本资格、权重与好淡分界。"""

    round_id: str
    threshold: float
    entries: tuple[FrameEntry, ...]
    locked: bool

    def weight_of(self, enterprise_id: str, unit_id: str) -> float | None:
        for entry in self.entries:
            if entry.enterprise_id == enterprise_id and entry.unit_id == unit_id:
                return entry.weight
        return None

    def enterprise_weight(self, enterprise_id: str) -> float:
        return sum(entry.weight for entry in self.entries if entry.enterprise_id == enterprise_id)


@dataclass(frozen=True)
class SurveyResponse:
    """企业调查答卷：现状与预期。"""

    round_id: str
    enterprise_id: str
    unit_id: str
    current: Answer
    expected: Answer
    late: bool = False


@dataclass(frozen=True)
class CommodityNode:
    """商品层级节点，声明期望的计量单位与币种。"""

    code: str
    name: str
    parent_code: str | None
    unit: str
    currency: str
    high_value: bool = False


@dataclass(frozen=True)
class TradeRecord:
    """贸易批次中的一行。"""

    line_no: int
    commodity_code: str
    market: str
    quantity: float
    unit: str
    value: float
    currency: str


@dataclass(frozen=True)
class TradeBatch:
    """贸易批次：批次号是幂等键，重送不产生第二份样本。"""

    batch_id: str
    period: str
    records: tuple[TradeRecord, ...]


@dataclass(frozen=True)
class AcceptedTrade:
    """已通过核对、进入聚合的贸易记录。"""

    batch_id: str
    record: TradeRecord


class ReconReason(Enum):
    """批次行被拦入核对区的原因。"""

    UNIT_MISMATCH = "单位不一致"
    CURRENCY_MISMATCH = "币种不一致"
    UNKNOWN_COMMODITY = "商品未登记"


class ReconStatus(Enum):
    PENDING = "待核对"
    RELEASED = "已放行"
    REJECTED = "已退回"


@dataclass
class ReconItem:
    """核对区项目：单位或币种不一致的批次行在此停留，不影响其他品类。"""

    recon_id: str
    batch_id: str
    period: str
    record: TradeRecord
    reason: ReconReason
    status: ReconStatus = ReconStatus.PENDING


@dataclass(frozen=True)
class ImportResult:
    batch_id: str
    accepted: int
    quarantined: int
    duplicate: bool = False


class AdjustmentStatus(Enum):
    PROPOSED = "已提出"
    CONFIRMED = "已确认"


@dataclass
class ForecastScenario:
    """预测情景：统计人员提出，另一角色确认推动因素、相反信号与适用期限。"""

    scenario_id: str
    round_id: str
    growth_low: float
    growth_high: float
    drivers: tuple[str, ...]
    contrary_signals: tuple[str, ...]
    applicable_period: str
    proposed_by: str
    status: AdjustmentStatus = AdjustmentStatus.PROPOSED
    confirmed_by: str | None = None


@dataclass(frozen=True)
class Evidence:
    """行业协会提交的依据。"""

    evidence_id: str
    round_id: str
    association_id: str
    summary: str
    basis: str


class PublishStage(Enum):
    """发布水位：按顺序确认，意外终止后从已确认水位恢复。"""

    FREEZE = 1
    INDEX = 2
    ATTRIBUTION = 3
    REPORT = 4
