"""贸易批次导入：单位或币种不一致停在核对区，重送批次不产生第二份样本。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

from .errors import (
    DomainError,
    DuplicateBatchError,
    ReconciliationError,
    RoleError,
    RoundStateError,
)
from .models import (
    AcceptedTrade,
    Actor,
    CommodityNode,
    ImportResult,
    ReconItem,
    ReconReason,
    ReconStatus,
    Role,
    RoundState,
    TradeBatch,
    TradeRecord,
)
from .store import Store


class IntakeService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def register_commodity(self, node: CommodityNode) -> None:
        if node.code in self._store.commodities:
            raise DomainError("商品编码重复")
        if node.parent_code is not None and node.parent_code not in self._store.commodities:
            raise DomainError("父级商品未登记")
        self._store.commodities[node.code] = node

    def import_batch(self, batch: TradeBatch) -> ImportResult:
        """导入批次；批次号幂等，重送返回首次结果，不产生第二份样本。"""
        fingerprint = _batch_fingerprint(batch)
        existing = self._store.batches.get(batch.batch_id)
        if existing is not None:
            if existing[1] != fingerprint:
                raise DuplicateBatchError("相同批次号携带不同内容")
            return replace(self._store.import_results[batch.batch_id], duplicate=True)
        round_ = self._store.round_for_period(batch.period)
        if round_ is None:
            raise RoundStateError("没有覆盖该期间的发布轮次")
        if round_.state in (RoundState.PUBLISHING, RoundState.PUBLISHED):
            raise RoundStateError("批次只能进入尚未发布的水位")
        if round_.state is RoundState.PREPARING:
            raise RoundStateError("轮次尚未开始收集")
        accepted = 0
        quarantined = 0
        for record in batch.records:
            reason = self._validate(record)
            if reason is None:
                self._store.trade_records.setdefault(batch.period, []).append(
                    AcceptedTrade(batch.batch_id, record)
                )
                accepted += 1
            else:
                recon_id = f"{batch.batch_id}:{record.line_no}"
                self._store.recon_items[recon_id] = ReconItem(
                    recon_id, batch.batch_id, batch.period, record, reason
                )
                quarantined += 1
        result = ImportResult(batch.batch_id, accepted, quarantined)
        self._store.batches[batch.batch_id] = (batch, fingerprint)
        self._store.import_results[batch.batch_id] = result
        return result

    def reconcile(
        self,
        recon_id: str,
        actor: Actor,
        *,
        approve: bool,
        unit: str | None = None,
        currency: str | None = None,
        commodity_code: str | None = None,
    ) -> ReconItem:
        """数据复核人员更正或退回核对区项目；放行前重新校验。"""
        if actor.role is not Role.REVIEWER:
            raise RoleError("只有数据复核人员可以处理核对区")
        item = self._store.recon_items.get(recon_id)
        if item is None:
            raise DomainError("核对项目不存在")
        if item.status is not ReconStatus.PENDING:
            raise ReconciliationError("核对项目已处理")
        if not approve:
            item.status = ReconStatus.REJECTED
            return item
        corrected = replace(
            item.record,
            unit=unit if unit is not None else item.record.unit,
            currency=currency if currency is not None else item.record.currency,
            commodity_code=commodity_code if commodity_code is not None else item.record.commodity_code,
        )
        if self._validate(corrected) is not None:
            raise ReconciliationError("更正后仍与商品层级不一致")
        round_ = self._store.round_for_period(item.period)
        if round_ is not None and round_.state in (RoundState.PUBLISHING, RoundState.PUBLISHED):
            raise RoundStateError("核对放行只能进入尚未发布的水位")
        self._store.trade_records.setdefault(item.period, []).append(
            AcceptedTrade(item.batch_id, corrected)
        )
        item.status = ReconStatus.RELEASED
        return item

    def _validate(self, record: TradeRecord) -> ReconReason | None:
        node = self._store.commodities.get(record.commodity_code)
        if node is None:
            return ReconReason.UNKNOWN_COMMODITY
        if record.unit != node.unit:
            return ReconReason.UNIT_MISMATCH
        if record.currency != node.currency:
            return ReconReason.CURRENCY_MISMATCH
        return None


def _batch_fingerprint(batch: TradeBatch) -> str:
    payload = json.dumps(
        {
            "batch_id": batch.batch_id,
            "period": batch.period,
            "records": [asdict(record) for record in sorted(batch.records, key=lambda r: r.line_no)],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
