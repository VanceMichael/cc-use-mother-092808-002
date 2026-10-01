"""预测情景与调整：统计人员提出，另一角色确认推动因素、相反信号与适用期限。"""

from __future__ import annotations

from .errors import ConfirmationError, DomainError, RoleError, RoundStateError
from .models import Actor, AdjustmentStatus, ForecastScenario, Role, RoundState
from .store import Store


class ForecastService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def propose(
        self,
        round_id: str,
        scenario_id: str,
        growth_low: float,
        growth_high: float,
        drivers: tuple[str, ...] | list[str],
        contrary_signals: tuple[str, ...] | list[str],
        applicable_period: str,
        actor: Actor,
    ) -> ForecastScenario:
        if actor.role is not Role.RESEARCHER:
            raise RoleError("只有统计人员可以提出预测调整")
        round_ = self._store.rounds.get(round_id)
        if round_ is None:
            raise RoundStateError("发布轮次不存在")
        if round_.state not in (RoundState.COLLECTING, RoundState.CLOSED):
            raise RoundStateError("轮次当前状态不接受预测调整")
        if scenario_id in self._store.scenarios:
            raise DomainError("情景编号已存在")
        if not 0.0 <= growth_low <= growth_high:
            raise DomainError("增长区间无效")
        drivers = tuple(drivers)
        contrary_signals = tuple(contrary_signals)
        if not drivers:
            raise DomainError("推动因素不能为空")
        if not contrary_signals:
            raise DomainError("相反信号不能为空")
        if not applicable_period.strip():
            raise DomainError("适用期限不能为空")
        scenario = ForecastScenario(
            scenario_id=scenario_id,
            round_id=round_id,
            growth_low=float(growth_low),
            growth_high=float(growth_high),
            drivers=drivers,
            contrary_signals=contrary_signals,
            applicable_period=applicable_period,
            proposed_by=actor.actor_id,
        )
        self._store.scenarios[scenario_id] = scenario
        self._store.round_scenarios.setdefault(round_id, []).append(scenario_id)
        return scenario

    def confirm(self, scenario_id: str, actor: Actor) -> ForecastScenario:
        """另一角色确认：推动因素、相反信号与适用期限缺一不可。"""
        if actor.role is not Role.APPROVER:
            raise RoleError("只有报告审批人员可以确认预测调整")
        scenario = self._store.scenarios.get(scenario_id)
        if scenario is None:
            raise DomainError("预测情景不存在")
        if scenario.status is AdjustmentStatus.CONFIRMED:
            raise ConfirmationError("预测调整已确认")
        if scenario.proposed_by == actor.actor_id:
            raise ConfirmationError("提出人与确认人不得为同一人")
        if (
            not scenario.drivers
            or not scenario.contrary_signals
            or not scenario.applicable_period.strip()
        ):
            raise ConfirmationError("推动因素、相反信号与适用期限缺一不可")
        scenario.status = AdjustmentStatus.CONFIRMED
        scenario.confirmed_by = actor.actor_id
        return scenario


def effective_scenario(store: Store, round_id: str) -> ForecastScenario | None:
    """最近一个已确认的情景为有效情景；未确认的调整不生效。"""
    for scenario_id in reversed(store.round_scenarios.get(round_id, [])):
        scenario = store.scenarios[scenario_id]
        if scenario.status is AdjustmentStatus.CONFIRMED:
            return scenario
    return None
