"""发布轮次与样本框：每期开始前锁定资格、权重与好淡分界。"""

from __future__ import annotations

from .errors import DomainError, FrameLockedError, IneligibleSampleError, RoundStateError
from .models import FrameEntry, Round, RoundState, SampleFrame
from .store import Store


class RoundService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def create_round(self, round_id: str, period: str, base_period: str) -> Round:
        if round_id in self._store.rounds:
            raise DomainError("发布轮次已存在")
        if not period.strip() or not base_period.strip():
            raise DomainError("轮次期间不能为空")
        if self._store.round_for_period(period) is not None:
            raise DomainError("期间已被其他轮次占用")
        round_ = Round(round_id=round_id, period=period, base_period=base_period)
        self._store.rounds[round_id] = round_
        return round_

    def prepare_frame(
        self,
        round_id: str,
        entries: list[FrameEntry] | tuple[FrameEntry, ...],
        threshold: float,
    ) -> None:
        """登记样本框草稿；锁定后资格、权重与好淡分界不得再改。"""
        round_ = self._round(round_id)
        if round_.state is not RoundState.PREPARING:
            raise FrameLockedError("样本资格、权重与好淡分界已锁定")
        if not entries:
            raise DomainError("样本框不能为空")
        if not 0.0 < threshold < 100.0:
            raise DomainError("好淡分界必须落在0到100之间")
        seen_units: set[str] = set()
        for entry in entries:
            if not entry.enterprise_id.strip() or not entry.unit_id.strip():
                raise DomainError("样本条目缺少企业或业务单元")
            if entry.weight <= 0:
                raise DomainError("样本权重必须为正")
            if entry.unit_id in seen_units:
                raise DomainError("业务单元重复登记")
            seen_units.add(entry.unit_id)
        self._store.frame_drafts[round_id] = (tuple(entries), float(threshold))

    def lock_frame(self, round_id: str, allotments: dict[str, float]) -> SampleFrame:
        """锁定样本框：业务单元权重归一到企业配额，拆分不会重复计权。"""
        round_ = self._round(round_id)
        if round_.state is not RoundState.PREPARING:
            raise FrameLockedError("样本资格、权重与好淡分界已锁定")
        draft = self._store.frame_drafts.get(round_id)
        if draft is None:
            raise DomainError("请先登记样本框")
        entries, threshold = draft
        by_enterprise: dict[str, list[FrameEntry]] = {}
        for entry in entries:
            by_enterprise.setdefault(entry.enterprise_id, []).append(entry)
        if set(by_enterprise) != set(allotments):
            raise IneligibleSampleError("企业配额与样本名单不一致")
        normalized: list[FrameEntry] = []
        for enterprise_id, items in by_enterprise.items():
            quota = allotments[enterprise_id]
            if quota <= 0:
                raise DomainError("企业配额必须为正")
            raw_total = sum(item.weight for item in items)
            for item in items:
                normalized.append(
                    FrameEntry(enterprise_id, item.unit_id, item.weight * quota / raw_total)
                )
        frame = SampleFrame(
            round_id=round_id, threshold=threshold, entries=tuple(normalized), locked=True
        )
        self._store.frames[round_id] = frame
        round_.state = RoundState.COLLECTING
        return frame

    def close_round(self, round_id: str) -> None:
        round_ = self._round(round_id)
        if round_.state is not RoundState.COLLECTING:
            raise RoundStateError("只有收集中的轮次可以截止")
        round_.state = RoundState.CLOSED

    def _round(self, round_id: str) -> Round:
        round_ = self._store.rounds.get(round_id)
        if round_ is None:
            raise RoundStateError("发布轮次不存在")
        return round_
