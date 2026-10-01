"""企业调查答卷：现状与预期；补报只能进入尚未发布的水位。"""

from __future__ import annotations

from .errors import DomainError, IneligibleSampleError, RoleError, RoundStateError
from .models import Actor, Answer, Role, Round, RoundState, SurveyResponse
from .store import Store


class SurveyService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def submit(
        self,
        round_id: str,
        enterprise_id: str,
        unit_id: str,
        current: Answer,
        expected: Answer,
    ) -> SurveyResponse:
        round_ = self._round(round_id)
        if round_.state is not RoundState.COLLECTING:
            raise RoundStateError("轮次不在收集阶段")
        return self._record(round_, enterprise_id, unit_id, current, expected, late=False)

    def submit_late(
        self,
        round_id: str,
        enterprise_id: str,
        unit_id: str,
        current: Answer,
        expected: Answer,
    ) -> SurveyResponse:
        """补报：只能进入尚未发布（未达发布水位）的轮次。"""
        round_ = self._round(round_id)
        if round_.state in (RoundState.PUBLISHING, RoundState.PUBLISHED):
            raise RoundStateError("补报数据只能进入尚未发布的水位")
        if round_.state is RoundState.PREPARING:
            raise RoundStateError("样本框尚未锁定")
        return self._record(
            round_, enterprise_id, unit_id, current, expected, late=round_.state is RoundState.CLOSED
        )

    def list_responses(self, round_id: str, actor: Actor) -> tuple[SurveyResponse, ...]:
        """行业协会与出口企业不得查看其他匿名答卷。"""
        if actor.role in (Role.ASSOCIATION, Role.ENTERPRISE):
            raise RoleError("该角色不得查看其他匿名答卷")
        return tuple(self._store.responses_of(round_id))

    def _record(
        self,
        round_: Round,
        enterprise_id: str,
        unit_id: str,
        current: Answer,
        expected: Answer,
        late: bool,
    ) -> SurveyResponse:
        frame = self._store.frames[round_.round_id]
        if frame.weight_of(enterprise_id, unit_id) is None:
            raise IneligibleSampleError("不在样本资格名单内")
        box = self._store.responses.setdefault(round_.round_id, {})
        existing = box.get((enterprise_id, unit_id))
        if existing is not None:
            if existing.current is current and existing.expected is expected:
                return existing
            raise DomainError("同一业务单元已提交不同答卷")
        response = SurveyResponse(round_.round_id, enterprise_id, unit_id, current, expected, late)
        box[(enterprise_id, unit_id)] = response
        return response

    def _round(self, round_id: str) -> Round:
        round_ = self._store.rounds.get(round_id)
        if round_ is None:
            raise RoundStateError("发布轮次不存在")
        return round_
