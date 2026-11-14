"""调用来源管理接口：维护来源登记，覆盖启用登记、冻结来源、注销来源等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.whitelist import WhitelistService

router = APIRouter(prefix="/api/whitelist", tags=["调用来源管理"])

service = WhitelistService()

LIST_FIELDS = ["登记编号", "应用名称", "调用标识", "日配额上限", "优先级", "生效日期", "失效日期", "登记状态"]
STATUSES = ["草稿", "已登记", "已冻结", "已注销"]


@router.get("/applications", response_model=dict)
def list_applications() -> dict[str, Any]:
    """应用名称选项：订阅任务实际引用过的应用 + 来源登记，标注登记状态。"""
    items = service.applications()
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出调用来源管理清单：返回当前全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "whitelist", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按登记编号或应用名称检索"),
    status: str | None = Query(default=None, description="草稿、已登记、已冻结、已注销"),
    application: str | None = Query(default=None, description="按应用名称检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按登记编号/应用名称与状态过滤调用来源管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, application=application, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条来源登记明细（含关联订阅任务）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"来源登记 {entry_id} 不存在或已注销")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条来源登记，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message="；".join(missing))
    return ActionResult(ok=True, message="来源登记已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """启用登记、冻结来源、注销来源；注销会同步停止引用该应用名称的订阅任务。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
