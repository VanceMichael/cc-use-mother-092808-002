"""出口景气后端：按发布轮次组织调查、贸易批次与预测情景。

核心规则见 docs/backend-rules.md：

- 每期开始前锁定样本资格、权重与好淡分界；同一企业的业务单元按份额计权，
  拆分不得重复占权重。
- 数据按“水位”累积，已确认（发布）的水位不可变；补报只能进入尚未发布的水位。
- 贸易批次按批次编号幂等；单位或币种不一致的行停在核对区，不影响其他品类。
- 行业协会可提交依据，但看不到其他匿名答卷。
- 统计人员提出预测调整后，须由另一角色确认推动因素、相反信号与适用期限。
- 所有状态变更写入仅追加事件日志，发布中断后从已确认水位恢复。
- 报告查询把增长区间、置信指数、市场与高价值货品贡献联在一起，输出经过
  最小披露单元压制，审核者可复算总额而看不到单家企业。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# 角色
ROLE_STATISTICIAN = "statistician"  # 统计人员（贸易发展研究人员）
ROLE_REVIEWER = "reviewer"  # 数据复核人员
ROLE_APPROVER = "approver"  # 报告审批人员（审核者）
ROLE_ASSOCIATION = "association"  # 行业协会
ROLE_ENTERPRISE = "enterprise"  # 出口企业

PRIVILEGED_ROLES = frozenset({ROLE_STATISTICIAN, ROLE_REVIEWER})
REPORT_ROLES = frozenset(
    {ROLE_STATISTICIAN, ROLE_REVIEWER, ROLE_APPROVER, ROLE_ASSOCIATION}
)

# 规范计量
CANONICAL_WEIGHT_UNIT = "KGM"
DEFAULT_COUNT_UNIT = "PCE"
DEFAULT_CURRENCY = "HKD"

# 最小披露单元：分组内企业少于该数，或最大企业占比达到该阈值，则压制明细。
MIN_GROUP_ENTERPRISES = 3
DOMINANCE_THRESHOLD = 0.6
_SHARE_TOLERANCE = 1e-9

# 周期状态
ST_OPEN = "open"
ST_PUBLISHING = "publishing"
ST_PUBLISHED = "published"


class DomainError(ValueError):
    """领域规则被违反。"""


class AccessDeniedError(PermissionError):
    """角色无权执行该操作或查看该数据。"""


@dataclass(frozen=True)
class Actor:
    """操作人；enterprise_id/association_id 按角色填写。"""

    role: str
    actor_id: str
    enterprise_id: str | None = None
    association_id: str | None = None


@dataclass
class _FrameUnit:
    enterprise_id: str
    unit_id: str
    weight: float  # 企业层级固定权重
    share: float  # 该业务单元占企业权重的份额
    eligible: bool

    @property
    def effective_weight(self) -> float:
        return self.weight * self.share


@dataclass
class _Level:
    seq: int
    label: str
    confirmed_at: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


@dataclass
class _Cycle:
    cycle_id: str
    period: str
    currency: str
    started: bool = False
    threshold: float | None = None
    units: dict[str, _FrameUnit] | None = None
    levels: list[_Level] | None = None
    status: str = ST_OPEN
    proposed: dict[str, Any] | None = None
    confirmed_fc: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.units is None:
            self.units = {}
        if self.levels is None:
            self.levels = []


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Store:
    """事件溯源的出口景气存储。一个目录对应一份仅追加日志。"""

    def __init__(self, path: str | Path) -> None:
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._log = self._dir / "events.jsonl"
        self._lock = threading.RLock()
        self._seq = 0
        self.commodities: dict[str, dict[str, Any]] = {}
        self.cycles: dict[str, _Cycle] = {}
        # unit 答卷：(cycle, unit, level_seq) -> 事件
        self._responses: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._response_refs: dict[tuple[str, str], dict[str, Any]] = {}
        # 已接受贸易批次：batch_id -> 行
        self._batches: dict[str, dict[str, Any]] = {}
        # 核对区：hold_id -> 滞留行（resolved 后删除）
        self.holds: dict[str, dict[str, Any]] = {}
        # 导入幂等：import_ref -> (内容摘要, 结果摘要)
        self._imports: dict[str, tuple[str, dict[str, Any]]] = {}
        self.evidence: list[dict[str, Any]] = []
        if self._log.exists():
            self._replay()

    # ------------------------------------------------------------------ 事件

    def _emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        self._seq += 1
        event = {"seq": self._seq, "type": event_type, **payload}
        with self._lock:
            with self._log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
        self._apply(event)
        return event

    def _replay(self) -> None:
        for line in self._log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            self._seq += 1
            self._apply(json.loads(line))

    def _apply(self, e: dict[str, Any]) -> None:
        t = e["type"]
        if t == "commodity_registered":
            self.commodities[e["code"]] = {
                "code": e["code"],
                "name": e["name"],
                "parent": e.get("parent"),
                "high_value": e["high_value"],
                "count_unit": e["count_unit"],
            }
        elif t == "cycle_created":
            self.cycles[e["cycle_id"]] = _Cycle(
                cycle_id=e["cycle_id"], period=e["period"], currency=e["currency"]
            )
        elif t == "frame_locked":
            cycle = self.cycles[e["cycle_id"]]
            cycle.threshold = e["threshold"]
            cycle.units = {
                u["unit_id"]: _FrameUnit(**u) for u in e["units"]
            }
        elif t == "cycle_started":
            self.cycles[e["cycle_id"]].started = True
        elif t == "level_opened":
            self.cycles[e["cycle_id"]].levels.append(
                _Level(seq=e["seq_no"], label=e["label"])
            )
        elif t == "response_recorded":
            key = (e["cycle_id"], e["unit_id"], e["level_seq"])
            self._responses[key] = e
            self._response_refs[(e["cycle_id"], e["ref"])] = e
        elif t == "import_received":
            self._imports[e["import_ref"]] = (e["content_hash"], e["summary"])
        elif t == "batch_accepted":
            self._batches[e["batch_id"]] = e
        elif t == "batch_held":
            self.holds[e["hold_id"]] = e
        elif t == "hold_resolved":
            self.holds.pop(e["hold_id"], None)
        elif t == "evidence_submitted":
            self.evidence.append(e)
        elif t == "forecast_proposed":
            self.cycles[e["cycle_id"]].proposed = e
        elif t == "forecast_confirmed":
            cycle = self.cycles[e["cycle_id"]]
            if cycle.proposed is None:  # pragma: no cover - 提案先于确认写入
                raise DomainError("确认事件缺少对应提案")
            cycle.confirmed_fc = {**cycle.proposed, "confirmed_by": e["by"], "confirmed_at": e.get("at")}
        elif t == "publication_begun":
            self.cycles[e["cycle_id"]].status = ST_PUBLISHING
        elif t == "level_confirmed":
            for lv in self.cycles[e["cycle_id"]].levels:
                if lv.seq == e["seq_no"]:
                    lv.confirmed_at = e["at"]
        elif t == "publication_completed":
            self.cycles[e["cycle_id"]].status = ST_PUBLISHED
        else:  # pragma: no cover - 未知事件说明日志被外部破坏
            raise DomainError(f"未知事件类型：{t}")

    # -------------------------------------------------------------- 基础资料

    def register_commodity(
        self,
        actor: Actor,
        code: str,
        name: str,
        *,
        parent: str | None = None,
        high_value: bool = False,
        count_unit: str = DEFAULT_COUNT_UNIT,
    ) -> None:
        self._require_role(actor, ROLE_STATISTICIAN)
        if not code or not name:
            raise DomainError("商品编码与名称不能为空")
        if code in self.commodities:
            raise DomainError("商品编码已存在")
        if parent is not None and parent not in self.commodities:
            raise DomainError("父级商品不存在")
        self._emit(
            "commodity_registered",
            code=code,
            name=name,
            parent=parent,
            high_value=high_value,
            count_unit=count_unit,
            by=actor.actor_id,
        )

    # ------------------------------------------------------------- 轮次与锁框

    def create_cycle(
        self,
        actor: Actor,
        cycle_id: str,
        period: str,
        *,
        currency: str = DEFAULT_CURRENCY,
    ) -> None:
        self._require_role(actor, ROLE_STATISTICIAN)
        if not cycle_id or not period:
            raise DomainError("轮次编号与期次不能为空")
        if cycle_id in self.cycles:
            raise DomainError("发布轮次已存在")
        self._emit(
            "cycle_created",
            cycle_id=cycle_id,
            period=period,
            currency=currency,
            by=actor.actor_id,
        )

    def lock_frame(
        self,
        actor: Actor,
        cycle_id: str,
        threshold: float,
        entries: Iterable[dict[str, Any]],
    ) -> None:
        """期开始前锁定样本资格、权重与好淡分界。

        entries 每项含 enterprise_id/unit_id/weight/share/eligible；
        同一企业各业务单元份额之和不得超过 1，防止拆分重复计权。
        """
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        if cycle.started or cycle.threshold is not None:
            raise DomainError("样本框已锁定或轮次已开始，不能再改")
        if not isinstance(threshold, (int, float)) or not 0 < threshold < 100:
            raise DomainError("好淡分界必须落在 0 到 100 之间")

        rows: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        shares: dict[str, float] = {}
        for raw in entries:
            try:
                enterprise_id = str(raw["enterprise_id"])
                unit_id = str(raw["unit_id"])
                weight = float(raw["weight"])
                share = float(raw["share"])
                eligible = bool(raw["eligible"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError("样本框条目字段不完整或取值无效") from exc
            if not enterprise_id or not unit_id:
                raise DomainError("企业与业务单元标识不能为空")
            if unit_id in seen_units:
                raise DomainError(f"业务单元重复：{unit_id}")
            if weight <= 0:
                raise DomainError("企业权重必须为正")
            if not 0 < share <= 1:
                raise DomainError("业务单元份额必须落在 (0, 1]")
            seen_units.add(unit_id)
            shares[enterprise_id] = shares.get(enterprise_id, 0.0) + share
            if shares[enterprise_id] > 1 + _SHARE_TOLERANCE:
                raise DomainError(
                    f"企业 {enterprise_id} 的业务单元份额合计超过 1，"
                    "拆分不得重复计权"
                )
            rows.append(
                {
                    "enterprise_id": enterprise_id,
                    "unit_id": unit_id,
                    "weight": weight,
                    "share": share,
                    "eligible": eligible,
                }
            )
        if not rows:
            raise DomainError("锁定样本框不能为空")
        self._emit(
            "frame_locked",
            cycle_id=cycle_id,
            threshold=float(threshold),
            units=rows,
            by=actor.actor_id,
        )

    def start_cycle(self, actor: Actor, cycle_id: str) -> None:
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        if cycle.started:
            raise DomainError("轮次已经开始")
        if cycle.threshold is None:
            raise DomainError("期开始前必须先锁定样本资格、权重与好淡分界")
        self._emit("cycle_started", cycle_id=cycle_id, by=actor.actor_id)

    def open_level(self, actor: Actor, cycle_id: str, label: str = "") -> int:
        """打开一个尚未发布的新水位，返回水位序号。"""
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        self._require_editable(cycle)
        seq = len(cycle.levels) + 1
        self._emit(
            "level_opened",
            cycle_id=cycle_id,
            seq_no=seq,
            label=label or f"水位{seq}",
            by=actor.actor_id,
        )
        return seq

    # --------------------------------------------------------------- 企业答卷

    def submit_response(
        self,
        actor: Actor,
        cycle_id: str,
        unit_id: str,
        current: float,
        expected: float,
        *,
        ref: str,
        intended_level_seq: int | None = None,
        at: str = "",
    ) -> dict[str, Any]:
        """录入企业现状与预期回答。目标水位已发布时，补报转入新水位。"""
        cycle = self._cycle(cycle_id)
        self._require_editable(cycle)
        unit = self._eligible_unit(cycle, unit_id)
        if actor.role == ROLE_ENTERPRISE:
            if actor.enterprise_id != unit.enterprise_id:
                raise AccessDeniedError("只能替本企业提交答卷")
        elif actor.role != ROLE_STATISTICIAN:
            raise AccessDeniedError("该角色不能提交企业答卷")

        for value, label in ((current, "现状"), (expected, "预期")):
            if not isinstance(value, (int, float)) or not 0 <= value <= 100:
                raise DomainError(f"{label}回答必须落在 0 到 100 之间")
        if not ref:
            raise DomainError("答卷提交编号不能为空")

        target_seq, late = self._route_to_open_level(cycle, intended_level_seq)

        existing_ref = self._response_refs.get((cycle_id, ref))
        if existing_ref is not None:
            if (
                existing_ref["unit_id"] == unit_id
                and existing_ref["current"] == current
                and existing_ref["expected"] == expected
            ):
                return existing_ref  # 重送相同答卷：幂等，不产生第二份样本
            raise DomainError("提交编号已用于内容不同的答卷")
        if (cycle_id, unit_id, target_seq) in self._responses:
            raise DomainError("该业务单元在本水位已有答卷，修正请走下一水位")

        return self._emit(
            "response_recorded",
            cycle_id=cycle_id,
            level_seq=target_seq,
            unit_id=unit_id,
            enterprise_id=unit.enterprise_id,
            current=float(current),
            expected=float(expected),
            ref=ref,
            late=late,
            at=at,
            by=actor.actor_id,
        )

    def get_responses(self, actor: Actor, cycle_id: str) -> list[dict[str, Any]]:
        """答卷明细仅统计人员与复核人员可见，对协会匿名。"""
        if actor.role not in PRIVILEGED_ROLES:
            raise AccessDeniedError("其他答卷属于匿名信息")
        cycle = self._cycle(cycle_id)
        return [
            dict(e)
            for e in self._responses.values()
            if e["cycle_id"] == cycle.cycle_id
        ]

    # ------------------------------------------------------------- 贸易批次导入

    def import_batches(
        self,
        actor: Actor,
        cycle_id: str,
        import_ref: str,
        rows: Iterable[dict[str, Any]],
        *,
        level_seq: int | None = None,
        at: str = "",
    ) -> dict[str, Any]:
        """导入贸易批次。

        返回 {accepted, held, duplicated}。单位或币种不一致的行停在核对区，
        同批其他行正常入库；整份导入重放（相同 import_ref 与内容）幂等。
        """
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        self._require_editable(cycle)
        rows = [dict(r) for r in rows]
        if not rows:
            raise DomainError("导入内容为空")
        content_hash = _stable_hash(rows)
        if import_ref in self._imports:
            seen_hash, previous = self._imports[import_ref]
            if seen_hash == content_hash:
                return previous  # 重送相同批次：不产生第二份样本
            raise DomainError("导入编号已用于内容不同的数据")

        target_seq, _ = self._route_to_open_level(cycle, level_seq)
        accepted: list[str] = []
        held: list[dict[str, str]] = []
        for row in rows:
            batch_id = str(row.get("batch_id", "")).strip()
            if not batch_id:
                raise DomainError("贸易批次编号不能为空")
            problems = self._validate_batch_row(cycle, row)
            if batch_id in self._batches:
                prior = self._batches[batch_id]
                if self._row_fingerprint(row) == prior.get("row_hash"):
                    accepted.append(batch_id)  # 行级幂等
                    continue
                problems.append("批次编号已存在且内容不一致")
            if problems:
                hold_id = f"{import_ref}:{batch_id}"
                if hold_id in self.holds:
                    held.append({"hold_id": hold_id, "batch_id": batch_id})
                    continue
                self._emit(
                    "batch_held",
                    hold_id=hold_id,
                    import_ref=import_ref,
                    batch_id=batch_id,
                    cycle_id=cycle_id,
                    level_seq=target_seq,
                    reasons=problems,
                    row=row,
                    at=at,
                )
                held.append({"hold_id": hold_id, "batch_id": batch_id})
                continue
            self._emit(
                "batch_accepted",
                import_ref=import_ref,
                batch_id=batch_id,
                cycle_id=cycle_id,
                level_seq=target_seq,
                enterprise_id=str(row["enterprise_id"]),
                unit_id=row.get("unit_id"),
                commodity=row["commodity"],
                market=row["market"],
                qty=float(row["qty"]),
                prior_qty=_optional_float(row.get("prior_qty")),
                weight=float(row["weight"]),
                prior_weight=_optional_float(row.get("prior_weight")),
                value=float(row["value"]),
                prior_value=_optional_float(row.get("prior_value")),
                currency=row["currency"],
                row_hash=self._row_fingerprint(row),
                at=at,
            )
            accepted.append(batch_id)

        summary = {
            "accepted": accepted,
            "held": held,
            "duplicated": [],
            "level_seq": target_seq,
        }
        self._emit(
            "import_received",
            import_ref=import_ref,
            cycle_id=cycle_id,
            content_hash=content_hash,
            summary=summary,
            by=actor.actor_id,
        )
        return summary

    def held_items(self, actor: Actor, cycle_id: str) -> list[dict[str, Any]]:
        if actor.role not in PRIVILEGED_ROLES:
            raise AccessDeniedError("核对区仅统计与复核人员可见")
        return [dict(h) for h in self.holds.values() if h["cycle_id"] == cycle_id]

    def resolve_hold(
        self,
        actor: Actor,
        hold_id: str,
        corrected_row: dict[str, Any],
        *,
        at: str = "",
    ) -> str:
        """核对区更正：原水位已发布时，只能落入尚未发布的水位。"""
        self._require_role(actor, ROLE_STATISTICIAN)
        held = self.holds.get(hold_id)
        if held is None:
            raise DomainError("核对区没有这条滞留记录")
        cycle = self._cycle(held["cycle_id"])
        self._require_editable(cycle)
        row = dict(corrected_row)
        if str(row.get("batch_id", "")) != held["batch_id"]:
            raise DomainError("更正不能改变贸易批次编号")
        problems = self._validate_batch_row(cycle, row)
        if problems:
            raise DomainError("更正后仍未通过核对：" + "；".join(problems))
        if held["batch_id"] in self._batches:
            raise DomainError("批次编号已被占用")

        target_seq, late = self._route_to_open_level(cycle, held["level_seq"])
        self._emit(
            "hold_resolved",
            hold_id=hold_id,
            batch_id=held["batch_id"],
            level_seq=target_seq,
            late=late,
            at=at,
            by=actor.actor_id,
        )
        self._emit(
            "batch_accepted",
            import_ref=held["import_ref"],
            batch_id=held["batch_id"],
            cycle_id=cycle.cycle_id,
            level_seq=target_seq,
            enterprise_id=str(row["enterprise_id"]),
            unit_id=row.get("unit_id"),
            commodity=row["commodity"],
            market=row["market"],
            qty=float(row["qty"]),
            prior_qty=_optional_float(row.get("prior_qty")),
            weight=float(row["weight"]),
            prior_weight=_optional_float(row.get("prior_weight")),
            value=float(row["value"]),
            prior_value=_optional_float(row.get("prior_value")),
            currency=row["currency"],
            row_hash=self._row_fingerprint(row),
            resolved_from=hold_id,
            at=at,
        )
        return target_seq

    # --------------------------------------------------------------- 协会依据

    def submit_evidence(
        self,
        actor: Actor,
        cycle_id: str,
        scope_type: str,
        scope_code: str,
        summary: str,
        *,
        at: str = "",
    ) -> str:
        if actor.role != ROLE_ASSOCIATION:
            raise AccessDeniedError("只有行业协会可以提交依据")
        if actor.association_id is None:
            raise AccessDeniedError("协会身份缺失")
        self._cycle(cycle_id)
        if scope_type not in {"commodity", "market"} or not scope_code:
            raise DomainError("依据范围无效")
        if not summary.strip():
            raise DomainError("依据内容不能为空")
        evidence_id = f"EV-{len(self.evidence) + 1:04d}"
        self._emit(
            "evidence_submitted",
            evidence_id=evidence_id,
            cycle_id=cycle_id,
            association_id=actor.association_id,
            scope_type=scope_type,
            scope_code=scope_code,
            summary=summary.strip(),
            at=at,
        )
        return evidence_id

    def list_evidence(self, actor: Actor, cycle_id: str) -> list[dict[str, Any]]:
        """协会只能看到自己提交的依据，且看不到答卷。"""
        if actor.role == ROLE_ASSOCIATION:
            if actor.association_id is None:
                raise AccessDeniedError("协会身份缺失")
            return [
                dict(e)
                for e in self.evidence
                if e["cycle_id"] == cycle_id
                and e["association_id"] == actor.association_id
            ]
        if actor.role in PRIVILEGED_ROLES or actor.role == ROLE_APPROVER:
            return [dict(e) for e in self.evidence if e["cycle_id"] == cycle_id]
        raise AccessDeniedError("该角色不能查看协会依据")

    # ------------------------------------------------------------- 预测双确认

    def propose_forecast(
        self,
        actor: Actor,
        cycle_id: str,
        scenario: str,
        low: float,
        high: float,
        *,
        drivers: Iterable[str],
        opposing_signals: Iterable[str],
        applicable_from: str,
        applicable_to: str,
        evidence_refs: Iterable[str] = (),
        at: str = "",
    ) -> str:
        """统计人员提出预测调整（增长区间 + 三要素）。"""
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        if cycle.status != ST_OPEN:
            raise DomainError("发布进行中或已完成，不能再调整预测")
        drivers = [d for d in drivers if d and d.strip()]
        opposing = [s for s in opposing_signals if s and s.strip()]
        if not drivers:
            raise DomainError("必须列明推动因素")
        if not opposing:
            raise DomainError("必须列明相反信号（件数、重量、货值背离等）")
        if not applicable_from or not applicable_to or applicable_from > applicable_to:
            raise DomainError("适用期限无效")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            raise DomainError("增长区间必须为数值")
        if not 0 <= low <= high:
            raise DomainError("增长区间需满足 0 <= 下限 <= 上限")
        known = {e["evidence_id"] for e in self.evidence if e["cycle_id"] == cycle_id}
        for ref in evidence_refs:
            if ref not in known:
                raise DomainError(f"引用依据不存在：{ref}")
        adjustment_id = f"FC-{cycle_id}"
        self._emit(
            "forecast_proposed",
            adjustment_id=adjustment_id,
            cycle_id=cycle_id,
            scenario=scenario,
            low=float(low),
            high=float(high),
            drivers=drivers,
            opposing_signals=opposing,
            applicable_from=applicable_from,
            applicable_to=applicable_to,
            evidence_refs=list(evidence_refs),
            at=at,
            by=actor.actor_id,
        )
        return adjustment_id

    def confirm_forecast(
        self,
        actor: Actor,
        cycle_id: str,
        *,
        drivers: Iterable[str],
        opposing_signals: Iterable[str],
        applicable_from: str,
        applicable_to: str,
        at: str = "",
    ) -> None:
        """另一角色（复核人员）逐项确认推动因素、相反信号与适用期限。"""
        self._require_role(actor, ROLE_REVIEWER)
        cycle = self._cycle(cycle_id)
        proposed = cycle.proposed
        if proposed is None:
            raise DomainError("尚无待确认的预测调整")
        if actor.actor_id == proposed["by"]:
            raise AccessDeniedError("提出人与确认人不能是同一人")
        attested = {
            "drivers": [d for d in drivers if d and d.strip()],
            "opposing_signals": [s for s in opposing_signals if s and s.strip()],
            "applicable_from": applicable_from,
            "applicable_to": applicable_to,
        }
        expected = {
            "drivers": proposed["drivers"],
            "opposing_signals": proposed["opposing_signals"],
            "applicable_from": proposed["applicable_from"],
            "applicable_to": proposed["applicable_to"],
        }
        if attested != expected:
            raise DomainError("确认内容与提案不一致，须逐项确认三要素")
        self._emit(
            "forecast_confirmed",
            adjustment_id=proposed["adjustment_id"],
            cycle_id=cycle_id,
            drivers=attested["drivers"],
            opposing_signals=attested["opposing_signals"],
            applicable_from=attested["applicable_from"],
            applicable_to=attested["applicable_to"],
            at=at,
            by=actor.actor_id,
        )

    # ----------------------------------------------------------------- 发布

    def begin_publication(self, actor: Actor, cycle_id: str, *, at: str = "") -> None:
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        if cycle.status == ST_PUBLISHED:
            raise DomainError("轮次已发布")
        if cycle.status == ST_PUBLISHING:
            return  # 重入：发布进程可能在中断后被再次拉起
        if not cycle.levels:
            raise DomainError("没有可发布的水位")
        if cycle.confirmed_fc is None:
            raise DomainError("发布前预测调整必须经另一角色确认")
        self._emit("publication_begun", cycle_id=cycle_id, at=at, by=actor.actor_id)

    def confirm_level(
        self, actor: Actor, cycle_id: str, seq: int, *, at: str = ""
    ) -> None:
        """按顺序确认水位；已确认的水位重放为幂等。"""
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        if cycle.status == ST_PUBLISHED:
            raise DomainError("轮次已发布完成")
        if cycle.status != ST_PUBLISHING:
            raise DomainError("请先打开发布进程")
        level = self._level(cycle, seq)
        if level.confirmed:
            return
        for prior in cycle.levels:
            if prior.seq < seq and not prior.confirmed:
                raise DomainError("水位必须按顺序确认")
        self._emit(
            "level_confirmed", cycle_id=cycle_id, seq_no=seq, at=at, by=actor.actor_id
        )

    def complete_publication(
        self, actor: Actor, cycle_id: str, *, at: str = ""
    ) -> None:
        self._require_role(actor, ROLE_STATISTICIAN)
        cycle = self._cycle(cycle_id)
        if cycle.status == ST_PUBLISHED:
            return
        if cycle.status != ST_PUBLISHING:
            raise DomainError("发布进程尚未开始")
        if any(not lv.confirmed for lv in cycle.levels):
            raise DomainError("仍有未确认水位")
        if cycle.confirmed_fc is None:
            raise DomainError("预测调整尚未确认")
        self._emit(
            "publication_completed", cycle_id=cycle_id, at=at, by=actor.actor_id
        )

    def recovery_point(self, cycle_id: str) -> dict[str, Any]:
        """发布中断后的恢复点：从最后一个已确认水位继续。"""
        cycle = self._cycle(cycle_id)
        confirmed = [lv.seq for lv in cycle.levels if lv.confirmed]
        next_seq = next(
            (lv.seq for lv in cycle.levels if not lv.confirmed), None
        )
        return {
            "cycle_id": cycle_id,
            "status": cycle.status,
            "confirmed_levels": confirmed,
            "next_level_seq": next_seq,
        }

    # ----------------------------------------------------------------- 报告

    def build_report(self, actor: Actor, cycle_id: str) -> dict[str, Any]:
        """联表输出增长区间、置信指数、市场与高价值货品贡献。"""
        if actor.role not in REPORT_ROLES:
            raise AccessDeniedError("该角色不能查询报告")
        cycle = self._cycle(cycle_id)
        if actor.role == ROLE_ASSOCIATION and cycle.status != ST_PUBLISHED:
            raise AccessDeniedError("协会只能查看已发布报告")
        if cycle.confirmed_fc is None:
            raise DomainError("预测调整尚未确认，报告不可用")

        confirmed_seqs = {lv.seq for lv in cycle.levels if lv.confirmed}
        responses = [
            e
            for e in self._responses.values()
            if e["cycle_id"] == cycle_id and e["level_seq"] in confirmed_seqs
        ]
        batches = [
            b
            for b in self._batches.values()
            if b["cycle_id"] == cycle_id and b["level_seq"] in confirmed_seqs
        ]
        held_count = sum(1 for h in self.holds.values() if h["cycle_id"] == cycle_id)

        confidence = self._confidence(cycle, responses)
        total_value = sum(b["value"] for b in batches)
        total_prior = sum(b.get("prior_value") or 0.0 for b in batches)
        markets = self._group_contribution(
            batches, total_value, total_prior, key=lambda b: b["market"]
        )
        high_value_rows = [
            b for b in batches if self.commodities.get(b["commodity"], {}).get("high_value")
        ]
        goods = self._group_contribution(
            high_value_rows,
            total_value,
            total_prior,
            key=lambda b: b["commodity"],
            with_signals=True,
        )
        hv_value = sum(b["value"] for b in high_value_rows)
        hv_prior = sum(b.get("prior_value") or 0.0 for b in high_value_rows)

        fc = cycle.confirmed_fc
        evidence_map = {e["evidence_id"]: e["summary"] for e in self.evidence}
        return {
            "cycle_id": cycle_id,
            "period": cycle.period,
            "status": cycle.status,
            "growth_range": {
                "scenario": fc["scenario"],
                "low": fc["low"],
                "high": fc["high"],
                "applicable_from": fc["applicable_from"],
                "applicable_to": fc["applicable_to"],
                "proposed_by_role": ROLE_STATISTICIAN,
                "confirmed_by_role": ROLE_REVIEWER,
            },
            "confidence": confidence,
            "drivers": fc["drivers"],
            "opposing_signals": fc["opposing_signals"],
            "evidence": [
                {"evidence_id": ref, "summary": evidence_map.get(ref, "")}
                for ref in fc["evidence_refs"]
            ],
            "markets": markets,
            "high_value_goods": {
                "value": _round(hv_value),
                "share": _round(hv_value / total_value) if total_value else None,
                "contribution_points": _contribution(hv_value, hv_prior, total_prior),
                "goods": goods,
            },
            "recompute": {
                "currency": cycle.currency,
                "total_value": _round(total_value),
                "total_prior_value": _round(total_prior),
                "observed_growth_pct": _round(
                    100 * (total_value - total_prior) / total_prior
                )
                if total_prior
                else None,
                "included_levels": sorted(confirmed_seqs),
                "accepted_batch_count": len(batches),
                "quarantined_rows": held_count,
            },
        }

    # ------------------------------------------------------------- 计算与校验

    def _confidence(
        self, cycle: _Cycle, responses: list[dict[str, Any]]
    ) -> dict[str, Any]:
        latest: dict[str, dict[str, Any]] = {}
        for e in responses:
            old = latest.get(e["unit_id"])
            if old is None or e["level_seq"] > old["level_seq"]:
                latest[e["unit_id"]] = e
        good_w = cur_w = exp_w = total_w = 0.0
        enterprises: set[str] = set()
        for e in latest.values():
            unit = cycle.units[e["unit_id"]]
            w = unit.effective_weight
            total_w += w
            if e["current"] > cycle.threshold:
                cur_w += w
            if e["expected"] > cycle.threshold:
                exp_w += w
            if e["current"] > cycle.threshold and e["expected"] > cycle.threshold:
                good_w += w
            enterprises.add(unit.enterprise_id)
        return {
            "threshold": cycle.threshold,
            "respondent_units": len(latest),
            "respondent_enterprises": len(enterprises),
            "covered_weight": _round(total_w),
            "current_index": _round(100 * cur_w / total_w) if total_w else None,
            "expected_index": _round(100 * exp_w / total_w) if total_w else None,
        }

    def _group_contribution(
        self,
        batches: list[dict[str, Any]],
        total_value: float,
        total_prior: float,
        *,
        key,
        with_signals: bool = False,
    ) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for b in batches:
            g = groups.setdefault(
                key(b),
                {"enterprises": set(), "value": 0.0, "prior": 0.0,
                 "qty": 0.0, "prior_qty": 0.0, "weight": 0.0,
                 "prior_weight": 0.0},
            )
            g["enterprises"].add(b["enterprise_id"])
            g["value"] += b["value"]
            g["prior"] += b.get("prior_value") or 0.0
            g["qty"] += b["qty"]
            g["prior_qty"] += b.get("prior_qty") or 0.0
            g["weight"] += b["weight"]
            g["prior_weight"] += b.get("prior_weight") or 0.0

        visible: list[dict[str, Any]] = []
        hidden = {"enterprises": set(), "value": 0.0, "prior": 0.0}
        for name, g in groups.items():
            top_share = self._top_enterprise_share(batches, key, name)
            suppress = (
                len(g["enterprises"]) < MIN_GROUP_ENTERPRISES
                or top_share >= DOMINANCE_THRESHOLD
            )
            entry = {
                "name": name,
                "enterprise_count": len(g["enterprises"]),
                "value": _round(g["value"]),
                "share": _round(g["value"] / total_value) if total_value else None,
                "contribution_points": _contribution(g["value"], g["prior"], total_prior),
            }
            if with_signals:
                entry["signals"] = {
                    "qty_change_pct": _pct(g["qty"], g["prior_qty"]),
                    "weight_change_pct": _pct(g["weight"], g["prior_weight"]),
                    "value_change_pct": _pct(g["value"], g["prior"]),
                }
            if suppress:
                hidden["enterprises"] |= g["enterprises"]
                hidden["value"] += g["value"]
                hidden["prior"] += g["prior"]
            else:
                visible.append(entry)

        if hidden["value"]:
            visible.append(
                {
                    "name": "其他（最小披露合并）",
                    "enterprise_count": len(hidden["enterprises"]),
                    "value": _round(hidden["value"]),
                    "share": _round(hidden["value"] / total_value) if total_value else None,
                    "contribution_points": _contribution(
                        hidden["value"], hidden["prior"], total_prior
                    ),
                    "suppressed": True,
                }
            )
        return sorted(visible, key=lambda r: r["value"], reverse=True)

    @staticmethod
    def _top_enterprise_share(
        batches: list[dict[str, Any]], key, group_name: str
    ) -> float:
        by_ent: dict[str, float] = {}
        total = 0.0
        for b in batches:
            if key(b) != group_name:
                continue
            by_ent[b["enterprise_id"]] = by_ent.get(b["enterprise_id"], 0.0) + b["value"]
            total += b["value"]
        if total == 0:
            return 0.0
        return max(by_ent.values()) / total

    def _validate_batch_row(self, cycle: _Cycle, row: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        required = ("enterprise_id", "commodity", "market", "qty", "weight", "value", "currency")
        for field in required:
            if field not in row or row[field] in (None, ""):
                problems.append(f"缺少字段 {field}")
        if problems:
            return problems
        eligible = {u.enterprise_id for u in cycle.units.values() if u.eligible}
        if str(row["enterprise_id"]) not in eligible:
            problems.append("企业不在锁定的合格样本内")
        unit_id = row.get("unit_id")
        if unit_id is not None:
            unit = cycle.units.get(unit_id)
            if unit is None or not unit.eligible:
                problems.append("业务单元不在锁定的合格样本内")
            elif unit.enterprise_id != str(row["enterprise_id"]):
                problems.append("业务单元与企业不匹配")
        commodity = self.commodities.get(row["commodity"])
        if commodity is None:
            problems.append("商品层级中没有该商品")
        elif row.get("count_unit", commodity["count_unit"]) != commodity["count_unit"]:
            problems.append(
                f"计量单位不一致（应为 {commodity['count_unit']}），停在核对区"
            )
        if row.get("weight_unit", CANONICAL_WEIGHT_UNIT) != CANONICAL_WEIGHT_UNIT:
            problems.append("重量单位不一致（应为 KGM），停在核对区")
        if row["currency"] != cycle.currency:
            problems.append(
                f"币种不一致（应为 {cycle.currency}），停在核对区"
            )
        for field in ("qty", "weight", "value"):
            try:
                if float(row[field]) < 0:
                    problems.append(f"{field}不能为负")
            except (TypeError, ValueError):
                problems.append(f"{field}取值无效")
        for field in ("prior_qty", "prior_weight", "prior_value"):
            if row.get(field) is not None:
                try:
                    if float(row[field]) < 0:
                        problems.append(f"{field}不能为负")
                except (TypeError, ValueError):
                    problems.append(f"{field}取值无效")
        return problems

    @staticmethod
    def _row_fingerprint(row: dict[str, Any]) -> str:
        return _stable_dict_hash(row)

    def _route_to_open_level(
        self, cycle: _Cycle, intended: int | None
    ) -> tuple[int, bool]:
        """返回应进入的水位序号；intended 已发布则补报转入未发布水位。"""
        if not cycle.levels:
            raise DomainError("尚未打开任何水位")
        latest = cycle.levels[-1]
        if intended is None:
            if latest.confirmed:
                raise DomainError("没有尚未发布的水位，请先打开新水位")
            return latest.seq, False
        target = self._level(cycle, intended)
        if not target.confirmed:
            return target.seq, False
        if latest.confirmed:
            raise DomainError("补报只能进入尚未发布的水位，请先打开新水位")
        return latest.seq, True

    def _require_role(self, actor: Actor, role: str) -> None:
        if actor.role != role:
            raise AccessDeniedError(f"需要 {role} 角色")

    def _cycle(self, cycle_id: str) -> _Cycle:
        cycle = self.cycles.get(cycle_id)
        if cycle is None:
            raise DomainError("发布轮次不存在")
        return cycle

    @staticmethod
    def _require_editable(cycle: _Cycle) -> None:
        """已开始且未整体发布即可写入；发布进行中只能写未确认水位。"""
        if not cycle.started:
            raise DomainError("轮次尚未开始")
        if cycle.status == ST_PUBLISHED:
            raise DomainError("轮次已发布，数据不能再改")

    @staticmethod
    def _eligible_unit(cycle: _Cycle, unit_id: str) -> _FrameUnit:
        unit = cycle.units.get(unit_id) if cycle.units else None
        if unit is None:
            raise DomainError("业务单元不在锁定样本框内")
        if not unit.eligible:
            raise DomainError("业务单元样本资格不合格")
        return unit

    @staticmethod
    def _level(cycle: _Cycle, seq: int) -> _Level:
        for lv in cycle.levels:
            if lv.seq == seq:
                return lv
        raise DomainError("水位不存在")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _pct(current: float, prior: float) -> float | None:
    if prior <= 0:
        return None
    return _round(100 * (current - prior) / prior)


def _contribution(current: float, prior: float, total_prior: float) -> float | None:
    if total_prior <= 0:
        return None
    return _round(100 * (current - prior) / total_prior)


def _round(value: float) -> float:
    return round(float(value), 4)


def _stable_dict_hash(row: dict[str, Any]) -> str:
    return _stable_hash({k: row.get(k) for k in sorted(row)})
