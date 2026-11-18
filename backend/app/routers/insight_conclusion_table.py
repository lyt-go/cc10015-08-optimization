"""洞察结论数据表接口：按报告版本去重聚合读取，供洞察报告与指标定义（V008）共用。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.services.insight_conclusion import conclusion_service

router = APIRouter(prefix="/api/insight_conclusion", tags=["洞察结论数据表"])


@router.get("")
def list_conclusions(
    report_code: str | None = Query(default=None, alias="报告编号"),
    version: str | None = Query(default=None, alias="版本"),
    raw: bool = Query(default=False, description="为真时附带原始行，便于排查残留"),
) -> dict[str, Any]:
    """读取洞察结论数据表：默认按报告版本去重聚合，不重复累计关键发现。"""
    if report_code:
        versions = [version] if version else conclusion_service.versions(report_code)
        aggregates = [
            conclusion_service.aggregate_by_version(report_code, item_version)
            for item_version in versions
        ]
    else:
        codes = sorted({
            str(row.get("报告编号") or "")
            for row in conclusion_service.rows()
            if row.get("报告编号")
        })
        aggregates = [
            conclusion_service.aggregate_by_version(code, item_version)
            for code in codes
            for item_version in conclusion_service.versions(code)
        ]
    payload: dict[str, Any] = {"aggregates": aggregates}
    if raw:
        payload["items"] = conclusion_service.list_rows(report_code=report_code, version=version)
    return payload


@router.get("/metrics")
def list_metric_fields(
    report_code: str | None = Query(default=None, alias="报告编号"),
    version: str | None = Query(default=None, alias="版本"),
) -> dict[str, Any]:
    """指标定义 V008 模块读取结论表指标字段的入口，同时返回口径冲突清单。"""
    return {
        "items": conclusion_service.metric_fields(report_code=report_code, version=version),
        "conflicts": conclusion_service.find_metric_conflicts(report_code=report_code, version=version),
        "rule_version": "V008",
    }
