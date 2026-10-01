"""指数发布：按水位推进，意外终止后从已确认水位恢复。"""

from __future__ import annotations

import hashlib

from .computations import compute_attribution, compute_indices
from .errors import DomainError, PrivacyError, PublishInterrupted, RoundStateError
from .forecast import effective_scenario
from .models import PublishStage, Round, RoundState
from .reporting import MIN_CELL, build_report
from .store import Store

STAGE_ORDER = (PublishStage.FREEZE, PublishStage.INDEX, PublishStage.ATTRIBUTION, PublishStage.REPORT)


class PublishService:
    """逐阶段确认水位；fail_at 仅用于演练中断恢复。"""

    def __init__(self, store: Store, fail_at: PublishStage | None = None) -> None:
        self._store = store
        self._fail_at = fail_at

    def publish(self, round_id: str) -> None:
        round_ = self._round(round_id)
        if round_.state is RoundState.PUBLISHED:
            return
        if round_.state is RoundState.CLOSED:
            self._check_preconditions(round_)
            round_.state = RoundState.PUBLISHING
        elif round_.state is not RoundState.PUBLISHING:
            raise RoundStateError("轮次尚未截止，不能发布")
        done = self._store.watermarks.get(round_id)
        for stage in STAGE_ORDER:
            if done is not None and stage.value <= done.value:
                continue
            if stage is self._fail_at:
                raise PublishInterrupted(f"发布在{stage.name}阶段意外终止")
            self._run_stage(stage, round_)
            self._store.watermarks[round_id] = stage
        round_.state = RoundState.PUBLISHED

    def _check_preconditions(self, round_: Round) -> None:
        if round_.round_id not in self._store.frames:
            raise DomainError("样本框尚未锁定")
        responses = self._store.responses_of(round_.round_id)
        if not responses:
            raise DomainError("无有效答卷，不能发布")
        if len({response.enterprise_id for response in responses}) < MIN_CELL:
            raise PrivacyError("样本量不足，报告无法匿名化")
        if not self._store.trade_records.get(round_.period):
            raise DomainError("本期无已接受贸易批次")
        if not self._store.trade_records.get(round_.base_period):
            raise DomainError("基期无已接受贸易批次")
        if effective_scenario(self._store, round_.round_id) is None:
            raise DomainError("缺少已确认的预测情景")

    def _run_stage(self, stage: PublishStage, round_: Round) -> None:
        outputs = self._store.stage_outputs.setdefault(round_.round_id, {})
        if stage is PublishStage.FREEZE:
            responses = self._store.responses_of(round_.round_id)
            digest = hashlib.sha256(
                "|".join(
                    sorted(
                        f"{r.enterprise_id}:{r.unit_id}:{r.current.name}:{r.expected.name}"
                        for r in responses
                    )
                ).encode("utf-8")
            ).hexdigest()
            outputs["freeze"] = {
                "responses": len(responses),
                "digest": digest,
                "trade_records": len(self._store.trade_records.get(round_.period, [])),
            }
        elif stage is PublishStage.INDEX:
            frame = self._store.frames[round_.round_id]
            outputs["index"] = compute_indices(frame.entries, self._store.responses_of(round_.round_id))
        elif stage is PublishStage.ATTRIBUTION:
            outputs["attribution"] = compute_attribution(
                self._store.trade_records.get(round_.period, []),
                self._store.trade_records.get(round_.base_period, []),
                self._store.commodities,
            )
        elif stage is PublishStage.REPORT:
            outputs["report"] = build_report(
                round_,
                self._store.frames[round_.round_id],
                outputs["index"],
                outputs["attribution"],
                effective_scenario(self._store, round_.round_id),
                self._store.responses_of(round_.round_id),
            )

    def _round(self, round_id: str) -> Round:
        round_ = self._store.rounds.get(round_id)
        if round_ is None:
            raise RoundStateError("发布轮次不存在")
        return round_
