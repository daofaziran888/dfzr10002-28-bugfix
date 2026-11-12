"""查询缓存管理接口：维护缓存规则，覆盖提交校验、启用配置、停用配置与失效重试。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.runtime.cache_store import (
    JOB_FAILED,
    QUERY_MODULE,
    SERVICE_MODULE,
    cache_runtime,
)
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cache import CacheService

router = APIRouter(prefix="/api/cache", tags=["查询缓存管理"])

service = CacheService()

LIST_FIELDS = ["规则编号", "缓存目标", "缓存时间", "失效条件", "预加载设置", "内存上限", "淘汰策略", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    target: str | None = Query(default=None, description="按缓存目标检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号、缓存目标与状态过滤查询缓存管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, target=target, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出查询缓存管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cache", "total": total, "items": items}


@router.get("/runtime/snapshots")
def list_snapshots(target: str | None = Query(default=None, description="按缓存目标过滤")) -> dict[str, Any]:
    """查看当前仍存活的缓存快照；停用失效后对应目标不应再出现旧快照。"""
    items = cache_runtime.list_snapshots(target)
    return {"items": items, "total": len(items)}


@router.get("/runtime/jobs")
def list_jobs(target: str | None = Query(default=None, description="按缓存目标过滤")) -> dict[str, Any]:
    """查看失效作业：待失效、已失效、失效失败（可重试）都在这里留痕。"""
    items = cache_runtime.list_jobs(target)
    return {"items": items, "total": len(items)}


@router.get("/runtime/hits")
def list_hits(target: str | None = Query(default=None, description="按缓存目标过滤")) -> dict[str, Any]:
    """历史命中记录只追加不删除，停用或失效后仍可完整回看。"""
    items = cache_runtime.list_hits(target)
    return {"items": items, "total": len(items)}


@router.post("/runtime/simulate-hit", response_model=ActionResult)
def simulate_hit(payload: EntryPayload) -> ActionResult:
    """模拟查询模板/数据服务按缓存目标取数，验证停用后不再命中旧快照。"""
    values = payload.values
    target = str(values.get("target") or values.get("缓存目标") or "").strip()
    if not target:
        return ActionResult(ok=False, message="缺少缓存目标 target，无法模拟取数")
    caller_module = str(values.get("callerModule") or QUERY_MODULE).strip()
    if caller_module not in (QUERY_MODULE, SERVICE_MODULE):
        return ActionResult(ok=False, message="调用方只能是查询模板(query)或数据服务(service)")
    record = cache_runtime.simulate_hit(target, caller_module, values.get("callerId"))
    return ActionResult(ok=True, message=f"取数结果：{record['result']}", entry=record)


@router.post("/runtime/arm-failure", response_model=ActionResult)
def arm_failure(payload: EntryPayload) -> ActionResult:
    """让指定缓存目标的下一次失效尝试失败，用于演示失效失败与重试链路。"""
    values = payload.values
    target = str(values.get("target") or values.get("缓存目标") or "").strip()
    if not target:
        return ActionResult(ok=False, message="缺少缓存目标 target")
    times = int(values.get("times") or 1)
    cache_runtime.arm_failure(target, times)
    return ActionResult(ok=True, message=f"已注入 {times} 次失效失败，下一次停用/重试将进入{JOB_FAILED}状态")


@router.post("/jobs/{job_id}/retry", response_model=ActionResult)
def retry_job(job_id: int) -> ActionResult:
    """对失效失败的作业重试；成功后清除旧快照并把下游缓存状态同步为已失效口径。"""
    job, message = cache_runtime.retry_invalidation(job_id)
    if job is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=job)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条缓存规则明细（含实时缓存状态、快照与失效作业），与列表同一口径。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存规则 {entry_id} 不存在或已归档")
    target = str(entry.get("缓存目标") or "")
    entry["runtime"] = cache_runtime.target_state(target) if target else None
    entry["hits"] = cache_runtime.list_hits(target) if target else []
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条缓存规则，缺字段时说明原因而不是静默丢弃。"""
    entry, error = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="缓存规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条缓存规则执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
