"""数据订阅推送业务规则：状态流转、字段校验、清单导入与增量水位都收在这里。

导入模型（本模块的关键约定）：

* **合并身份**：``数据源名称 + 推送频率`` 是订阅任务的合并键。同一合并键只保留
  一条任务，反复导入不会产生副本。
* **增量水位**：每个任务维护一个可恢复的水位。相同过滤条件再次导入时，只把水位
  向前推进；过滤条件变化时，先把上一代水位与最近推送记录标记为“已保留”，再创建
  新一代水位。
* **批次拆分**：一次导入按合并键拆成若干逻辑子批次，便于在响应里说明每个任务
  本次会做什么；整次导入仍然是原子的，任一行校验失败都不会改动原清单。
* **冲突优先级**：带明确「任务编号」的行优先于只带合并键的行；同一优先级内后出现
  的行覆盖先出现的行。任务编号不能指向另一个合并键。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "subscribe"
PUSH_MODULE = "_subscribe_pushes"
BATCH_MODULE = "_subscribe_imports"

REQUIRED_FIELDS = ["数据源名称", "推送目标", "推送频率"]
IMPORT_REQUIRED_FIELDS = ["数据源名称", "推送频率"]
STATUS_ORDER = ["草稿", "推送中", "已暂停", "已完成"]
ACTION_RULES = {"启动推送": "草稿", "暂停推送": "推送中", "归档任务": "已暂停"}
NEGATIVE_ACTIONS: list[str] = []

IMPORT_FIELDS = [
    "任务编号",
    "数据源名称",
    "推送目标",
    "推送格式",
    "推送频率",
    "过滤条件",
    "应用名称",
]
EXPORT_FIELDS = [
    "任务编号",
    "数据源名称",
    "应用名称",
    "推送目标",
    "推送格式",
    "推送频率",
    "过滤条件",
    "最近推送",
    "任务状态",
    "合并键",
    "水位版本",
    "增量水位",
    "可恢复位置",
    "可恢复",
]

# CSV/JSON 导入时兼容常见英文列名，中文列名始终优先。
FIELD_ALIASES = {
    "task_id": "任务编号",
    "task_no": "任务编号",
    "taskid": "任务编号",
    "任务id": "任务编号",
    "source": "数据源名称",
    "data_source": "数据源名称",
    "数据源": "数据源名称",
    "target": "推送目标",
    "push_target": "推送目标",
    "format": "推送格式",
    "push_format": "推送格式",
    "frequency": "推送频率",
    "push_frequency": "推送频率",
    "filter": "过滤条件",
    "filter_condition": "过滤条件",
    "application": "应用名称",
    "app": "应用名称",
    "应用": "应用名称",
}

MAX_IMPORT_ROWS = 5000


class ImportFormatError(ValueError):
    """导入文件本身无法解析时抛出，调用方会把原因原样返回给页面。"""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_filter(value: Any) -> str:
    """过滤条件先归一化再计算指纹，避免首尾空格造成“假变化”。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _text(value)


