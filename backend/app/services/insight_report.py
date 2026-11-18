"""洞察报告业务规则：报告状态机、结论版本联动与归档事务。

规则要点（按 V008 调用链核对）：

1. **报告状态与结论版本同步推进。**
   ``开始生成`` 只从草稿出发（重试可从生成中出发），结论版本同步进入「生成中」；
   ``确认完成`` 先在结论表里把结论定稿，再让报告进入「已完成」；
   ``归档报告`` 先校验结论版本完整、口径一致，再同时归档报告与结论。
   任何一步失败都整体回滚，报告不会停在「报告已完成、结论还在生成中」的半成品态。

2. **归档失败保留最后一份完整结论。**
   每次确认完成（以及每次成功归档）都会保存结论表快照 ``_last_full_snapshot``；
   归档失败时按快照恢复，并把报告标记为「归档失败」等待重试。已归档章节永远
   不会退回草稿——重试只会基于新版本继续。

3. **读取按报告版本去重聚合。**
   工作台与详情页共用 ``_project``，只输出一条「当前数据版本」聚合结果；
   关键发现数量按 ``(指标编号, 指标口径, 关键发现)`` 去重后计数，重复生成不会重复累计。
   历史版本保留在 ``版本历史`` 中可回看。

4. **归档与重算并发按版本冲突处理。**
   重算回执先到、归档请求随后到达时，用 ``resolution`` 显式选择：
   * ``recompute_then_archive``（默认）：先完成重算再归档，归档内容包含重算后的新结论；
   * ``archive_then_recompute``：先归档旧结论，再把重算版本登记为「修订」。
"""

from __future__ import annotations

import time
from typing import Any

from app.services.insight_conclusion import (
    ACTIVE_STATUSES,
    conclusion_service,
    today,
    version_sort_key,
)
from app.store import store

