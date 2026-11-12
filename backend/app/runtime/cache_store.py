"""缓存运行时：把缓存规则与真实的缓存命中、快照失效串起来。

设计要点（对应查询缓存管理的修复口径）：

* 快照按「缓存目标」作为键存放；查询模板、数据服务取数时都从这里查快照，
  规则停用后该目标下的旧快照必须被清掉，下游不能再命中旧数据。
* 停用结论优先：所有命中判断与失效动作都在同一把锁里完成，停用一旦落库，
  并发进来的取数请求只会走缓存未命中，不会再读到旧快照。
* 失效是一份可追踪的作业（待失效 → 已失效 / 失效失败），失败可重试；
  待失效会先同步给查询模板与数据服务，避免下游继续展示「缓存正常」。
* 历史命中记录只追加、不删除，失效与停用都不影响审计留痕。
* 查询模板、数据服务只是缓存规则的下游引用方：它们的缓存状态不自己维护，
  统一由本运行时按缓存目标派生，保证列表、详情、快照三处口径一致。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

CACHE_MODULE = "cache"
QUERY_MODULE = "query"
SERVICE_MODULE = "service"

# 下游模块里指向缓存目标的字段名，以及展示缓存状态用的字段名。
TARGET_FIELD = "缓存目标"
CACHE_STATUS_FIELD = "缓存状态"

# 失效作业状态
JOB_PENDING = "待失效"
JOB_DONE = "已失效"
JOB_FAILED = "失效失败"

# 下游缓存状态（由规则状态与失效作业派生）
DOWNSTREAM_OK = "缓存正常"
DOWNSTREAM_PENDING = "待失效"
DOWNSTREAM_FAILED = "失效失败-待重试"
DOWNSTREAM_DISABLED = "缓存已停用"
DOWNSTREAM_WARMING = "待生效"
DOWNSTREAM_INACTIVE = "缓存未启用"
DOWNSTREAM_UNBOUND = "未接入缓存"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CacheRuntime:
    """进程内缓存运行时；真实项目里这里会换成 Redis + 消息队列 + 任务表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 缓存目标 -> 快照列表（同一目标允许不同参数键的多份快照）
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        # 缓存目标 -> 失效作业列表（最新的在末尾）
        self._jobs: dict[str, list[dict[str, Any]]] = {}
        # 只追加的历史命中记录
        self._hits: list[dict[str, Any]] = []
        # 失效失败注入：缓存目标 -> 剩余失败次数，用于演示/验证重试链路
        self._fail_budget: dict[str, int] = {}
        self._job_seq = 0
        self._hit_seq = 0
        self._snapshot_seq = 0
        self._bootstrapped = False

    # ------------------------------------------------------------------
    # 启动装配：依据种子数据建立初始快照与历史命中
    # ------------------------------------------------------------------
    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        with self._lock:
            if self._bootstrapped:
                return
            for rule in store.rows(CACHE_MODULE):
                target = str(rule.get("缓存目标") or "").strip()
                if not target or rule.get("status") != "已启用":
                    continue
                self._snapshot_seq += 1
                self._snapshots.setdefault(target, []).append(
                    {
                        "snapshotId": self._snapshot_seq,
                        "target": target,
                        "ruleCode": rule.get("规则编号"),
                        "payload": f"{target}的缓存快照",
                        "createdAt": _now(),
                    }
                )
            # 给演示场景留两条历史命中（停用后也必须保留）
            demo_target = "查询模板-地区月报"
            if demo_target in self._snapshots:
                for caller, caller_id in ((QUERY_MODULE, 4), (SERVICE_MODULE, 4)):
                    self._hit_seq += 1
                    self._hits.append(
                        {
                            "hitId": self._hit_seq,
                            "target": demo_target,
                            "callerModule": caller,
                            "callerId": caller_id,
                            "result": "命中",
                            "ruleCode": self._rule_code_for_target(demo_target),
                            "time": _now(),
                        }
                    )
            self._bootstrapped = True

    # ------------------------------------------------------------------
    # 规则侧工具
    # ------------------------------------------------------------------
    def _rule_code_for_target(self, target: str) -> str | None:
        for rule in store.rows(CACHE_MODULE):
            if str(rule.get("缓存目标") or "").strip() == target:
                return str(rule.get("规则编号"))
        return None

    def _latest_job(self, target: str) -> dict[str, Any] | None:
        jobs = self._jobs.get(target) or []
        return jobs[-1] if jobs else None

    def target_state(self, target: str) -> dict[str, Any]:
        """汇总某个缓存目标的运行时状态，供规则列表/详情统一派生口径。"""
        with self._lock:
            rules = [
                rule
                for rule in store.rows(CACHE_MODULE)
                if str(rule.get("缓存目标") or "").strip() == target
            ]
            snapshots = list(self._snapshots.get(target, []))
            job = self._latest_job(target)
            active_rules = [rule for rule in rules if rule.get("status") == "已启用"]
            # 失效作业优先级最高：失败/待失效要显式暴露给下游，不能被停用结论盖掉；
            # 规则停用本身仍然是取数层面的最高优先级（simulate_hit 只认已启用规则）。
            if job and job["status"] == JOB_FAILED:
                cache_status = DOWNSTREAM_FAILED
            elif job and job["status"] == JOB_PENDING:
                cache_status = DOWNSTREAM_PENDING
            elif active_rules:
                cache_status = DOWNSTREAM_OK if snapshots else DOWNSTREAM_WARMING
            elif any(rule.get("status") == "已停用" for rule in rules):
                cache_status = DOWNSTREAM_DISABLED
            elif rules:
                # 规则还在草稿/待校验生命周期里，并非没有接入缓存。
                cache_status = DOWNSTREAM_INACTIVE
            else:
                cache_status = DOWNSTREAM_UNBOUND
            return {
                "target": target,
                "cacheStatus": cache_status,
                "snapshotCount": len(snapshots),
                "snapshots": snapshots,
                "latestJob": job,
                "jobs": list(self._jobs.get(target, [])),
                "ruleCount": len(rules),
                "activeRuleCount": len(active_rules),
            }

    def enrich_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        """给缓存规则挂上运行时信息，列表与详情走同一个函数，避免两处口径不一致。

        「缓存状态」反映这条规则自身的结论（停用即缓存已停用）；「目标缓存状态」
        反映整个缓存目标的派生口径，查询模板与数据服务同步的就是后者，同一目标
        下还存在其它启用规则时两个值可以不同，但各自语义明确、不会互相覆盖。
        """
        target = str(rule.get("缓存目标") or "").strip()
        if not target:
            rule["缓存状态"] = DOWNSTREAM_UNBOUND
            rule["目标缓存状态"] = DOWNSTREAM_UNBOUND
            rule["快照数量"] = 0
            rule["失效作业"] = None
            return rule
        state = self.target_state(target)
        rule["目标缓存状态"] = state["cacheStatus"]
        rule_status = str(rule.get("status") or "")
        if rule_status == "已停用":
            rule["缓存状态"] = DOWNSTREAM_DISABLED
        elif rule_status == "已启用":
            rule["缓存状态"] = state["cacheStatus"]
        else:
            rule["缓存状态"] = DOWNSTREAM_INACTIVE
        rule["快照数量"] = state["snapshotCount"]
        rule["失效作业"] = state["latestJob"]
        return rule

    # ------------------------------------------------------------------
    # 停用 → 失效 → 下游同步 的主链路
    # ------------------------------------------------------------------
    def invalidate_on_disable(self, rule: dict[str, Any]) -> dict[str, Any]:
        """停用规则时调用：登记失效作业、清快照、把待失效/失败状态同步给下游。

        调用方必须已经在持锁的状态流转里先把规则置为「已停用」；本方法与状态
        变更共用同一把锁，因此并发取数要么在停用前完成、要么看到停用后的结论。
        """
        target = str(rule.get("缓存目标") or "").strip()
        with self._lock:
            self._job_seq += 1
            job = {
                "jobId": self._job_seq,
                "target": target,
                "ruleCode": rule.get("规则编号"),
                "ruleId": rule.get("id"),
                "status": JOB_PENDING,
                "attempts": 0,
                "lastError": None,
                "createdAt": _now(),
                "updatedAt": _now(),
            }
            self._jobs.setdefault(target, []).append(job)
            # 先把「待失效」广播给下游，阻断“规则已停用、下游仍显示正常”的窗口。
            self._sync_downstream(target)
            self._run_invalidation(job)
            return dict(job)

    def _run_invalidation(self, job: dict[str, Any]) -> None:
        target = job["target"]
        job["attempts"] += 1
        job["updatedAt"] = _now()
        remaining = self._fail_budget.get(target, 0)
        if remaining > 0:
            # 模拟下游缓存节点抖动：本次失效失败，保留现场等待人工/自动重试。
            self._fail_budget[target] = remaining - 1
            job["status"] = JOB_FAILED
            job["lastError"] = f"缓存节点失效第 {job['attempts']} 次尝试未确认，请重试"
        else:
            removed = len(self._snapshots.pop(target, []))
            job["status"] = JOB_DONE
            job["lastError"] = None
            job["clearedSnapshots"] = removed
        self._sync_downstream(target)

    def retry_invalidation(self, job_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            for target, jobs in self._jobs.items():
                for job in jobs:
                    if int(job.get("jobId", 0)) != job_id:
                        continue
                    if job["status"] != JOB_FAILED:
                        return None, f"失效作业 {job_id} 当前为{job['status']}，无需重试"
                    job["status"] = JOB_PENDING
                    job["updatedAt"] = _now()
                    self._sync_downstream(target)
                    self._run_invalidation(job)
                    return dict(job), "失效作业已重试"
            return None, f"失效作业 {job_id} 不存在"

    def arm_failure(self, target: str, times: int = 1) -> None:
        """让某目标接下来的 times 次失效尝试失败，用于验证重试状态与重试接口。"""
        with self._lock:
            self._fail_budget[target] = self._fail_budget.get(target, 0) + max(times, 1)

    # ------------------------------------------------------------------
    # 下游同步：查询模板与数据服务的缓存状态以缓存规则列表为准
    # ------------------------------------------------------------------
    def _sync_downstream(self, target: str) -> None:
        state = self.target_state(target)
        for module in (QUERY_MODULE, SERVICE_MODULE):
            for row in store.rows(module):
                if str(row.get(TARGET_FIELD) or "").strip() != target:
                    continue
                row[CACHE_STATUS_FIELD] = state["cacheStatus"]

    def refresh_all_downstream(self) -> None:
        """全量重算下游缓存状态，保证任何入口看到的数据服务详情都与规则一致。"""
        with self._lock:
            targets = {
                str(rule.get("缓存目标") or "").strip()
                for rule in store.rows(CACHE_MODULE)
                if str(rule.get("缓存目标") or "").strip()
            }
            for module in (QUERY_MODULE, SERVICE_MODULE):
                for row in store.rows(module):
                    target = str(row.get(TARGET_FIELD) or "").strip()
                    row[CACHE_STATUS_FIELD] = (
                        self.target_state(target)["cacheStatus"]
                        if target in targets
                        else DOWNSTREAM_UNBOUND
                    )

    def downstream_view(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """下游列表/详情读取时实时派生缓存状态，避免展示旧状态。"""
        with self._lock:
            target = str(row.get(TARGET_FIELD) or "").strip()
            if not target:
                row[CACHE_STATUS_FIELD] = DOWNSTREAM_UNBOUND
                return row
            row[CACHE_STATUS_FIELD] = self.target_state(target)["cacheStatus"]
            return row

    # ------------------------------------------------------------------
    # 取数链路：查询模板/数据服务命中模拟
    # ------------------------------------------------------------------
    def simulate_hit(
        self,
        target: str,
        caller_module: str,
        caller_id: int | None = None,
    ) -> dict[str, Any]:
        """下游按缓存目标取数；停用结论优先，命中与否都写历史记录。"""
        with self._lock:
            target = target.strip()
            active = [
                rule
                for rule in store.rows(CACHE_MODULE)
                if str(rule.get("缓存目标") or "").strip() == target
                and rule.get("status") == "已启用"
            ]
            snapshots = self._snapshots.get(target, [])
            if not active:
                # 规则已停用（或不存在）：旧快照一律不可见，直接回源。
                result, snapshot = "未命中-规则已停用", None
            elif not snapshots:
                result, snapshot = "未命中-回源重建", None
                self._snapshot_seq += 1
                snapshot = {
                    "snapshotId": self._snapshot_seq,
                    "target": target,
                    "ruleCode": active[0].get("规则编号"),
                    "payload": f"{target}的缓存快照",
                    "createdAt": _now(),
                }
                self._snapshots.setdefault(target, []).append(snapshot)
            else:
                result, snapshot = "命中", snapshots[-1]
            self._hit_seq += 1
            record = {
                "hitId": self._hit_seq,
                "target": target,
                "callerModule": caller_module,
                "callerId": caller_id,
                "result": result,
                "ruleCode": active[0].get("规则编号") if active else self._rule_code_for_target(target),
                "snapshotId": snapshot.get("snapshotId") if snapshot else None,
                "time": _now(),
            }
            self._hits.append(record)
            return dict(record)

    def list_hits(self, target: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if target:
                target = target.strip()
                return [dict(hit) for hit in self._hits if hit["target"] == target]
            return [dict(hit) for hit in self._hits]

    def list_snapshots(self, target: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if target:
                target = target.strip()
                return [dict(item) for item in self._snapshots.get(target, [])]
            return [dict(item) for items in self._snapshots.values() for item in items]

    def list_jobs(self, target: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if target:
                target = target.strip()
                return [dict(job) for job in self._jobs.get(target, [])]
            return [dict(job) for jobs in self._jobs.values() for job in jobs]


cache_runtime = CacheRuntime()
