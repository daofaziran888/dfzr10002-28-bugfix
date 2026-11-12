"""查询缓存管理业务规则：状态流转、字段校验、筛选口径与停用失效链路都收在这里。"""
from __future__ import annotations

from typing import Any

from app.runtime.cache_store import cache_runtime
from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["规则编号", "缓存目标", "缓存时间"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
# 每次状态变更只允许沿生命周期向前流转一步；已停用是终态，恢复需重新登记走完整生命周期。
NEXT_STATUS: dict[str, str] = {
    "草稿": "待校验",
    "待校验": "已启用",
    "已启用": "已停用",
}
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class CacheService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        target: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            # 规则编号检索（原有口径保留）。
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if target:
            # 缓存目标检索：之前只看规则编号导致按目标检索永远是空列表。
            rows = [row for row in rows if target in str(row.get("缓存目标", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = [cache_runtime.enrich_rule(dict(row)) for row in rows]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return cache_runtime.enrich_rule(dict(entry))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        rows = store.rows(MODULE)
        rule_code = str(values.get("规则编号") or "").strip()
        target = str(values.get("缓存目标") or "").strip()
        # 主对象是缓存规则：规则编号唯一；缓存目标与规则编号必须一致指代同一缓存对象，
        # 下游（查询模板、数据服务）只能引用已登记规则的缓存目标。
        if any(str(row.get("规则编号") or "").strip() == rule_code for row in rows):
            return None, f"规则编号 {rule_code} 已存在，规则编号必须唯一"
        conflict = next(
            (
                str(row.get("规则编号") or "").strip()
                for row in rows
                if str(row.get("缓存目标") or "").strip() == target
                and str(row.get("规则编号") or "").strip() != rule_code
            ),
            None,
        )
        if conflict:
            return None, f"缓存目标「{target}」已归属规则 {conflict}，缓存目标与规则编号必须一致"
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return cache_runtime.enrich_rule(dict(entry)), ""

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询缓存管理可执行范围"
        target_status = ACTION_RULES[action]
        current = str(entry.get("status") or "")
        # 严格状态机：每次只允许流转一步，已停用是终态，不能直接回到已启用。
        allowed = NEXT_STATUS.get(current)
        if allowed is None:
            return None, f"缓存规则已处于「{current}」终态，停用后不能回到启用，恢复需重新登记并走完整生命周期"
        if target_status != allowed:
            return None, (
                f"缓存规则当前为「{current}」，只能执行流转到「{allowed}」的动作，"
                "每次状态变更只允许流转一步"
            )
        entry["status"] = target_status
        entry["pending"] = target_status != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"缓存规则已{action}"
        if action == "停用配置":
            # 停用即触发依赖失效：清旧快照、登记失效作业，并把待失效同步给查询模板与数据服务。
            job = cache_runtime.invalidate_on_disable(entry)
            message = self._compose_disable_message(job)
        return cache_runtime.enrich_rule(dict(entry)), message

    @staticmethod
    def _compose_disable_message(job: dict[str, Any]) -> str:
        if job["status"] == "已失效":
            return "缓存规则已停用配置，旧快照已清除，查询模板与数据服务缓存状态已同步"
        if job["status"] == "失效失败":
            return "缓存规则已停用配置，旧快照失效失败，已标记待重试，下游缓存状态同步为失效失败-待重试"
        return "缓存规则已停用配置，失效作业待执行，下游已同步为待失效"
