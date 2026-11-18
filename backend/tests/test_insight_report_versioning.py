"""洞察报告 V008 链路测试：结论去重聚合、状态同步、归档回滚与并发冲突。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import store
from app.services.insight_conclusion import conclusion_service
from app.services.insight_report import InsightReportService
from app.services.metric_def import MetricDefService


def _reset(reports=None):
    store._tables["insight_report"] = reports if reports is not None else [{
        "id": 1, "status": "草稿", "pending": True, "abnormal": False,
        "报告编号": "INS-1", "分析主题": "主题A", "数据来源": "来源A",
        "关键发现": "", "建议措施": "", "影响范围": "", "生成时间": "",
        "报告版本": "V1", "当前版本": "V1", "版本状态": "草稿",
    }]
    store._tables["insight_conclusion"] = []
    return InsightReportService()


def _draft_row(**overrides):
    row = {
        "id": 1, "status": "草稿", "pending": True, "abnormal": False,
        "报告编号": "INS-1", "分析主题": "主题A", "数据来源": "来源A",
        "关键发现": "", "建议措施": "", "影响范围": "", "生成时间": "",
        "报告版本": "V1", "当前版本": "V1", "版本状态": "草稿",
    }
    row.update(overrides)
    return row


def _current_count(service, entry_id=1):
    detail = service.get_entry(entry_id)
    return detail["关键发现数量"]


def test_retry_does_not_double_count_findings():
    """生成失败后重试，结论按版本覆盖写入，关键发现不重复累计。"""
    service = _reset()
    service.run_action(1, "开始生成")
    first = _current_count(service)
    assert first > 0

    for _ in range(3):
        row, message = service.run_action(1, "开始生成")
        assert row is not None, message
        assert _current_count(service) == first, "重试不应重复累计关键发现"

    rows = conclusion_service.list_rows(report_code="INS-1", version="V1")
    assert len({row["结论编号"] for row in rows}) == len(rows), "同一版本不应出现重复结论编号"


def test_duplicate_content_rows_are_deduped_on_read():
    """读取侧按 (指标编号, 关键发现) 去重，历史脏数据也不会重复计数。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    table = conclusion_service.rows()
    duplicated = [dict(row) for row in table if row["报告编号"] == "INS-1"]
    for offset, row in enumerate(duplicated, start=100):
        clone = dict(row)
        clone["id"] = offset
        clone["结论编号"] = f"{row['结论编号']}-dup{offset}"
        table.append(clone)

    assert _current_count(service) == 3, "同内容重复行不应重复计入关键发现"
    aggregate = conclusion_service.aggregate_by_version("INS-1", "V1")
    assert aggregate["原始行数"] > aggregate["去重行数"]


def test_status_and_version_advance_together():
    """报告状态与结论版本同步推进，不会停在中间态。"""
    service = _reset()
    row, message = service.run_action(1, "开始生成")
    assert row["status"] == "生成中", message
    assert row["版本状态"] == "生成中"
    assert {item["status"] for item in conclusion_service.list_rows(report_code="INS-1", version="V1")} == {"生成中"}

    row, message = service.run_action(1, "确认完成")
    assert row["status"] == "已完成", message
    assert row["版本状态"] == "已完成"
    assert {item["status"] for item in conclusion_service.list_rows(report_code="INS-1", version="V1")} == {"已完成"}

    row, message = service.run_action(1, "归档报告")
    assert row["status"] == "已归档", message
    assert row["版本状态"] == "已归档"
    assert {item["status"] for item in conclusion_service.list_rows(report_code="INS-1", version="V1")} == {"已归档"}


