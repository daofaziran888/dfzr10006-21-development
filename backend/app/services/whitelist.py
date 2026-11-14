"""调用来源管理业务规则：状态流转、字段校验与筛选口径都收在这里。

来源登记的「应用名称」与数据订阅推送任务的「应用名称」是同一个引用键：
注销某个应用后，订阅任务不能再以该应用名称新增或恢复推送。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "whitelist"
SUBSCRIBE_MODULE = "subscribe"
REQUIRED_FIELDS = ["登记编号", "应用名称", "调用标识"]
STATUS_ORDER = ["草稿", "已登记", "已冻结", "已注销"]
ACTION_RULES = {
    "启用登记": ("草稿", "已登记"),
    "冻结来源": ("已登记", "已冻结"),
    "注销来源": ("已冻结", "已注销"),
}
NEGATIVE_ACTIONS = []


class WhitelistService:
    def _linked_tasks(self, application: str) -> list[dict[str, Any]]:
        application = str(application or "").strip()
        if not application:
            return []
        return [
            row
            for row in store.rows(SUBSCRIBE_MODULE)
            if str(row.get("应用名称") or "").strip() == application
            or (
                not str(row.get("应用名称") or "").strip()
                and str(row.get("推送目标") or "").strip() == application
            )
        ]

    def _application_cancelled(self, application: str) -> bool:
        """同一应用的全部登记都注销后才算来源注销；只要还有一条有效登记就仍可推送。"""
        application = str(application or "").strip()
        if not application:
            return False
        entries = [row for row in store.rows(MODULE) if str(row.get("应用名称") or "").strip() == application]
        return bool(entries) and all(row.get("status") == "已注销" for row in entries)

    def _sync_linked_tasks(self, application: str) -> int:
        """把订阅任务的来源状态与来源登记对齐，返回被停止的任务数。"""
        cancelled = self._application_cancelled(application)
        linked = self._linked_tasks(application)
        stopped = 0
        for task in linked:
            task["来源状态"] = "已注销" if cancelled else "正常"
            if cancelled:
                task["可恢复"] = False
                if task.get("status") == "推送中":
                    task["status"] = "已暂停"
                    task["任务状态"] = "已暂停"
                    task["pending"] = True
                task["abnormal"] = True
                stopped += 1
            else:
                task["可恢复"] = True
        return stopped

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        linked = self._linked_tasks(str(row.get("应用名称") or ""))
        view["关联订阅任务数"] = len(linked)
        view["关联订阅任务"] = "、".join(
            str(task.get("任务编号") or "") for task in linked if task.get("任务编号")
        )
        return view

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        application: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            needle = keyword.strip()
            rows = [
                row
                for row in rows
                if needle in str(row.get("登记编号", "")) or needle in str(row.get("应用名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if application:
            needle = application.strip()
            rows = [row for row in rows if needle in str(row.get("应用名称", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._view(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        application = str(values.get("应用名称") or "").strip()
        # 复用已注销应用名称等同于重新启用该来源，只有冻结态可以走注销联动后再启用。

        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        # 重新登记一个此前已全部注销的应用名称时，解除订阅任务的来源锁定。
        self._sync_linked_tasks(application)
        return self._view(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"来源登记 {entry_id} 不存在或已注销"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于调用来源管理可执行范围"
        source_status, target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if entry.get("status") != source_status:
            return None, f"来源登记当前为「{entry.get('status')}」，不能执行「{action}」"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        application = str(entry.get("应用名称") or "").strip()
        if action == "注销来源":
            stopped = self._sync_linked_tasks(application)
            suffix = f"，已停止 {stopped} 个订阅任务的推送" if stopped else ""
            return self._view(entry), f"来源登记已{action}{suffix}"
        if action == "启用登记":
            # 同一应用重新登记后，已注销造成的锁定解除，订阅任务可恢复推送。
            self._sync_linked_tasks(application)
        return self._view(entry), f"来源登记已{action}"

    def applications(self) -> list[dict[str, Any]]:
        """供订阅任务/来源登记选择应用名称：以订阅任务里的应用名称为准并标注登记状态。"""
        names: dict[str, dict[str, Any]] = {}
        for task in store.rows(SUBSCRIBE_MODULE):
            application = str(task.get("应用名称") or task.get("推送目标") or "").strip()
            if not application:
                continue
            names.setdefault(application, {"应用名称": application, "订阅任务": [], "登记状态": "未登记"})
            names[application]["订阅任务"].append(str(task.get("任务编号") or ""))
        for row in store.rows(MODULE):
            application = str(row.get("应用名称") or "").strip()
            if not application:
                continue
            names.setdefault(application, {"应用名称": application, "订阅任务": [], "登记状态": "未登记"})
            names[application]["登记编号"] = str(row.get("登记编号") or "")
            names[application]["登记状态"] = str(row.get("status") or "")
        return sorted(names.values(), key=lambda item: item["应用名称"])
