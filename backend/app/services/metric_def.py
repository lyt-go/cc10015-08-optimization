"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。

V008 联动：指标定义会读取洞察结论数据表里的指标字段。读取统一走
``conclusion_service.metric_fields``（按报告版本去重），并用
``find_metric_conflicts`` 拒绝「共享同一版本却口径不同」的结论——这类结论
既不能参与指标生效，也不能进入报告归档。
"""
from __future__ import annotations

from typing import Any

from app.services.insight_conclusion import PROCESS_VERSION, conclusion_service
from app.store import store

MODULE = "metric_def"
PROCESS_VERSION = PROCESS_VERSION
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def insight_metric_fields(
        self,
        *,
        report_code: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """读取洞察结论数据表里的指标字段（V008 调用链）。

        返回值同时带回口径冲突清单：只要同一报告版本下同一个指标编号出现
        多个口径，本次读取即视为不可用，调用方应当拒绝消费。
        """
        conflicts = conclusion_service.find_metric_conflicts(
            report_code=report_code, version=version,
        )
        return {
            # 冲突时不下发字段，避免口径不一致的结论被指标定义消费。
            "items": [] if conflicts else conclusion_service.metric_fields(
                report_code=report_code, version=version,
            ),
            "conflicts": conflicts,
            "ok": not conflicts,
            "rule_version": PROCESS_VERSION,
            "message": "结论口径一致，指标字段可消费" if not conflicts else "存在同版本不同口径的结论，已拒绝消费",
        }

    def _insight_conflicts(self, entry: dict[str, Any]) -> list[str]:
        """指标生效前核对结论表：同一（报告版本, 指标编号）口径必须一致。"""
        metric_id = str(entry.get("指标编号") or "").strip()
        caliber_version = str(entry.get("口径版本") or "").strip()
        if not metric_id:
            return []
        reasons: list[str] = []
        for conflict in conclusion_service.find_metric_conflicts():
            if str(conflict.get("指标编号")) != metric_id:
                continue
            if caliber_version and str(conflict.get("报告版本")) != caliber_version:
                continue
            reasons.append(
                f"{conflict.get('报告编号')}/{conflict.get('报告版本')} 的 {metric_id} "
                f"存在多个口径：{', '.join(conflict.get('指标口径') or [])}"
            )
        return reasons

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "标记生效":
            conflicts = self._insight_conflicts(entry)
            if conflicts:
                return None, "结论口径冲突，已拒绝指标生效：" + "；".join(conflicts)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"
