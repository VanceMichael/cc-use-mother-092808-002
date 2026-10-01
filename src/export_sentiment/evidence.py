"""行业协会依据：可以提交，但看不到其他匿名答卷。"""

from __future__ import annotations

from .errors import DomainError, RoleError, RoundStateError
from .models import Actor, Evidence, Role, RoundState
from .store import Store


class EvidenceService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def submit(
        self, round_id: str, actor: Actor, evidence_id: str, summary: str, basis: str
    ) -> Evidence:
        if actor.role is not Role.ASSOCIATION:
            raise RoleError("只有行业协会可以提交依据")
        round_ = self._store.rounds.get(round_id)
        if round_ is None:
            raise RoundStateError("发布轮次不存在")
        if round_.state is RoundState.PUBLISHED:
            raise RoundStateError("轮次已发布，不再接受依据")
        if not summary.strip() or not basis.strip():
            raise DomainError("依据内容不能为空")
        if evidence_id in self._store.evidences:
            raise DomainError("依据编号已存在")
        evidence = Evidence(evidence_id, round_id, actor.actor_id, summary, basis)
        self._store.evidences[evidence_id] = evidence
        return evidence

    def list_for_round(self, round_id: str, actor: Actor) -> tuple[Evidence, ...]:
        """协会只能看到自己提交的依据，研究、复核与审批角色可见全部。"""
        items = [item for item in self._store.evidences.values() if item.round_id == round_id]
        if actor.role is Role.ASSOCIATION:
            return tuple(item for item in items if item.association_id == actor.actor_id)
        if actor.role is Role.ENTERPRISE:
            raise RoleError("出口企业不得查阅依据")
        return tuple(items)
