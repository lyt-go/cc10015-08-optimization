"""洞察结论数据表服务。

洞察报告每次生成都会往结论表里写一批「结论」。历史上这里只追加不清洗：
生成失败重试时同一批结论会被重复写入，读取侧又逐行计数，关键发现数量就被
重复累计。本服务统一收口结论表的读写：

* 写入：按 ``(报告编号, 报告版本)`` 覆盖式 upsert，重试不会产生第二份数据；
* 读取：按版本去重聚合。先按结论编号取最新一条，再按
  ``(指标编号, 关键发现)`` 去掉内容重复项，最后才计数；
* 校验：同一报告版本下，同一个指标编号只允许一个口径，供指标定义模块
  （V008 调用链）读取并拒绝口径冲突的结论；
* 归档：已归档的结论行是只读的，任何重算都必须落到新版本，不能改写归档内容。

所有结构都保持在内存仓库里，方便与现有模块一致地演示状态流转。
"""
from __future__ import annotations

import copy
import hashlib
import time
from datetime import datetime
from typing import Any, Iterable

from app.store import store

MODULE = "insight_conclusion"
PROCESS_VERSION = "V008"
ACTIVE_STATUSES = ("草稿", "生成中")
COMPLETE_STATUSES = ("已完成", "已归档", "修订")
ALL_STATUSES = ["草稿", "生成中", "已完成", "已归档", "修订"]
CONCLUSION_FIELDS = [
    "报告编号", "报告版本", "结论编号", "结论主题", "关键发现",
    "指标编号", "指标口径", "指标值", "数据来源", "生成时间",
]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stable_number(*parts: Any, low: float, high: float) -> float:
    """把业务键映射到稳定数值，保证工作台和详情页读到同一份数据。"""
    seed = "|".join(_text(part) for part in parts)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    ratio = int(digest[:12], 16) / float(0xFFFFFFFFFFFF)
    return round(low + (high - low) * ratio, 3)


