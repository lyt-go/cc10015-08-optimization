"""洞察报告接口：报告状态流转、结论版本聚合与归档事务。

路由层只做参数整形和错误转 HTTP，状态推进、版本去重、口径校验都放在
``app.services.insight_report`` / ``app.services.insight_conclusion`` 里。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult
from app.services.insight_report import InsightReportService

router = APIRouter(prefix="/api/insight_report", tags=["洞察报告"])
service = InsightReportService()
class ReportActionPayload(BaseModel):
    """报告动作：除动作名外还可以带冲突处理方式与结论生成参数。"""

    action: str
    resolution: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class RecomputePayload(BaseModel):
    """结论重算回执：声明要发布的目标版本与生成参数。"""

    version: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    results = service.list_results()
    archived_versions = {
        (item.get("报告编号"), item.get("当前版本"))
        for item in results
        if item.get("status") == "已归档"
    }
    return {"metrics": [
        {"label": "报告总数", "value": len(results), "trend": 0},
        {"label": "当前结论版本", "value": len(archived_versions) or len(results), "trend": 0},
        {"label": "关键发现数量", "value": sum(int(item.get("关键发现数量") or 0) for item in results), "trend": 0},
    ]}


@router.get("")
def list_results() -> dict[str, Any]:
    """工作台：每个报告只返回当前数据版本的一条聚合结果。"""
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """分析试算，不写结论表。"""
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    return {"items": service.run_analysis(values or {})}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        keys: list[str] = []
        for item in items:
            for key in item:
                if key not in keys:
                    keys.append(key)
        all_keys = ["id", *[key for key in keys if key != "id"]]
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(key, "")) for key in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=insight_report.csv"})


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, Any]) -> ActionResult:
    row, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少或冲突的字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="洞察报告已登记", entry=service.get_entry(int(row["id"])))


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ReportActionPayload) -> ActionResult:
    """状态动作：开始生成、确认完成、归档报告。

    ``resolution`` 只在归档请求与重算回执同时到达时生效：
    ``recompute_then_archive`` 先完成重算再归档；
    ``archive_then_recompute`` 先归档旧结论再发布重算版本。
    """
    row, message = service.run_action(
        entry_id,
        payload.action.strip(),
        resolution=payload.resolution,
        params=payload.values,
        remark=payload.remark,
    )
    if row is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.get_entry(entry_id))


@router.post("/{entry_id}/recompute", response_model=ActionResult)
def receive_recompute(entry_id: int, payload: RecomputePayload) -> ActionResult:
    """接收结论重算回执；不会直接覆盖已归档版本。"""
    row = service.receive_recompute(entry_id, payload.version, payload.values)
    if row is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在")
    return ActionResult(ok=True, message="重算回执已接收，等待随归档发布", entry=service.get_entry(entry_id))


@router.get("/{entry_id}/versions", response_model=dict)
def get_versions(entry_id: int) -> dict:
    """报告版本历史：按版本去重聚合，历史版本可回看且不重复计数。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在")
    return {
        "报告编号": entry.get("报告编号"),
        "当前版本": entry.get("当前版本"),
        "版本状态": entry.get("版本状态"),
        "版本历史": entry.get("版本历史", []),
    }


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"洞察报告 {entry_id} 不存在")
    return entry