def test_archive_keeps_one_current_version_and_history_readable():
    """重复生成与归档后只出现一条当前数据版本，历史版本仍可回看。"""
    service = _reset()
    for _ in range(2):
        service.run_action(1, "开始生成")
        service.run_action(1, "确认完成")
        service.run_action(1, "归档报告")

    items = service.list_results()
    assert len(items) == 1, "每个报告只应出现一条当前数据版本"
    assert items[0]["当前版本"] == "V2"

    detail = service.get_entry(1)
    versions = {item["报告版本"]: item for item in detail["版本历史"]}
    assert set(versions) == {"V1", "V2"}
    assert versions["V1"]["版本状态"] == "已归档"
    assert versions["V2"]["版本状态"] == "已归档"
    assert versions["V1"]["关键发现数量"] == versions["V2"]["关键发现数量"] == 3


def test_workbench_and_detail_share_same_business_data():
    """工作台与详情页展示相同的业务数据。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    item = service.list_results()[0]
    detail = service.get_entry(1)
    for field in ("当前版本", "版本状态", "关键发现", "关键发现数量", "指标字段"):
        assert item[field] == detail[field], f"{field} 在工作台与详情页不一致"


def test_archive_failure_keeps_last_full_conclusions():
    """归档失败保留最后完整结论，报告不会停在中间态。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    full = conclusion_service.snapshot("INS-1")

    conclusion_service.rows().append({
        "id": 99, "报告编号": "INS-1", "报告版本": "V0",
        "结论编号": "INS-1-V0-C1", "关键发现": "半成品", "指标编号": "M9",
        "指标口径": "默认口径", "指标值": 0.1, "数据来源": "来源A",
        "status": "草稿", "archived": False,
    })

    row, message = service.run_action(1, "归档报告")
    assert row is None and "未清残留" in message
    detail = service.get_entry(1)
    assert detail["status"] == "归档失败"
    assert [item["结论编号"] for item in conclusion_service.list_rows(report_code="INS-1", version="V1")] == [
        item["结论编号"] for item in full
    ]
    assert all(item["status"] == "已完成" for item in conclusion_service.list_rows(report_code="INS-1", version="V1"))

    # 重试：清掉未完成残留后归档成功，已归档章节不会退回草稿。
    row, message = service.run_action(1, "归档报告")
    assert row is not None, message
    assert conclusion_service.find_residue("INS-1") == []
    assert all(item["status"] == "已归档" for item in conclusion_service.list_rows(report_code="INS-1", version="V1"))


def test_archived_version_is_read_only():
    """已归档章节不可被重算覆盖或退回草稿。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    service.run_action(1, "归档报告")

    row, message = service.run_action(1, "确认完成")
    assert row is None and "已归档" in message

    try:
        conclusion_service.upsert_version("INS-1", "V1", [{"关键发现": "篡改"}], status="已完成")
    except ValueError as error:
        assert "已归档" in str(error)
    else:
        raise AssertionError("已归档版本不应允许覆盖写入")

    service.run_action(1, "开始生成")
    assert service.get_entry(1)["当前版本"] == "V2"
    assert all(item["status"] == "已归档" for item in conclusion_service.list_rows(report_code="INS-1", version="V1"))


def test_metric_def_rejects_same_version_with_different_caliber():
    """指标定义 V008 读取状态：同版本不同口径的结论被拒绝。"""
    service = _reset()
    service.run_action(1, "开始生成", params={"指标字段": [
        {"指标编号": "M-001", "指标口径": "点击去重"},
        {"指标编号": "M-001", "指标口径": "曝光去重"},
    ]})
    row, message = service.run_action(1, "确认完成")
    assert row is None and "口径不一致" in message

    conflicts = conclusion_service.find_metric_conflicts(report_code="INS-1", version="V1")
    assert conflicts and conflicts[0]["指标编号"] == "M-001"

    metric_service = MetricDefService()
    result = metric_service.insight_metric_fields(report_code="INS-1")
    assert result["ok"] is False and result["items"] == []

    entry, missing = metric_service.create_entry({
        "指标编号": "M-001", "指标名称": "点击率", "计算公式": "clicks/impressions",
        "口径版本": "V1",
    })
    assert entry is not None, missing
    entry, message = metric_service.run_action(entry["id"], "标记生效")
    assert entry is None and "口径冲突" in message


def test_conflicting_archive_requests_choose_ordering():
    """归档请求与重算回执同时到达：两种先后顺序都可判定且可追溯。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    service.receive_recompute(1)

    row, message = service.run_action(1, "归档报告", resolution="recompute_then_archive")
    assert row is not None, message
    detail = service.get_entry(1)
    assert detail["status"] == "已归档"
    assert detail["当前版本"] == "V2"
    latest = conclusion_service.aggregate_by_version("INS-1", "V2")
    assert "；".join(item["关键发现"] for item in latest["结论"]) == detail["关键发现"]
    assert detail["最近冲突处理"]["说明"] == "归档内容包含重算后的新结论"

    # 先归档旧结论：归档版本保留旧结论，重算版本列为修订。
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    service.receive_recompute(1)

    row, message = service.run_action(1, "归档报告", resolution="archive_then_recompute")
    assert row is not None, message
    detail = service.get_entry(1)
    assert detail["status"] == "已归档"
    assert detail["当前版本"] == "V1"
    assert detail["重算版本"] == "V2"
    assert conclusion_service.aggregate_by_version("INS-1", "V1")["版本状态"] == "已归档"
    assert conclusion_service.aggregate_by_version("INS-1", "V2")["版本状态"] == "修订"
    assert detail["最近冲突处理"]["说明"] == "归档内容保留旧结论，重算版本列为修订"