MODULE = "insight_report"
REQUIRED_FIELDS = ["报告编号", "分析主题", "数据来源"]
STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
STATUS_LABELS = STATUS_ORDER + ["归档失败"]
ACTION_RULES = {"开始生成": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []
RESOLUTIONS = {
    "recompute_then_archive": "先完成重算再归档",
    "archive_then_recompute": "先归档旧结论再发布重算版本",
}
DEFAULT_RESOLUTION = "recompute_then_archive"
COMPLETE_CONCLUSION_STATUSES = ("已完成", "已归档", "修订")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class InsightReportService:
    """洞察报告服务：所有状态流转和结论写入都经过这里。"""

    def __init__(self) -> None:
        # 最后一版完整结论快照：报告编号 -> 结论表快照，用于归档失败回滚。
        self._last_full_snapshot: dict[str, list[dict[str, Any]]] = {}
        # 已到达但尚未随归档处理的重算回执：报告编号 -> 目标版本。
        self._pending_recompute: dict[str, str] = {}
        # 最近一次并发冲突的处理结论，供详情页追溯。
        self._last_resolution: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------------- 版本解析
    def _current_version(self, row: dict[str, Any]) -> str:
        report_code = _text(row.get("报告编号"))
        version = _text(row.get("当前版本"))
        if not version:
            versions = conclusion_service.versions(report_code)
            version = versions[-1] if versions else _text(row.get("报告版本")) or "V1"
        return version

    def _latest_version(self, row: dict[str, Any]) -> str:
        """报告名下最大的版本号（报告行 + 结论表一起比较）。"""
        report_code = _text(row.get("报告编号"))
        versions = set(conclusion_service.versions(report_code))
        for candidate in (_text(row.get("当前版本")), _text(row.get("报告版本"))):
            if candidate:
                versions.add(candidate)
        if not versions:
            return "V1"
        return sorted(versions, key=version_sort_key)[-1]

    @staticmethod
    def _increment(version: str) -> str:
        text = _text(version)
        if text.upper().startswith("V") and text[1:].isdigit():
            return f"V{int(text[1:]) + 1}"
        digits = "".join(char for char in text if char.isdigit())
        return f"V{int(digits) + 1}" if digits else "V1"

    # --------------------------------------------------------------- 读取投影
    def _project(self, row: dict[str, Any]) -> dict[str, Any]:
        """把报告行投影成看板行；工作台与详情页共用，保证业务数据一致。"""
        report_code = _text(row.get("报告编号"))
        version = self._current_version(row)
        aggregate = conclusion_service.aggregate_by_version(report_code, version)
        conflicts = conclusion_service.find_metric_conflicts(report_code=report_code, version=version)
        metric_values = [item.get("指标值") for item in aggregate["指标字段"] if item.get("指标值") is not None]

        projected = dict(row)
        projected["当前版本"] = version
        projected["报告版本"] = version
        projected["版本状态"] = aggregate["版本状态"]
        projected["关键发现"] = aggregate["关键发现"] or _text(row.get("关键发现"))
        projected["关键发现数量"] = aggregate["关键发现数量"]
        projected["结论数量"] = aggregate["结论数量"]
        projected["指标值"] = metric_values[0] if metric_values else None
        projected["指标字段"] = aggregate["指标字段"]
        projected["abnormal"] = bool(conflicts) or aggregate["版本状态"] in ("草稿", "生成中")
        projected["口径冲突"] = conflicts
        return projected

    def list_results(self) -> list[dict[str, Any]]:
        """工作台列表：每个报告只输出当前数据版本的一条聚合结果。"""
        return [self._project(row) for row in store.rows(MODULE)]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """详情页：与工作台同源的聚合结果，加上版本历史与冲突追溯。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        detail = self._project(row)
        report_code = _text(row.get("报告编号"))
        detail["版本历史"] = self._version_history(report_code)
        detail["结论"] = conclusion_service.aggregate_by_version(report_code, detail["当前版本"])["结论"]
        detail["待归档重算版本"] = self._pending_recompute.get(report_code)
        detail["最近冲突处理"] = self._last_resolution.get(report_code)
        return detail

    def _version_history(self, report_code: str) -> list[dict[str, Any]]:
        """历史版本可回看：每个版本一条去重聚合记录，不重复计算发现数量。"""
        return [
            {
                "报告版本": aggregate["报告版本"],
                "版本状态": aggregate["版本状态"],
                "关键发现数量": aggregate["关键发现数量"],
                "关键发现": aggregate["关键发现"],
                "结论": aggregate["结论"],
                "指标字段": aggregate["指标字段"],
            }
            for aggregate in reversed(conclusion_service.aggregate_report(report_code))
        ]

    # --------------------------------------------------------------- 结论生成
    def _build_conclusions(
        self,
        row: dict[str, Any],
        version: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """按报告字段生成一批结论；内容与版本绑定，重复生成结果完全一致。"""
        report_code = _text(row.get("报告编号"))
        topic = _text(params.get("结论主题")) or _text(row.get("分析主题")) or "洞察结论"
        source = _text(params.get("数据来源")) or _text(row.get("数据来源"))
        metrics = params.get("指标字段")
        if isinstance(metrics, list) and metrics:
            metric_items = [metric for metric in metrics if isinstance(metric, dict)]
        else:
            metric_items = [
                {"指标编号": f"{report_code}-M{index + 1}", "指标口径": "默认口径"}
                for index in range(3)
            ]

        findings = params.get("关键发现")
        if isinstance(findings, list):
            finding_list = [_text(item) for item in findings if _text(item)]
        elif _text(findings):
            finding_list = [_text(findings)]
        else:
            finding_list = []

        built: list[dict[str, Any]] = []
        for index, metric in enumerate(metric_items):
            finding = (
                finding_list[index % len(finding_list)]
                if finding_list
                else f"{topic} 发现{index + 1}"
            )
            built.append({
                "结论编号": f"{report_code}-{version}-C{index + 1}",
                "结论主题": topic,
                "关键发现": finding,
                "指标编号": _text(metric.get("指标编号")),
                "指标口径": _text(metric.get("指标口径")) or "默认口径",
                "数据来源": source,
            })
        return built

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """分析看板参数试算：不落结论表，避免试算污染正式结论。"""
        count = _as_int(params.get("样本范围", params.get("并行数量", "6")), 6)
        results: list[dict[str, Any]] = []
        for index in range(max(count, 0)):
            metric = round(0.62 + (index % 5) * 0.061, 3)
            baseline = round(0.58 + (index % 4) * 0.055, 3)
            results.append({
                "id": index + 1,
                "分组": f"分组{(index % 3) + 1}",
                "指标值": metric,
                "对比基准": baseline,
                "差异": round(metric - baseline, 3),
            })
        return results

    # --------------------------------------------------------------- 动作入口
    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        resolution: str | None = DEFAULT_RESOLUTION,
        params: dict[str, Any] | None = None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"洞察报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于洞察报告可执行范围"
        if action == "开始生成":
            return self._start_generation(row, params or {})
        if action == "确认完成":
            return self._confirm_generation(row, params or {})
        return self._archive(row, resolution or DEFAULT_RESOLUTION, params or {}, remark)

    # --------------------------------------------------------------- 开始生成
    def _start_generation(
        self,
        row: dict[str, Any],
        params: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        report_code = _text(row.get("报告编号"))
        status = _text(row.get("status"))
        if status not in STATUS_ORDER:
            return None, f"报告当前为「{status}」，请先重试归档或登记新报告"

        current = self._current_version(row)
        latest = self._latest_version(row)
        is_retry = status == "生成中" and bool(
            conclusion_service.list_rows(report_code=report_code, version=current)
        )

        current_rows = conclusion_service.list_rows(report_code=report_code, version=current)
        current_finalized = any(
            _text(item.get("status")) not in ACTIVE_STATUSES for item in current_rows
        )
        if status in ("已完成", "已归档") or current_finalized:
            # 重新生成必须落到新版本：历史/归档版本保持只读可追溯。
            target = self._increment(latest)
        elif is_retry:
            target = current
        else:
            target = current

        # 失败重试：先清理上次失败写入的草稿/生成中残留，再按版本覆盖写入，
        # 已归档、已完成的结论不参与清理，避免重复累计也不破坏历史版本。
        residue = conclusion_service.discard_residue(report_code)

        row["当前版本"] = target
        row["版本状态"] = "生成中"
        row["status"] = "生成中"
        row["pending"] = True
        row["abnormal"] = False
        row["生成开始时间"] = today()
        if is_retry:
            row["生成失败次数"] = _as_int(row.get("生成失败次数")) + 1

        conclusions = self._build_conclusions(row, target, params)
        try:
            conclusion_service.upsert_version(
                report_code, target, conclusions, status="生成中", replace=True,
            )
        except ValueError as error:
            return None, str(error)
        store.bump_version(MODULE)
        message = f"洞察报告已开始生成，结论版本 {target} 同步进入生成中"
        if is_retry:
            message = f"洞察报告重试生成，已清理 {len(residue)} 条残留结论，版本 {target} 重新生成"
        return row, message

    # --------------------------------------------------------------- 确认完成
    def _confirm_generation(
        self,
        row: dict[str, Any],
        params: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        status = _text(row.get("status"))
        if status == "已归档":
            return None, "报告已归档，结论只读；如需更新请开始生成新版本"
        if status not in ("生成中", "已完成"):
            return None, f"报告当前为「{status}」，请先开始生成"

        report_code = _text(row.get("报告编号"))
        version = self._current_version(row)
        snapshot = conclusion_service.snapshot(report_code)

        try:
            if params:
                conclusions = self._build_conclusions(row, version, params)
                conclusion_service.upsert_version(
                    report_code, version, conclusions,
                    status="已完成", replace=True, allow_complete=True,
                )
            else:
                if not conclusion_service.list_rows(report_code=report_code, version=version):
                    raise ValueError(f"报告版本 {version} 还没有生成结论，不能确认完成")
                conclusion_service.promote_version(report_code, version, "已完成")

            conflicts = conclusion_service.find_metric_conflicts(report_code=report_code, version=version)
            if conflicts:
                raise ValueError(
                    "结论口径不一致，已拒绝定稿：" + "；".join(
                        f"{item['指标编号']} → {', '.join(item['指标口径'])}" for item in conflicts
                    )
                )
            empty = [
                item for item in conclusion_service.list_rows(report_code=report_code, version=version)
                if _text(item.get("status")) not in COMPLETE_CONCLUSION_STATUSES
            ]
            if empty:
                raise ValueError(f"报告版本 {version} 仍有未定稿结论，不能确认完成")
        except ValueError as error:
            conclusion_service.restore(report_code, snapshot)
            return None, str(error)

        # 先落定结论版本，再同步报告状态：报告状态不会领先于结论。
        row["status"] = "已完成"
        row["pending"] = False
        row["abnormal"] = False
        row["当前版本"] = version
        row["版本状态"] = "已完成"
        row["完成时间"] = today()
        self._last_full_snapshot[report_code] = conclusion_service.snapshot(report_code)
        store.bump_version(MODULE)
        return row, f"洞察报告已完成，结论版本 {version} 同步定稿"

    # --------------------------------------------------------------- 归档
    def _archive(
        self,
        row: dict[str, Any],
        resolution: str,
        params: dict[str, Any],
        remark: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        if resolution not in RESOLUTIONS:
            return None, f"冲突处理方式「{resolution}」不受支持，可选：{'、'.join(RESOLUTIONS)}"

        report_code = _text(row.get("报告编号"))
        if _text(row.get("status")) == "归档失败":
            # 归档失败重试：先按最后完整结论快照恢复，再显式清理草稿/生成中的
            # 未清残留。清理只删未完成行，已归档章节不可能被退回草稿。
            snapshot = self._last_full_snapshot.get(report_code)
            if snapshot is None:
                return None, "归档失败但缺少最后完整结论快照，已拒绝继续归档"
            conclusion_service.restore(report_code, snapshot)
            removed = conclusion_service.discard_residue(report_code)
            row["归档重试清理数"] = len(removed)
            row["status"] = "已完成"
            row["pending"] = False

        pending_version = self._pending_recompute.get(report_code)
        current = self._current_version(row)
        if pending_version and pending_version != current:
            return self._resolve_conflict(row, pending_version, resolution, params, remark)

        status = _text(row.get("status"))
        if status != "已完成":
            return None, f"报告当前为「{status}」，请先确认完成后再归档"

        try:
            message = self._commit_archive(row, current, remark)
        except ValueError as error:
            return None, str(error)
        self._pending_recompute.pop(report_code, None)
        return row, message

    def _commit_archive(self, row: dict[str, Any], version: str, remark: str | None) -> str:
        """归档事务：校验 → 清理残留 → 同步归档，任何失败整体回滚。"""
        report_code = _text(row.get("报告编号"))
        snapshot = conclusion_service.snapshot(report_code)
        try:
            conclusions = conclusion_service.list_rows(report_code=report_code, version=version)
            if not conclusions:
                raise ValueError(f"报告版本 {version} 没有可归档的结论")
            unfinished = [item for item in conclusions if _text(item.get("status")) not in COMPLETE_CONCLUSION_STATUSES]
            if unfinished:
                raise ValueError(f"报告版本 {version} 仍处于生成中，不能归档半成品")

            conflicts = conclusion_service.find_metric_conflicts(report_code=report_code, version=version)
            if conflicts:
                raise ValueError(
                    "同一版本下指标口径不一致，拒绝归档：" + "；".join(
                        f"{item['指标编号']} → {', '.join(item['指标口径'])}" for item in conflicts
                    )
                )

            # 归档内容保留：先核对是否还有草稿/生成中的未清残留。
            # 有残留就整体回滚，不能把半成品一起归档；重试时再显式清理。
            residue = conclusion_service.find_residue(report_code, protected_versions=[version])
            if residue:
                raise ValueError(
                    "归档内容保留校验发现未清残留，已回滚本次归档："
                    + "、".join(sorted({
                        f"{item.get('报告版本')}/{item.get('结论编号')}" for item in residue
                    }))
                )

            conclusion_service.promote_version(report_code, version, "已归档")
            row["status"] = "已归档"
            row["pending"] = False
            row["abnormal"] = False
            row["当前版本"] = version
            row["版本状态"] = "已归档"
            row["归档时间"] = today()
            if remark:
                row["归档备注"] = remark
        except ValueError as error:
            # 关键：回滚到归档前的完整结论，报告标记为归档失败等待重试。
            conclusion_service.restore(report_code, snapshot)
            self._mark_archive_failed(row, version, str(error))
            raise ValueError(str(error)) from error

        self._last_full_snapshot[report_code] = conclusion_service.snapshot(report_code)
        store.bump_version(MODULE)
        return f"洞察报告 {version} 已归档，历史版本仍可回看"

    def _mark_archive_failed(self, row: dict[str, Any], version: str, reason: str) -> None:
        row["status"] = "归档失败"
        row["pending"] = True
        row["归档失败原因"] = reason
        row["归档失败版本"] = version
        store.bump_version(MODULE)

    def _rollback(
        self,
        report_code: str,
        snapshot: list[dict[str, Any]],
        row: dict[str, Any],
        status: str,
        row_snapshot: dict[str, Any] | None = None,
    ) -> None:
        conclusion_service.restore(report_code, snapshot)
        for key, value in (row_snapshot or {}).items():
            if value is None:
                row.pop(key, None)
            else:
                row[key] = value
        row["status"] = status
        row["pending"] = False
        row["abnormal"] = False
        store.bump_version(MODULE)

    # --------------------------------------------------------------- 并发冲突
    def receive_recompute(
        self,
        entry_id: int,
        version: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """接收结论重算回执。

        回执只落新版本，不直接改写当前版本；是否随归档发布由归档时的
        ``resolution`` 决定。若报告已归档，重算版本一律作为修订候选。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        report_code = _text(row.get("报告编号"))
        current = self._current_version(row)
        target = _text(version)
        current_rows = conclusion_service.list_rows(report_code=report_code, version=current)
        if not target:
            # 当前版本还没有结论时沿用当前版本，否则新开一个版本承接重算。
            target = self._increment(self._latest_version(row)) if current_rows else current
        elif target == current and current_rows:
            target = self._increment(self._latest_version(row))

        conclusions = self._build_conclusions(row, target, params or {})
        existing = conclusion_service.list_rows(report_code=report_code, version=target)
        if not existing:
            conclusion_service.upsert_version(
                report_code, target, conclusions, status="已完成", replace=True, allow_complete=True,
            )
        elif all(_text(item.get("status")) in ACTIVE_STATUSES for item in existing):
            conclusion_service.upsert_version(
                report_code, target, conclusions, status="已完成", replace=True, allow_complete=True,
            )

        self._pending_recompute[report_code] = target
        row["重算版本"] = target
        row["重算回执时间"] = today()
        row["pending"] = True
        store.bump_version(MODULE)
        return dict(row)

    def _resolve_conflict(
        self,
        row: dict[str, Any],
        target_version: str,
        resolution: str,
        params: dict[str, Any],
        remark: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """归档请求与重算回执撞车：按选定策略决定谁先落定。"""
        report_code = _text(row.get("报告编号"))
        current = self._current_version(row)
        status = _text(row.get("status"))
        if status != "已完成":
            return None, f"报告当前为「{status}」，请先确认完成后再归档"

        snapshot = conclusion_service.snapshot(report_code)
        row_snapshot = {key: row.get(key) for key in (
            "当前版本", "版本状态", "重算版本", "重算状态", "pending", "abnormal",
        )}

        if resolution == "recompute_then_archive":
            try:
                self._complete_recompute_for_archive(row, target_version, params)
                message = self._commit_archive(row, target_version, remark)
            except ValueError as error:
                # 重算版本没通过校验：回滚结论表与报告行，并丢弃这个不合格版本，
                # 避免它在历史版本里以「已完成」的身份留下半成品。
                self._rollback(report_code, snapshot, row, "已完成", row_snapshot)
                conclusion_service.discard_version(report_code, target_version)
                self._pending_recompute[report_code] = target_version
                row["pending"] = True
                return None, f"归档失败，已保留最后完整结论：{error}"
            self._pending_recompute.pop(report_code, None)
            row["重算版本"] = target_version
            self._last_resolution[report_code] = {
                "处理方式": resolution,
                "先后顺序": RESOLUTIONS[resolution],
                "归档版本": target_version,
                "重算版本": target_version,
                "被替换的版本": current,
                "说明": "归档内容包含重算后的新结论",
            }
            return row, (
                f"检测到归档与重算并发：先完成重算 {target_version} 再归档，"
                f"归档内容包含重算后的新结论"
            )

        try:
            message = self._commit_archive(row, current, remark)
        except ValueError as error:
            self._rollback(report_code, snapshot, row, "已完成", row_snapshot)
            return None, f"归档失败，已保留最后完整结论：{error}"

        try:
            self._publish_revision(row, target_version, params)
        except ValueError as error:
            # 旧结论已归档成功：只记重算版本待发布，不回退归档内容。
            self._pending_recompute[report_code] = target_version
            row["pending"] = True
            self._last_resolution[report_code] = {
                "处理方式": resolution,
                "先后顺序": RESOLUTIONS[resolution],
                "归档版本": current,
                "重算版本": target_version,
                "重算状态": "修订待发布",
                "失败原因": str(error),
            }
            store.bump_version(MODULE)
            return None, f"旧结论 {current} 已归档，但重算版本 {target_version} 修订发布失败：{error}"

        self._pending_recompute.pop(report_code, None)
        self._last_resolution[report_code] = {
            "处理方式": resolution,
            "先后顺序": RESOLUTIONS[resolution],
            "归档版本": current,
            "重算版本": target_version,
            "重算状态": "已登记为修订",
            "说明": "归档内容保留旧结论，重算版本列为修订",
        }
        return row, (
            f"检测到归档与重算并发：{current} 已归档并保留旧结论，"
            f"重算版本 {target_version} 已登记为修订"
        )

    def _complete_recompute_for_archive(
        self,
        row: dict[str, Any],
        version: str,
        params: dict[str, Any],
    ) -> None:
        """「先重算再归档」：确认重算版本结论完整且口径一致。"""
        report_code = _text(row.get("报告编号"))
        existing = conclusion_service.list_rows(report_code=report_code, version=version)
        if not existing:
            raise ValueError(f"重算回执缺少版本 {version} 的结论数据")
        if any(_text(item.get("status")) in ACTIVE_STATUSES for item in existing):
            conclusions = self._build_conclusions(row, version, params)
            conclusion_service.upsert_version(
                report_code, version, conclusions,
                status="已完成", replace=True, allow_complete=True,
            )
        conflicts = conclusion_service.find_metric_conflicts(report_code=report_code, version=version)
        if conflicts:
            raise ValueError(
                "重算结论口径不一致，拒绝归档：" + "；".join(
                    f"{item['指标编号']} → {', '.join(item['指标口径'])}" for item in conflicts
                )
            )
        row["当前版本"] = version

    def _publish_revision(self, row: dict[str, Any], version: str, params: dict[str, Any]) -> None:
        """「先归档旧结论」：把重算版本登记为修订，归档版本保持当前。"""
        report_code = _text(row.get("报告编号"))
        existing = conclusion_service.list_rows(report_code=report_code, version=version)
        if not existing:
            conclusions = self._build_conclusions(row, version, params)
            conclusion_service.upsert_version(
                report_code, version, conclusions, status="修订", replace=True, allow_complete=False,
            )
        conclusion_service.promote_version(report_code, version, "修订")
        row["重算版本"] = version
        row["重算状态"] = "已登记为修订"
        store.bump_version(MODULE)

    def archive_with_recompute(
        self,
        entry_id: int,
        resolution: str = DEFAULT_RESOLUTION,
        params: dict[str, Any] | None = None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """在已收到重算回执的前提下执行归档，按 resolution 处理版本冲突。"""
        return self.run_action(
            entry_id, "归档报告", resolution=resolution, params=params, remark=remark,
        )

    def clear_pending_recompute(self, entry_id: int) -> bool:
        row = store.find(MODULE, entry_id)
        if row is None:
            return False
        self._pending_recompute.pop(_text(row.get("报告编号")), None)
        store.bump_version(MODULE)
        return True

    # --------------------------------------------------------------- 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, missing
        report_code = _text(values.get("报告编号"))
        if any(_text(row.get("报告编号")) == report_code for row in store.rows(MODULE)):
            return None, [f"报告编号 {report_code} 已存在"]
        row: dict[str, Any] = {"id": store.next_id(MODULE)}
        for field in ["报告编号", "分析主题", "数据来源", "关键发现", "建议措施", "影响范围", "生成时间", "报告版本"]:
            row[field] = values.get(field, "")
        row["status"] = STATUS_ORDER[0]
        row["pending"] = True
        row["abnormal"] = False
        row["当前版本"] = _text(values.get("报告版本")) or "V1"
        row["版本状态"] = "草稿"
        row["created_at"] = time.time()
        store.rows(MODULE).append(row)
        store.bump_version(MODULE)
        return row, []