def version_sort_key(version: str) -> tuple[int, str]:
    text = _text(version).upper()
    if text.startswith("V") and text[1:].isdigit():
        return (int(text[1:]), text)
    return (10_000, text)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class InsightConclusionService:
    """洞察结论数据表的唯一读写入口。"""

    # ---------------------------------------------------------------- 基础读取
    def rows(self) -> list[dict[str, Any]]:
        return store.rows(MODULE)

    def list_rows(
        self,
        *,
        report_code: str | None = None,
        version: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.rows()
        if report_code is not None:
            rows = [row for row in rows if _text(row.get("报告编号")) == _text(report_code)]
        if version is not None:
            rows = [row for row in rows if _text(row.get("报告版本")) == _text(version)]
        if statuses is not None:
            allowed = set(statuses)
            rows = [row for row in rows if _text(row.get("status")) in allowed]
        return rows

    def versions(self, report_code: str) -> list[str]:
        return sorted(
            {_text(row.get("报告版本")) for row in self.list_rows(report_code=report_code) if _text(row.get("报告版本"))},
            key=version_sort_key,
        )

    # ---------------------------------------------------------------- 去重聚合
    def _dedupe(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按结论编号取最新，再按 ``(指标编号, 指标口径, 关键发现)`` 去掉重复内容。"""
        latest_by_id: dict[str, dict[str, Any]] = {}
        unordered: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: _as_int(item.get("id"))):
            conclusion_id = _text(row.get("结论编号"))
            if conclusion_id:
                latest_by_id[conclusion_id] = row
            else:
                unordered.append(row)

        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        candidates = list(latest_by_id.values()) + unordered
        for row in sorted(candidates, key=lambda item: _as_int(item.get("id"))):
            key = (
                _text(row.get("指标编号")).casefold(),
                _text(row.get("指标口径")).casefold(),
                _text(row.get("关键发现")).casefold(),
            )
            # 同一内容保留最后写入的一条（重试后内容相同，不会重复计数）。
            # 口径不同视为不同结论，不能在这里被合掉，否则会掩盖口径冲突。
            deduped[key] = row
        return list(deduped.values())

    def version_status(self, report_code: str, version: str) -> str:
        rows = self.list_rows(report_code=report_code, version=version)
        if not rows:
            return "草稿"
        statuses = {_text(row.get("status")) or "草稿" for row in rows}
        if statuses <= {"已归档"}:
            return "已归档"
        if "生成中" in statuses:
            return "生成中"
        if "草稿" in statuses:
            return "草稿"
        if "已完成" in statuses:
            return "已完成"
        if "修订" in statuses:
            return "修订"
        return "草稿"

    def aggregate_by_version(self, report_code: str, version: str) -> dict[str, Any]:
        report_code = _text(report_code)
        version = _text(version)
        raw_rows = self.list_rows(report_code=report_code, version=version)
        rows = self._dedupe(raw_rows)
        findings = [_text(row.get("关键发现")) for row in rows if _text(row.get("关键发现"))]
        seen_metrics: set[tuple[str, str]] = set()
        metrics: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: _as_int(item.get("id"))):
            metric_id = _text(row.get("指标编号"))
            caliber = _text(row.get("指标口径"))
            key = (metric_id, caliber)
            if metric_id and key not in seen_metrics:
                seen_metrics.add(key)
                metrics.append({
                    "指标编号": metric_id,
                    "指标口径": caliber,
                    "指标值": row.get("指标值"),
                })
        return {
            "报告编号": report_code,
            "报告版本": version,
            "结论数量": len(rows),
            "关键发现数量": len(rows),
            "关键发现": "；".join(findings),
            "结论": [dict(row) for row in rows],
            "指标字段": metrics,
            "版本状态": self.version_status(report_code, version),
            "原始行数": len(raw_rows),
            "去重行数": len(rows),
        }

    def aggregate_report(self, report_code: str) -> list[dict[str, Any]]:
        return [
            self.aggregate_by_version(report_code, version)
            for version in self.versions(report_code)
        ]

    # ---------------------------------------------------------------- 口径校验
    def find_metric_conflicts(
        self,
        *,
        report_code: str | None = None,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        """同一报告版本下，同一指标编号出现多个口径即为冲突。

        这是指标定义模块（V008）读取结论表指标字段时使用的判定规则：
        共享版本却口径不同的结论必须被拒绝，不能进入报告归档。
        """
        codes = (
            [_text(report_code)]
            if report_code is not None
            else sorted({_text(row.get("报告编号")) for row in self.rows() if _text(row.get("报告编号"))})
        )
        conflicts: list[dict[str, Any]] = []
        for code in codes:
            versions = [version] if version is not None else self.versions(code)
            for item_version in versions:
                aggregate = self.aggregate_by_version(code, item_version)
                grouped: dict[str, set[str]] = {}
                for row in aggregate["结论"]:
                    metric_id = _text(row.get("指标编号"))
                    if not metric_id:
                        continue
                    caliber = _text(row.get("指标口径")) or "<空口径>"
                    grouped.setdefault(metric_id, set()).add(caliber)
                for metric_id, calibers in grouped.items():
                    if len(calibers) > 1:
                        conflicts.append({
                            "报告编号": code,
                            "报告版本": item_version,
                            "指标编号": metric_id,
                            "指标口径": sorted(calibers),
                            "原因": "同一版本下指标口径不一致",
                            "规则版本": PROCESS_VERSION,
                        })
        return conflicts

    def metric_fields(
        self,
        *,
        report_code: str | None = None,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        """供指标定义模块读取的指标字段（已按版本去重）。"""
        codes = (
            [_text(report_code)]
            if report_code is not None
            else sorted({_text(row.get("报告编号")) for row in self.rows() if _text(row.get("报告编号"))})
        )
        fields: list[dict[str, Any]] = []
        for code in codes:
            versions = [version] if version is not None else self.versions(code)
            for item_version in versions:
                for metric in self.aggregate_by_version(code, item_version)["指标字段"]:
                    fields.append({
                        "报告编号": code,
                        "报告版本": item_version,
                        "指标编号": metric["指标编号"],
                        "指标口径": metric["指标口径"],
                        "指标值": metric["指标值"],
                        "规则版本": PROCESS_VERSION,
                    })
        return fields

    # ---------------------------------------------------------------- 写入
    def upsert_version(
        self,
        report_code: str,
        version: str,
        conclusions: Iterable[dict[str, Any]],
        *,
        status: str = "草稿",
        replace: bool = True,
        allow_complete: bool = False,
    ) -> list[dict[str, Any]]:
        """覆盖式写入某报告版本的全部结论。

        - 归档版本只读，不允许覆盖；
        - 已完成版本默认也视为只读，只有显式的 ``allow_complete``（例如
          「先重算再归档」策略）才允许替换；
        - ``replace`` 时只清理可写的旧行，已归档行保持原样。
        """
        report_code = _text(report_code)
        version = _text(version)
        status = _text(status) or "草稿"
        existing = self.list_rows(report_code=report_code, version=version)
        if any(_text(row.get("status")) == "已归档" for row in existing):
            raise ValueError(f"报告版本 {version} 已归档，结论只读，请基于新版本重算")
        if not allow_complete:
            if any(_text(row.get("status")) in ("已完成", "修订") for row in existing) and status in ACTIVE_STATUSES:
                raise ValueError(f"报告版本 {version} 已定稿，请创建新版本后再生成")

        table = self.rows()
        if replace:
            def writable(row: dict[str, Any]) -> bool:
                if _text(row.get("报告版本")) != version or _text(row.get("报告编号")) != report_code:
                    return False
                row_status = _text(row.get("status"))
                if row_status == "已归档":
                    return False
                if allow_complete:
                    return True
                return row_status in ACTIVE_STATUSES

            table[:] = [row for row in table if not writable(row)]

        created: list[dict[str, Any]] = []
        now = today()
        for index, conclusion in enumerate(conclusions):
            entry = {
                "id": store.next_id(MODULE),
                "报告编号": report_code,
                "报告版本": version,
                "结论编号": _text(conclusion.get("结论编号")) or f"{report_code}-{version}-C{index + 1}",
                "结论主题": _text(conclusion.get("结论主题")) or _text(conclusion.get("关键发现")),
                "关键发现": _text(conclusion.get("关键发现")),
                "指标编号": _text(conclusion.get("指标编号")),
                "指标口径": _text(conclusion.get("指标口径")),
                "指标值": conclusion.get("指标值", _stable_number(report_code, version, index, low=0.05, high=0.35)),
                "数据来源": _text(conclusion.get("数据来源")),
                "status": _text(conclusion.get("status")) or status,
                "archived": _text(conclusion.get("status") or status) == "已归档",
                "生成时间": _text(conclusion.get("生成时间")) or now,
            }
            entry["created_at"] = time.time()
            table.append(entry)
            created.append(entry)
        store.bump_version(MODULE)
        return created

    def promote_version(self, report_code: str, version: str, status: str) -> list[dict[str, Any]]:
        """把某版本的结论整体推进到目标状态；归档行永远不会被退回草稿。"""
        promoted: list[dict[str, Any]] = []
        for row in self.list_rows(report_code=report_code, version=version):
            current = _text(row.get("status"))
            if current == "已归档" and status != "已归档":
                # 归档内容不可回退，保证重试/恢复不会把已归档章节退回中间态。
                continue
            row["status"] = status
            row["archived"] = status == "已归档"
            if status == "已归档":
                row["归档时间"] = today()
            promoted.append(row)
        if promoted:
            store.bump_version(MODULE)
        return promoted

    def find_residue(
        self,
        report_code: str,
        *,
        protected_versions: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """找出归档内容保留阶段不允许带进归档的残留行。

        残留 = 同报告下仍处于 ``草稿/生成中`` 的结论，且不属于本次受保护的
        归档版本。已归档、已完成、修订行一律不算残留，因此归档重试不会把
        已归档章节退回草稿。
        """
        protected = {_text(item) for item in protected_versions if _text(item)}
        return [
            dict(row)
            for row in self.list_rows(report_code=report_code)
            if _text(row.get("status")) in ACTIVE_STATUSES
            and _text(row.get("报告版本")) not in protected
        ]

    def discard_residue(
        self,
        report_code: str,
        *,
        protected_versions: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """显式清理残留（只删草稿/生成中行）。

        归档失败重试时调用：清掉未完成残留好让归档可以继续，但绝不触碰
        已归档/已完成/修订行。
        """
        report_code = _text(report_code)
        protected = {_text(item) for item in protected_versions if _text(item)}
        removed: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for row in self.rows():
            same_report = _text(row.get("报告编号")) == report_code
            removable = (
                same_report
                and _text(row.get("status")) in ACTIVE_STATUSES
                and _text(row.get("报告版本")) not in protected
            )
            if removable:
                removed.append(dict(row))
            else:
                kept.append(row)
        if removed:
            self.rows()[:] = kept
            store.bump_version(MODULE)
        return removed

    def discard_version(self, report_code: str, version: str) -> list[dict[str, Any]]:
        """丢弃未通过校验的版本（只允许草稿/生成中/已完成/修订）。

        归档校验失败的重算版本会走这里：它既不能进归档，也不该以「已完成」
        的身份留在历史里，避免历史版本可回看时被半成品误导。已归档行拒绝删除。
        """
        report_code = _text(report_code)
        version = _text(version)
        removed: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for row in self.rows():
            matches = (
                _text(row.get("报告编号")) == report_code
                and _text(row.get("报告版本")) == version
            )
            if matches and _text(row.get("status")) == "已归档":
                raise ValueError(f"报告版本 {version} 含已归档结论，不允许丢弃")
            if matches:
                removed.append(dict(row))
            else:
                kept.append(row)
        if removed:
            self.rows()[:] = kept
            store.bump_version(MODULE)
        return removed

    # ---------------------------------------------------------------- 快照
    def snapshot(self, report_code: str | None = None) -> list[dict[str, Any]]:
        """结论表快照；指定报告编号时只快照该报告的行，避免回滚误伤其他报告。"""
        rows = self.rows()
        if report_code is None:
            return copy.deepcopy(rows)
        code = _text(report_code)
        return copy.deepcopy([row for row in rows if _text(row.get("报告编号")) == code])

    def restore(self, report_code: str | None, snapshot: list[dict[str, Any]]) -> None:
        """按快照恢复；指定报告编号时只替换该报告的行。"""
        rows = self.rows()
        if report_code is None:
            store._tables[MODULE] = copy.deepcopy(snapshot)
            return
        code = _text(report_code)
        kept = [row for row in rows if _text(row.get("报告编号")) != code]
        kept.extend(copy.deepcopy(snapshot))
        store._tables[MODULE] = kept


# 跨服务共享一个结论表读写入口，保证去重口径一致。
conclusion_service = InsightConclusionService()