def test_archive_conflict_rejects_bad_caliber_without_losing_snapshot():
    """重算版本口径冲突时拒绝归档，最后完整结论原样保留。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    full = conclusion_service.snapshot("INS-1")
    service.receive_recompute(1, params={"指标字段": [
        {"指标编号": "M-001", "指标口径": "口径A"},
        {"指标编号": "M-001", "指标口径": "口径B"},
    ]})

    row, message = service.run_action(1, "归档报告", resolution="recompute_then_archive")
    assert row is None and "口径不一致" in message
    detail = service.get_entry(1)
    assert detail["status"] == "已完成"
    assert detail["当前版本"] == "V1"
    # 最后完整结论（V1）原样保留；不合格的重算版本被丢弃，回执仍在等待处理。
    v1_rows = conclusion_service.list_rows(report_code="INS-1", version="V1")
    assert v1_rows == full
    assert conclusion_service.list_rows(report_code="INS-1", version="V2") == []
    assert "V2" not in {item["报告版本"] for item in detail["版本历史"]}
    assert detail["待归档重算版本"] == "V2"
    assert conclusion_service.aggregate_by_version("INS-1", "V1")["版本状态"] == "已完成"


def test_archive_requires_completed_report_and_keeps_state():
    """报告未完成时归档被拒绝，状态与结论都不变。"""
    service = _reset()
    service.run_action(1, "开始生成")
    before = conclusion_service.snapshot("INS-1")

    row, message = service.run_action(1, "归档报告")
    assert row is None and "确认完成" in message
    detail = service.get_entry(1)
    assert detail["status"] == "生成中"
    assert detail["版本状态"] == "生成中"
    assert conclusion_service.snapshot("INS-1") == before


def test_recompute_after_archive_never_rewrites_archived_rows():
    """归档后的重算只能落到新版本，归档行的内容与状态保持不变。"""
    service = _reset()
    service.run_action(1, "开始生成")
    service.run_action(1, "确认完成")
    service.run_action(1, "归档报告")
    archived = conclusion_service.snapshot("INS-1")

    service.receive_recompute(1)
    detail = service.get_entry(1)
    assert detail["重算版本"] == "V2"
    assert conclusion_service.aggregate_by_version("INS-1", "V1")["版本状态"] == "已归档"
    assert [row for row in conclusion_service.snapshot("INS-1") if row["报告版本"] == "V1"] == archived
