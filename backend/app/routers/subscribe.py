"""数据订阅推送接口：列表、动作、清单导入、增量水位、导出与错误清单下载。"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.subscribe import ImportFormatError, SubscribeService

router = APIRouter(prefix="/api/subscribe", tags=["数据订阅推送"])

service = SubscribeService()

LIST_FIELDS = ["任务编号", "数据源名称", "推送目标", "推送格式", "推送频率", "过滤条件", "最近推送", "任务状态"]
STATUSES = ["草稿", "推送中", "已暂停", "已完成"]

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


def _attachment(filename: str) -> dict[str, str]:
    """下载响应头：ASCII 兜底 + RFC 5987 中文文件名。"""
    return {"Content-Disposition": f"attachment; filename=\"subscribe.csv\"; filename*=UTF-8''{quote(filename)}"}


# ----------------------------------------------------------------------
# 静态路径必须放在 /{entry_id} 之前，否则 "export"/"imports" 会被当成编号。
# ----------------------------------------------------------------------
@router.get("/stats", response_model=dict)
def get_stats() -> dict[str, int]:
    """订阅任务与批次的汇总指标，供页面顶部卡片使用。"""
    return service.stats()


@router.get("/export")
def export_entries(format: str = Query(default="csv", pattern="^(csv|json)$")) -> Response:
    """导出订阅任务清单并直接下载；CSV 便于再次导入，JSON 保留水位明细。"""
    if format == "json":
        payload = service.export_json()
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=\"subscribe.json\""},
        )
    return Response(
        content=service.export_csv(),
        media_type=CSV_MEDIA_TYPE,
        headers=_attachment("订阅任务清单.csv"),
    )


@router.get("/template")
def download_template() -> Response:
    """导入模板：只保留合并键、过滤条件与可选展示字段。"""
    content = (
        "﻿任务编号,数据源名称,推送频率,过滤条件,推送目标,推送格式,应用名称\n"
        ",订单主表,每小时,status=已支付,订单推送,JSON,订单中心\n"
    )
    return Response(
        content=content,
        media_type=CSV_MEDIA_TYPE,
        headers=_attachment("订阅任务导入模板.csv"),
    )


@router.get("/imports", response_model=PageResult[dict])
def list_imports(page: int = 1, size: int = 20) -> PageResult[dict]:
    """导入批次列表：成功与失败的批次都保留，失败批次可下载错误清单。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_batches(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/imports/{batch_id}", response_model=dict)
def get_import(batch_id: str) -> dict:
    """读取单个导入批次的拆分结果与错误明细。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次 {batch_id} 不存在")
    return batch


@router.get("/imports/{batch_id}/errors")
def download_import_errors(batch_id: str) -> Response:
    """把该批次的失败项打包成 CSV 错误清单下载。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导入批次 {batch_id} 不存在")
    if not batch.get("errors"):
        raise HTTPException(status_code=404, detail=f"导入批次 {batch_id} 没有失败项")
    return Response(
        content=service.export_errors_csv(batch_id),
        media_type=CSV_MEDIA_TYPE,
        headers=_attachment(f"导入错误清单-{batch_id}.csv"),
    )


@router.post("/imports", response_model=dict)
async def import_entries(request: Request) -> dict:
    """从文件导入订阅任务清单。

    支持三种提交方式：
    * ``multipart/form-data`` 上传 ``file`` 字段（页面默认用法）；
    * 原始请求体为 CSV/JSON 文本，文件名放在 ``X-Filename`` 头；
    * JSON 请求体 ``{"filename": ..., "content": "csv 文本"}`` 或 ``{"items": [...]}``。
    """
    try:
        raw, filename, structured = await _read_import_request(request)
        rows = service.normalise_json_rows(structured) if structured is not None else service.parse_import(raw, filename)
    except ImportFormatError as exc:
        return service.record_parse_failure(request.headers.get("x-filename", ""), str(exc))
    return service.import_entries(rows, filename)


async def _read_import_request(request: Request) -> tuple[bytes, str, list[dict[str, Any]] | None]:
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    try:
        if content_type.startswith("multipart/form-data"):
            return _parse_multipart(body, content_type)
        if content_type.startswith("application/json"):
            payload = json.loads(body.decode("utf-8-sig") or "{}")
            if not isinstance(payload, dict):
                raise ImportFormatError("JSON 请求体需要是对象，包含 filename 与 content 或 items")
            filename = str(payload.get("filename") or "").strip()
            if isinstance(payload.get("content"), str):
                return payload["content"].encode("utf-8"), filename, None
            if isinstance(payload.get("items"), list):
                return b"", filename, payload["items"]
            # 其余 JSON（数组、单对象等）交给 parse_import 统一判断。
            return body, filename, None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportFormatError(f"JSON 请求体解析失败：{exc}") from exc
    filename = request.headers.get("x-filename", "").strip()
    return body, filename, None


def _parse_multipart(body: bytes, content_type: str) -> tuple[bytes, str, None]:
    """不依赖 python-multipart 的单文件 multipart 解析，失败时给出可读原因。"""
    boundary = ""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "boundary":
            boundary = value.strip().strip('"')
            break
    if not boundary:
        raise ImportFormatError("multipart 请求缺少 boundary，无法读取上传文件")
    marker = f"--{boundary}".encode("utf-8")
    for chunk in body.split(marker):
        if b"\r\n\r\n" not in chunk:
            continue
        head, _, content = chunk.partition(b"\r\n\r\n")
        lines = [line.strip() for line in head.decode("utf-8", errors="replace").splitlines() if line.strip()]
        disposition = next((line for line in lines if line.lower().startswith("content-disposition:")), "")
        if "filename=" not in disposition.lower() and 'name="file"' not in disposition.lower():
            continue
        filename = ""
        for item in disposition.split(";")[1:]:
            key, _, value = item.strip().partition("=")
            if key.strip().lower() == "filename":
                filename = value.strip().strip('"')
        content = content.rstrip(b"\r\n")
        if content.endswith(marker):
            content = content[: -len(marker)].rstrip(b"\r\n")
        return content, filename, None
    raise ImportFormatError("multipart 请求里没有找到文件字段 file")


# ----------------------------------------------------------------------
# 单条任务
# ----------------------------------------------------------------------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号、数据源名称或应用名称检索"),
    status: str | None = Query(default=None, description="草稿、推送中、已暂停、已完成"),
    source: str | None = Query(default=None, description="按数据源名称检索"),
    frequency: str | None = Query(default=None, description="按推送频率检索"),
    application: str | None = Query(default=None, description="按应用名称检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号/来源/应用/状态过滤订阅任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        source=source,
        frequency=frequency,
        application=application,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条订阅任务明细（含最近推送记录）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅任务 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/pushes", response_model=dict)
def list_pushes(entry_id: int) -> dict:
    """最近推送记录：保留旧批次，可恢复标记只落在最新水位位置。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅任务 {entry_id} 不存在或已归档")
    return {"entry_id": entry_id, "items": entry["最近推送记录"], "total": len(entry["最近推送记录"])}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条订阅任务；同一数据源名称+推送频率会并入已有任务。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message[0] if message else "订阅任务未登记")
    return ActionResult(ok=True, message="订阅任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """启动推送、暂停推送、归档任务；来源已注销时不允许继续新增推送。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
