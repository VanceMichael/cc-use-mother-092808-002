"""指数与归因的纯函数计算，发布与复核共用同一套算法。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import DomainError
from .models import AcceptedTrade, CommodityNode, FrameEntry, SurveyResponse


@dataclass(frozen=True)
class IndexPair:
    """现状与预期指数及其可复算的加总原料。"""

    current_index: float
    expected_index: float
    current_weighted_score: float
    expected_weighted_score: float
    total_weight: float
    enterprise_count: int
    unit_count: int


def compute_indices(
    entries: tuple[FrameEntry, ...], responses: Iterable[SurveyResponse]
) -> IndexPair:
    """扩散指数：企业配额在锁定时已归一，拆分业务单元不会重复计权。"""
    weights = {(entry.enterprise_id, entry.unit_id): entry.weight for entry in entries}
    current_score = 0.0
    expected_score = 0.0
    total_weight = 0.0
    enterprises: set[str] = set()
    units = 0
    for response in responses:
        weight = weights.get((response.enterprise_id, response.unit_id))
        if weight is None:
            continue
        current_score += weight * response.current.score
        expected_score += weight * response.expected.score
        total_weight += weight
        enterprises.add(response.enterprise_id)
        units += 1
    if total_weight <= 0:
        raise DomainError("无有效答卷，无法计算指数")
    return IndexPair(
        current_index=100.0 * current_score / total_weight,
        expected_index=100.0 * expected_score / total_weight,
        current_weighted_score=current_score,
        expected_weighted_score=expected_score,
        total_weight=total_weight,
        enterprise_count=len(enterprises),
        unit_count=units,
    )


@dataclass(frozen=True)
class MarketContribution:
    market: str
    base_value: float
    current_value: float
    share: float
    contribution_pp: float


@dataclass(frozen=True)
class CommodityContribution:
    code: str
    name: str
    base_value: float
    current_value: float
    contribution_pp: float


@dataclass(frozen=True)
class Attribution:
    """增长归因：各市场与高价值货品的贡献合计等于总增长。"""

    base_total: float
    current_total: float
    growth_pp: float
    markets: tuple[MarketContribution, ...]
    high_value_goods: tuple[CommodityContribution, ...]


def compute_attribution(
    current: Iterable[AcceptedTrade],
    base: Iterable[AcceptedTrade],
    commodities: dict[str, CommodityNode],
) -> Attribution:
    """把货值增长分解到市场与高价值货品（贡献单位：百分点）。"""
    base_total = sum(item.record.value for item in base)
    current_total = sum(item.record.value for item in current)
    if base_total <= 0:
        raise DomainError("基期货值缺失，无法归因")
    growth_pp = (current_total - base_total) / base_total * 100.0

    markets: dict[str, list[float]] = {}
    for item in base:
        markets.setdefault(item.record.market, [0.0, 0.0])[0] += item.record.value
    for item in current:
        markets.setdefault(item.record.market, [0.0, 0.0])[1] += item.record.value
    market_rows = tuple(
        sorted(
            (
                MarketContribution(
                    market=market,
                    base_value=base_value,
                    current_value=current_value,
                    share=current_value / current_total if current_total else 0.0,
                    contribution_pp=(current_value - base_value) / base_total * 100.0,
                )
                for market, (base_value, current_value) in markets.items()
            ),
            key=lambda row: row.current_value,
            reverse=True,
        )
    )

    goods: dict[str, list[float]] = {}
    for source, slot in ((base, 0), (current, 1)):
        for item in source:
            root = _high_value_root(item.record.commodity_code, commodities)
            if root is not None:
                goods.setdefault(root.code, [0.0, 0.0])[slot] += item.record.value
    goods_rows = tuple(
        sorted(
            (
                CommodityContribution(
                    code=code,
                    name=commodities[code].name,
                    base_value=base_value,
                    current_value=current_value,
                    contribution_pp=(current_value - base_value) / base_total * 100.0,
                )
                for code, (base_value, current_value) in goods.items()
            ),
            key=lambda row: row.contribution_pp,
            reverse=True,
        )
    )

    return Attribution(base_total, current_total, growth_pp, market_rows, goods_rows)


def _high_value_root(code: str, commodities: dict[str, CommodityNode]) -> CommodityNode | None:
    """沿商品层级向上找最近的高价值节点，作为归因归组。"""
    node = commodities.get(code)
    while node is not None:
        if node.high_value:
            return node
        node = commodities.get(node.parent_code) if node.parent_code else None
    return None