def _fingerprint(value: Any) -> str:
    text = _normalise_filter(value)
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class SubscribeService:
    def __init__(self) -> None:
        self._normalise_all()

    # ------------------------------------------------------------------
    # 基础读取
    # ------------------------------------------------------------------
    def _normalise_all(self) -> None:
        for row in store.rows(MODULE):
            self._normalise_task(row)

    def _normalise_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """给老数据补齐导入相关字段，保证新旧记录都能参与合并与水位推进。"""
        source = _text(task.get("数据源名称"))
        frequency = _text(task.get("推送频率"))
        task["数据源名称"] = source
        task["推送频率"] = frequency
        task["应用名称"] = _text(task.get("应用名称")) or _text(task.get("推送目标")) or source
        task["合并键"] = f"{source}::{frequency}"

        status = _text(task.get("status")) or STATUS_ORDER[0]
        task["status"] = status
        task["任务状态"] = status

        watermark = task.get("增量水位")
        if not isinstance(watermark, dict):
            watermark = {
                "版本": int(task.get("水位版本") or 1),
                "位置": int(task.get("水位") or 0),
                "批次号": _text(task.get("最近推送批次")),
                "过滤指纹": _fingerprint(task.get("过滤条件")),
                "可恢复位置": _text(task.get("可恢复位置"))
                or f"第{int(task.get('水位版本') or 1)}代水位#0条",
                "更新时间": "",
                "历史": [],
            }
            task["增量水位"] = watermark
        task["水位版本"] = int(watermark.get("版本") or 1)
        task["水位"] = int(watermark.get("位置") or 0)
        task["可恢复位置"] = _text(watermark.get("可恢复位置"))
        task["可恢复"] = bool(task.get("可恢复", True))
        task["来源状态"] = _text(task.get("来源状态")) or "正常"
        return task

    def _task_view(self, task: dict[str, Any]) -> dict[str, Any]:
        self._normalise_task(task)
        view = dict(task)
        view["最近推送记录数"] = sum(
            1 for record in store.rows(PUSH_MODULE) if int(record.get("任务ID", 0)) == int(task.get("id", 0))
        )
        return view

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        source: str | None = None,
        frequency: str | None = None,
        application: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._normalise_task(row) for row in store.rows(MODULE)]
        if keyword:
            needle = keyword.strip()
            rows = [
                row
                for row in rows
                if needle in _text(row.get("任务编号"))
                or needle in _text(row.get("数据源名称"))
                or needle in _text(row.get("应用名称"))
            ]
        if source:
            rows = [row for row in rows if source.strip() in _text(row.get("数据源名称"))]
        if frequency:
            rows = [row for row in rows if frequency.strip() in _text(row.get("推送频率"))]
        if application:
            rows = [row for row in rows if application.strip() in _text(row.get("应用名称"))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._task_view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        task = store.find(MODULE, entry_id)
        if task is None:
            return None
        view = self._task_view(task)
        view["最近推送记录"] = self.list_pushes(entry_id)
        return view

    def list_pushes(self, entry_id: int) -> list[dict[str, Any]]:
        records = [
            dict(record)
            for record in store.rows(PUSH_MODULE)
            if int(record.get("任务ID", 0)) == int(entry_id)
        ]
        records.sort(key=lambda item: int(item.get("id", 0)))
        return records

    def stats(self) -> dict[str, int]:
        tasks = [self._normalise_task(row) for row in store.rows(MODULE)]
        return {
            "total": len(tasks),
            "pushing": sum(1 for row in tasks if row.get("status") == "推送中"),
            "paused": sum(1 for row in tasks if row.get("status") == "已暂停"),
            "recoverable": sum(1 for row in tasks if row.get("可恢复")),
            "push_records": len(store.rows(PUSH_MODULE)),
            "import_batches": len(store.rows(BATCH_MODULE)),
        }

    # ------------------------------------------------------------------
    # 单条登记与动作
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记单条任务；第二个返回值是可直接展示的失败原因，成功时为空。"""
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]

        source = _text(values.get("数据源名称"))
        frequency = _text(values.get("推送频率"))
        application = _text(values.get("应用名称")) or _text(values.get("推送目标")) or source
        if self._application_deregistered(application):
            return None, [f"应用名称「{application}」已在调用来源管理注销，不能新增推送"]

        rows = store.rows(MODULE)
        key = f"{source}::{frequency}"
        existing = next((row for row in rows if self._merge_key(row) == key), None)
        if existing is not None:
            # 同一数据源名称 + 推送频率只保留一条任务，登记重复任务同样不产生副本。
            self._normalise_task(existing)
            old_filter = _normalise_filter(existing.get("过滤条件"))
            for field in ("推送目标", "推送格式"):
                if field in values and _text(values.get(field)):
                    existing[field] = _text(values.get(field))
            if "过滤条件" in values:
                existing["过滤条件"] = _normalise_filter(values.get("过滤条件"))
            existing["应用名称"] = application
            filter_changed = _fingerprint(existing.get("过滤条件")) != _fingerprint(old_filter)
            if filter_changed or _text(values.get("记录水位")):
                self._advance_watermark(
                    existing,
                    batch_id=f"MANUAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    timestamp=self._now(),
                    filter_changed=filter_changed,
                    is_new_task=False,
                )
            return self._task_view(existing), []

        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["任务编号"] = _text(values.get("任务编号")) or self._next_task_number(
            {_text(row.get("任务编号")) for row in rows}
        )
        entry["数据源名称"] = source
        entry["推送频率"] = frequency
        entry["推送目标"] = _text(values.get("推送目标"))
        entry["推送格式"] = _text(values.get("推送格式"))
        entry["过滤条件"] = _normalise_filter(values.get("过滤条件"))
        entry["应用名称"] = application
        entry["status"] = STATUS_ORDER[0]
        entry["任务状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最近推送"] = ""
        rows.append(entry)
        self._normalise_task(entry)
        return self._task_view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"订阅任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据订阅推送可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        self._normalise_task(entry)
        if action == "启动推送" and self._application_deregistered(_text(entry.get("应用名称"))):
            return None, (
                f"应用名称「{_text(entry.get('应用名称'))}」已在调用来源管理注销，"
                f"订阅任务 {_text(entry.get('任务编号'))} 不能继续新增推送"
            )

        entry["status"] = target
        entry["任务状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "启动推送":
            # 来源只有在全部登记注销时才不可恢复；重新登记后允许恢复推送。
            entry["来源状态"] = "正常"
            entry["可恢复"] = True
            entry["abnormal"] = False
        return entry, f"订阅任务已{action}"

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    def normalise_json_rows(self, payload: Any) -> list[dict[str, Any]]:
        """已解码的 JSON 任务数组（例如 ``{"items": [...]}``）走同一套行映射。"""
        return self._rows_from_json(payload)

    def parse_import(self, raw: bytes, filename: str = "") -> list[dict[str, Any]]:
        """把上传的 CSV / JSON 解析成带行号的字典列表，不在这里做业务校验。"""
        if not raw or not raw.strip():
            raise ImportFormatError("导入文件为空，请选择包含订阅任务的 CSV 或 JSON 文件")
        text = self._decode(raw)
        stripped = text.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                if filename.lower().endswith(".json"):
                    raise ImportFormatError(f"JSON 文件解析失败：第 {exc.lineno} 行 {exc.msg}") from exc
                raise ImportFormatError(f"导入内容既不是有效 JSON 也不是 CSV：{exc.msg}") from exc
            return self._rows_from_json(payload, filename)
        return self._rows_from_csv(text)

    def _decode(self, raw: bytes) -> str:
        for encoding in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ImportFormatError("文件编码无法识别，请使用 UTF-8 或 GB18030 编码")

    def _rows_from_json(self, payload: Any, filename: str = "") -> list[dict[str, Any]]:
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            return self.parse_import(payload["content"].encode("utf-8"), filename or "import.csv")
        if isinstance(payload, dict):
            for key in ("items", "rows", "tasks", "data"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
            else:
                mapped_keys = {self._field_name(key) for key in payload}
                if "数据源名称" in mapped_keys:
                    payload = [payload]
                else:
                    raise ImportFormatError("JSON 需要是任务数组，或包含 items / rows / tasks / data 数组")
        if not isinstance(payload, list):
            raise ImportFormatError("JSON 订阅任务清单必须是数组")
        if not payload:
            raise ImportFormatError("JSON 订阅任务清单为空")
        if len(payload) > MAX_IMPORT_ROWS:
            raise ImportFormatError(f"单次最多导入 {MAX_IMPORT_ROWS} 行，请拆分文件后重试")
        rows = []
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                raise ImportFormatError(f"第 {index} 条 JSON 记录不是对象，无法识别订阅任务")
            rows.append(self._map_row(item, index, item))
        return rows

    def _rows_from_csv(self, text: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ImportFormatError("CSV 缺少表头，至少需要「数据源名称」和「推送频率」两列")
        recognised = {self._field_name(name) for name in reader.fieldnames if name}
        missing = [field for field in IMPORT_REQUIRED_FIELDS if field not in recognised]
        if missing:
            raise ImportFormatError(f"CSV 缺少必需列：{'、'.join(missing)}")
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(reader, start=2):
            if not any(_text(value) for value in item.values() if value is not None):
                continue
            rows.append(self._map_row(item, index, item))
            if len(rows) > MAX_IMPORT_ROWS:
                raise ImportFormatError(f"单次最多导入 {MAX_IMPORT_ROWS} 行，请拆分文件后重试")
        if not rows:
            raise ImportFormatError("CSV 中没有可导入的订阅任务行")
        return rows

    def _field_name(self, name: Any) -> str | None:
        key = _text(name)
        if not key:
            return None
        if key in IMPORT_FIELDS:
            return key
        return FIELD_ALIASES.get(key.lower()) or FIELD_ALIASES.get(key)

    def _map_row(self, item: dict[str, Any], index: int, raw: dict[str, Any]) -> dict[str, Any]:
        mapped: dict[str, Any] = {}
        for key, value in item.items():
            field = self._field_name(key)
            if field is None:
                continue
            mapped[field] = _text(value)
        mapped["_row"] = index
        mapped["_raw"] = {
            _text(key): ("" if value is None else str(value))
            for key, value in raw.items()
            if key is not None
        }
        return mapped

    def import_entries(self, rows: list[dict[str, Any]], filename: str = "") -> dict[str, Any]:
        """校验并提交一批导入；校验失败时只落批次错误，原订阅任务清单保持不动。"""
        timestamp = self._now()
        batch_id = self._next_batch_id()
        errors: list[dict[str, Any]] = []
        groups: dict[str, list[dict[str, Any]]] = {}

        for row in rows:
            source = _text(row.get("数据源名称"))
            frequency = _text(row.get("推送频率"))
            missing = []
            if not source:
                missing.append("数据源名称")
            if not frequency:
                missing.append("推送频率")
            if missing:
                errors.append(self._error(row, f"缺少必填字段：{'、'.join(missing)}"))
                continue
            groups.setdefault(self._merge_key(source, frequency), []).append(row)

        # 同一文件内，一个任务编号不能对应两个合并键。
        number_keys: dict[str, str] = {}
        for merge_key, group in groups.items():
            for row in group:
                task_no = _text(row.get("任务编号"))
                if not task_no:
                    continue
                previous = number_keys.get(task_no)
                if previous and previous != merge_key:
                    errors.append(
                        self._error(
                            row,
                            f"任务编号 {task_no} 在同一批次里对应了多个数据源/推送频率，无法合并",
                        )
                    )
                else:
                    number_keys[task_no] = merge_key

        plans: list[dict[str, Any]] = []
        for merge_key, group in groups.items():
            # 明确任务编号优先；同优先级后出现的行覆盖先出现的行。
            chosen = max(group, key=lambda item: (1 if _text(item.get("任务编号")) else 0, int(item["_row"])))
            source = _text(chosen.get("数据源名称"))
            frequency = _text(chosen.get("推送频率"))
            task_no = _text(chosen.get("任务编号"))
            row_numbers = {_text(item.get("任务编号")) for item in group if _text(item.get("任务编号"))}
            if len(row_numbers) > 1:
                errors.append(
                    self._error(
                        chosen,
                        f"同一数据源「{source}」/推送频率「{frequency}」出现了多个任务编号"
                        f"（{'、'.join(sorted(row_numbers))}），无法确定合并到哪条订阅任务",
                    )
                )
                continue
            before = len(errors)

            existing_by_key = self._find_by_key(merge_key)
            existing_by_number = self._find_by_number(task_no) if task_no else None
            if existing_by_number is not None and self._merge_key(existing_by_number) != merge_key:
                errors.append(
                    self._error(
                        chosen,
                        f"任务编号 {task_no} 已属于数据源「{_text(existing_by_number.get('数据源名称'))}」"
                        f"/推送频率「{_text(existing_by_number.get('推送频率'))}」，与本次合并键冲突",
                    )
                )
                continue
            if (
                existing_by_key is not None
                and existing_by_number is not None
                and int(existing_by_key.get("id", 0)) != int(existing_by_number.get("id", 0))
            ):
                errors.append(
                    self._error(chosen, f"任务编号 {task_no} 与合并键命中了不同的订阅任务，请核对清单")
                )
                continue
            target = existing_by_number or existing_by_key
            if target is not None:
                self._normalise_task(target)

            application = _text(chosen.get("应用名称")) or _text(chosen.get("推送目标"))
            if not application and target is not None:
                application = _text(target.get("应用名称"))
            if not application:
                application = source
            if self._application_deregistered(application):
                label = _text(target.get("任务编号")) if target else (task_no or "自动生成")
                errors.append(
                    self._error(
                        chosen,
                        f"订阅任务 {label} 的应用名称「{application}」已在调用来源管理注销，"
                        "不能新增或恢复推送",
                    )
                )
                continue

            if "过滤条件" in chosen:
                new_filter = _normalise_filter(chosen.get("过滤条件"))
            elif target is not None:
                new_filter = _normalise_filter(target.get("过滤条件"))
            else:
                new_filter = ""
            old_filter = _normalise_filter(target.get("过滤条件")) if target is not None else ""
            filter_changed = target is not None and _fingerprint(new_filter) != _fingerprint(old_filter)

            if target is None:
                action = "新建任务"
            elif filter_changed:
                action = "过滤条件变化（保留旧记录，新建水位）"
            else:
                action = "仅更新水位"

            plans.append(
                {
                    "merge_key": merge_key,
                    "source": source,
                    "frequency": frequency,
                    "task_no": task_no,
                    "application": application,
                    "filter": new_filter,
                    "filter_changed": filter_changed,
                    "target": target,
                    "chosen": chosen,
                    "action": action,
                    "duplicates": max(len(group) - 1, 0),
                    "rows": len(group),
                    "errors_before": before,
                }
            )

        split_batches = [
            {
                "index": index,
                "merge_key": plan["merge_key"],
                "source": plan["source"],
                "frequency": plan["frequency"],
                "rows": plan["rows"],
                "duplicates": plan["duplicates"],
                "action": plan["action"],
            }
            for index, plan in enumerate(plans, start=1)
        ]

        if errors:
            batch = self._save_batch(
                batch_id=batch_id,
                filename=filename,
                status="失败",
                total=len(rows),
                created=0,
                updated=0,
                watermark_only=0,
                duplicates=sum(max(len(group) - 1, 0) for group in groups.values()),
                errors=errors,
                groups=split_batches,
                task_numbers=[],
                timestamp=timestamp,
            )
            first = errors[0]
            identity = self._identity_text(first)
            return {
                "ok": False,
                "message": (
                    f"导入失败：{len(errors)} 行未通过校验，原订阅任务清单保持不变。"
                    f"首个问题：{identity}，{_text(first.get('错误原因'))}"
                ),
                "batch_id": batch_id,
                "status": "失败",
                "total": len(rows),
                "created": 0,
                "updated": 0,
                "watermark_only": 0,
                "duplicates": batch["duplicates"],
                "failed": len(errors),
                "groups": split_batches,
                "tasks": [],
                "errors": errors,
                "error_list_url": f"/api/subscribe/imports/{batch_id}/errors",
            }

        created = 0
        updated = 0
        watermark_only = 0
        touched: list[dict[str, Any]] = []
        used_numbers = {_text(row.get("任务编号")) for row in store.rows(MODULE)}
        next_id = max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1

        for plan in plans:
            target = plan["target"]
            chosen = plan["chosen"]
            if target is None:
                number = plan["task_no"] or self._next_task_number(used_numbers)
                used_numbers.add(number)
                target = {
                    "id": next_id,
                    "任务编号": number,
                    "数据源名称": plan["source"],
                    "推送频率": plan["frequency"],
                    "推送目标": _text(chosen.get("推送目标")) or plan["application"],
                    "推送格式": _text(chosen.get("推送格式")),
                    "过滤条件": plan["filter"],
                    "应用名称": plan["application"],
                    "最近推送": "",
                    "status": "推送中",
                    "任务状态": "推送中",
                    "pending": True,
                    "abnormal": False,
                    "来源状态": "正常",
                    "可恢复": True,
                }
                next_id += 1
                store.rows(MODULE).append(target)
                self._normalise_task(target)
                created += 1
            else:
                for field in ("推送目标", "推送格式"):
                    if _text(chosen.get(field)):
                        target[field] = _text(chosen.get(field))
                target["应用名称"] = plan["application"]
                if plan["filter_changed"]:
                    target["过滤条件"] = plan["filter"]
                updated += 1
                if not plan["filter_changed"]:
                    watermark_only += 1

            self._advance_watermark(
                target,
                batch_id=batch_id,
                timestamp=timestamp,
                filter_changed=plan["filter_changed"],
                is_new_task=plan["target"] is None,
            )
            touched.append(self._task_view(target))

        batch = self._save_batch(
            batch_id=batch_id,
            filename=filename,
            status="成功",
            total=len(rows),
            created=created,
            updated=updated,
            watermark_only=watermark_only,
            duplicates=sum(max(len(group) - 1, 0) for group in groups.values()),
            errors=[],
            groups=split_batches,
            task_numbers=[_text(task.get("任务编号")) for task in touched],
            timestamp=timestamp,
        )
        message = (
            f"导入完成：新建 {created} 个任务，合并更新 {updated} 个任务"
            f"（其中仅更新水位 {watermark_only} 个），重复行 {batch['duplicates']} 条未产生副本；"
            f"批次 {batch_id}"
        )
        return {
            "ok": True,
            "message": message,
            "batch_id": batch_id,
            "status": "成功",
            "total": len(rows),
            "created": created,
            "updated": updated,
            "watermark_only": watermark_only,
            "duplicates": batch["duplicates"],
            "failed": 0,
            "groups": split_batches,
            "tasks": touched,
            "errors": [],
            "error_list_url": "",
        }

    def record_parse_failure(self, filename: str, message: str) -> dict[str, Any]:
        """文件无法解析时也留一条失败批次，方便下载错误清单；不触碰任务清单。"""
        timestamp = self._now()
        batch_id = self._next_batch_id()
        error = {
            "行号": "-",
            "任务编号": "-",
            "数据源名称": "-",
            "推送频率": "-",
            "过滤条件": "-",
            "错误原因": message,
            "原始数据": "",
        }
        self._save_batch(
            batch_id=batch_id,
            filename=filename,
            status="失败",
            total=0,
            created=0,
            updated=0,
            watermark_only=0,
            duplicates=0,
            errors=[error],
            groups=[],
            task_numbers=[],
            timestamp=timestamp,
        )
        return {
            "ok": False,
            "message": f"导入失败：{message}；原订阅任务清单保持不变。",
            "batch_id": batch_id,
            "status": "失败",
            "total": 0,
            "created": 0,
            "updated": 0,
            "watermark_only": 0,
            "duplicates": 0,
            "failed": 1,
            "groups": [],
            "tasks": [],
            "errors": [error],
            "error_list_url": f"/api/subscribe/imports/{batch_id}/errors",
        }

    def _advance_watermark(
        self,
        task: dict[str, Any],
        *,
        batch_id: str,
        timestamp: str,
        filter_changed: bool,
        is_new_task: bool,
    ) -> None:
        watermark = task.get("增量水位")
        if not isinstance(watermark, dict):
            self._normalise_task(task)
            watermark = task["增量水位"]

        history = list(watermark.get("历史") or [])
        if filter_changed and not is_new_task:
            # 过滤条件变化：先保留上一代水位，再创建新一代水位。
            snapshot = {key: value for key, value in watermark.items() if key != "历史"}
            snapshot["保留时间"] = timestamp
            history.append(snapshot)
            history = history[-20:]
            generation = int(watermark.get("版本") or 1) + 1
            position = 1
        else:
            generation = int(watermark.get("版本") or 1)
            position = int(watermark.get("位置") or 0) + 1

        position_text = f"第{generation}代水位#{position}条"
        watermark = {
            "版本": generation,
            "位置": position,
            "批次号": batch_id,
            "过滤指纹": _fingerprint(task.get("过滤条件")),
            "可恢复位置": position_text,
            "更新时间": timestamp,
            "历史": history,
        }
        task["增量水位"] = watermark
        task["水位版本"] = generation
        task["水位"] = position
        task["可恢复位置"] = position_text
        task["可恢复"] = True
        task["来源状态"] = "正常"
        task["最近推送"] = timestamp
        task["最近推送批次"] = batch_id

        # 旧批次的最近推送记录继续保留，只把“可恢复”标记挪到本次新位置。
        for record in store.rows(PUSH_MODULE):
            if int(record.get("任务ID", 0)) == int(task.get("id", 0)) and record.get("状态") == "可恢复":
                record["状态"] = "已保留"

        push_rows = store.rows(PUSH_MODULE)
        record_id = max((int(record.get("id", 0)) for record in push_rows), default=0) + 1
        push_rows.append(
            {
                "id": record_id,
                "记录编号": f"PUSH-{record_id:04d}",
                "任务ID": int(task.get("id", 0)),
                "任务编号": _text(task.get("任务编号")),
                "数据源名称": _text(task.get("数据源名称")),
                "推送频率": _text(task.get("推送频率")),
                "过滤条件": _text(task.get("过滤条件")),
                "过滤指纹": _fingerprint(task.get("过滤条件")),
                "批次号": batch_id,
                "水位版本": generation,
                "水位位置": position,
                "可恢复位置": position_text,
                "推送结果": "已记录",
                "状态": "可恢复",
                "创建时间": timestamp,
            }
        )

    # ------------------------------------------------------------------
    # 批次与错误清单
    # ------------------------------------------------------------------
    def list_batches(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = list(reversed(store.rows(BATCH_MODULE)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._batch_view(row) for row in rows[start:start + size]], total

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("batch_id") == batch_id:
                return self._batch_view(row)
        return None

    def _batch_view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        view["errors"] = [dict(item) for item in row.get("errors", [])]
        view["groups"] = [dict(item) for item in row.get("groups", [])]
        return view

    def _save_batch(
        self,
        *,
        batch_id: str,
        filename: str,
        status: str,
        total: int,
        created: int,
        updated: int,
        watermark_only: int,
        duplicates: int,
        errors: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        task_numbers: list[str],
        timestamp: str,
    ) -> dict[str, Any]:
        rows = store.rows(BATCH_MODULE)
        batch = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "batch_id": batch_id,
            "file_name": filename,
            "status": status,
            "total": total,
            "created": created,
            "updated": updated,
            "watermark_only": watermark_only,
            "duplicates": duplicates,
            "failed": len(errors),
            "groups": groups,
            "errors": errors,
            "task_numbers": task_numbers,
            "created_at": timestamp,
        }
        rows.append(batch)
        return batch

    def export_errors_csv(self, batch_id: str | None = None) -> str:
        rows = store.rows(BATCH_MODULE)
        if batch_id:
            rows = [row for row in rows if row.get("batch_id") == batch_id]
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=["批次号", "行号", "任务编号", "数据源名称", "推送频率", "过滤条件", "错误原因", "原始数据"],
        )
        writer.writeheader()
        for batch in rows:
            for error in batch.get("errors", []):
                writer.writerow(
                    {
                        "批次号": batch.get("batch_id", ""),
                        "行号": error.get("行号", ""),
                        "任务编号": error.get("任务编号", ""),
                        "数据源名称": error.get("数据源名称", ""),
                        "推送频率": error.get("推送频率", ""),
                        "过滤条件": error.get("过滤条件", ""),
                        "错误原因": error.get("错误原因", ""),
                        "原始数据": error.get("原始数据", ""),
                    }
                )
        return "﻿" + buffer.getvalue()

    def export_csv(self) -> str:
        rows = [self._normalise_task(row) for row in store.rows(MODULE)]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            record = {field: row.get(field, "") for field in EXPORT_FIELDS}
            record["增量水位"] = json.dumps(row.get("增量水位", {}), ensure_ascii=False)
            writer.writerow(record)
        return "﻿" + buffer.getvalue()

    def export_json(self) -> dict[str, Any]:
        items = [self._task_view(row) for row in store.rows(MODULE)]
        return {"module": "subscribe", "total": len(items), "items": items}

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _merge_key(self, task_or_source: Any, frequency: str | None = None) -> str:
        if isinstance(task_or_source, dict):
            source = _text(task_or_source.get("数据源名称"))
            frequency = _text(task_or_source.get("推送频率"))
        else:
            source = _text(task_or_source)
            frequency = _text(frequency)
        return f"{source}::{frequency}"

    def _find_by_key(self, merge_key: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if self._merge_key(row) == merge_key:
                return row
        return None

    def _find_by_number(self, task_no: str) -> dict[str, Any] | None:
        if not task_no:
            return None
        for row in store.rows(MODULE):
            if _text(row.get("任务编号")) == task_no:
                return row
        return None

    def _next_task_number(self, used: set[str]) -> str:
        numbers = [int(text.split("-")[-1]) for text in used if text.startswith("SUBS-") and text.split("-")[-1].isdigit()]
        number = max(numbers, default=0) + 1
        while True:
            candidate = f"SUBS-{number:04d}"
            if candidate not in used:
                return candidate
            number += 1

    def _next_batch_id(self) -> str:
        sequence = len(store.rows(BATCH_MODULE)) + 1
        return f"IMPB-{datetime.now().strftime('%Y%m%d')}-{sequence:04d}"

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _error(self, row: dict[str, Any], reason: str) -> dict[str, Any]:
        task_no = _text(row.get("任务编号")) or "自动生成"
        return {
            "行号": row.get("_row", "-"),
            "任务编号": task_no,
            "数据源名称": _text(row.get("数据源名称")),
            "推送频率": _text(row.get("推送频率")),
            "过滤条件": _normalise_filter(row.get("过滤条件")),
            "错误原因": reason,
            "原始数据": json.dumps(row.get("_raw", {}), ensure_ascii=False),
        }

    def _identity_text(self, error: dict[str, Any]) -> str:
        return (
            f"第 {error.get('行号', '-')} 行（任务编号 {error.get('任务编号', '-')}，"
            f"数据源名称「{error.get('数据源名称', '-')}」，"
            f"推送频率「{error.get('推送频率', '-')}」）"
        )

    def _application_deregistered(self, application: str) -> bool:
        """同一应用既可能有多条登记；只要还有未注销登记，就仍然允许推送。

        调用来源里完全没有该应用时按“未登记”放行（保持与旧数据兼容），
        只有存在登记且全部已注销才拦截。
        """
        application = _text(application)
        if not application:
            return False
        rows = [row for row in store.rows("whitelist") if _text(row.get("应用名称")) == application]
        if not rows:
            return False
        return all(row.get("status") == "已注销" for row in rows)
