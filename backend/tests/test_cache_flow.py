import unittest

from app.runtime.cache_store import (
    JOB_DONE,
    JOB_FAILED,
    QUERY_MODULE,
    SERVICE_MODULE,
    CacheRuntime,
)
from app.services.cache import CacheService
from app.services.query import QueryService
from app.services.service import ServiceService
from app.store import Store

TARGET = "查询模板-地区月报"
TARGET2 = "数据服务-销售概览"


class CacheFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用全新的内存仓库与运行时，避免单例状态串用例。
        import app.runtime.cache_store as runtime_mod
        import app.services.cache as cache_svc_mod
        import app.services.query as query_svc_mod
        import app.services.service as service_svc_mod

        self.store = Store()
        runtime_mod.cache_runtime = CacheRuntime()
        cache_svc_mod.cache_runtime = runtime_mod.cache_runtime
        query_svc_mod.cache_runtime = runtime_mod.cache_runtime
        service_svc_mod.cache_runtime = runtime_mod.cache_runtime
        # 让运行时与所有服务共用同一份替换后的仓库。
        runtime_mod.store = self.store
        for module in (cache_svc_mod, query_svc_mod, service_svc_mod):
            module.store = self.store
        self.runtime = runtime_mod.cache_runtime
        self.runtime.bootstrap()
        self.runtime.refresh_all_downstream()
        self.cs = CacheService()
        self.qs = QueryService()
        self.ss = ServiceService()

    def test_search_by_cache_target_finds_two_rules(self) -> None:
        """按缓存目标检索必须命中该目标下的两条规则（根因：原来只看规则编号）。"""
        rows, total = self.cs.list_entries(target=TARGET)
        self.assertEqual(total, 2)
        self.assertEqual({row["规则编号"] for row in rows}, {"CACH-0001", "CACH-0002"})
        # 规则编号检索口径保持可用。
        _, total = self.cs.list_entries(keyword="CACH-0001")
        self.assertEqual(total, 1)
        # 不存在的目标返回空列表而不是报错。
        _, total = self.cs.list_entries(target="不存在的目标")
        self.assertEqual(total, 0)

    def test_state_machine_only_one_step_and_disable_is_terminal(self) -> None:
        # 草稿不能跳步停用
        entry, _ = self.cs.run_action(1, "停用配置")
        self.assertIsNone(entry)
        # 正常一步一流转
        entry, _ = self.cs.run_action(1, "提交校验")
        self.assertEqual(entry["status"], "待校验")
        entry, _ = self.cs.run_action(1, "启用配置")
        self.assertEqual(entry["status"], "已启用")
        # 已启用只能停用
        entry, _ = self.cs.run_action(1, "提交校验")
        self.assertIsNone(entry)
        entry, _ = self.cs.run_action(1, "停用配置")
        self.assertEqual(entry["status"], "已停用")
        # 停用后不能回到启用，恢复需重新走生命周期
        entry, _ = self.cs.run_action(1, "启用配置")
        self.assertIsNone(entry)

    def test_disable_clears_snapshot_and_blocks_downstream_hits(self) -> None:
        # 停用前可以命中旧快照
        hit = self.runtime.simulate_hit(TARGET, QUERY_MODULE, 4)
        self.assertEqual(hit["result"], "命中")
        # 停用 CACH-0002
        entry, message = self.cs.run_action(2, "停用配置")
        self.assertEqual(entry["status"], "已停用")
        self.assertIn("旧快照已清除", message)
        # 旧快照被清除
        self.assertEqual(self.runtime.list_snapshots(TARGET), [])
        # 并发取数以停用结论为准，不再命中旧快照
        hit = self.runtime.simulate_hit(TARGET, SERVICE_MODULE, 4)
        self.assertTrue(hit["result"].startswith("未命中"))
        # 查询模板与数据服务缓存状态同步为缓存已停用
        self.assertEqual(self.qs.get_entry(4)["缓存状态"], "缓存已停用")
        self.assertEqual(self.ss.get_entry(4)["缓存状态"], "缓存已停用")
        # 列表与详情口径一致
        self.assertEqual(self.cs.get_entry(2)["缓存状态"], "缓存已停用")
        self.assertEqual(self.cs.list_entries(keyword="CACH-0002")[0][0]["缓存状态"], "缓存已停用")

    def test_hit_history_is_retained_after_invalidation(self) -> None:
        self.runtime.simulate_hit(TARGET, QUERY_MODULE, 4)
        before = len(self.runtime.list_hits(TARGET))
        self.cs.run_action(2, "停用配置")
        self.runtime.simulate_hit(TARGET, QUERY_MODULE, 4)
        after = self.runtime.list_hits(TARGET)
        self.assertGreaterEqual(len(after), before + 1)
        self.assertTrue(any(hit["result"].startswith("未命中") for hit in after))

    def test_invalidation_failure_shows_retry_and_can_recover(self) -> None:
        self.runtime.arm_failure(TARGET2, 1)
        entry, message = self.cs.run_action(3, "停用配置")
        self.assertEqual(entry["status"], "已停用")
        job = entry["失效作业"]
        self.assertEqual(job["status"], JOB_FAILED)
        self.assertEqual(job["attempts"], 1)
        self.assertIn("待重试", message)
        # 下游同步为失效失败-待重试，但取数仍以停用结论为准
        self.assertEqual(self.ss.get_entry(5)["缓存状态"], "失效失败-待重试")
        self.assertTrue(
            self.runtime.simulate_hit(TARGET2, SERVICE_MODULE, 5)["result"].startswith("未命中")
        )
        # 重试成功：作业已失效、快照清除、下游回到与规则一致的停用口径
        retried, message = self.runtime.retry_invalidation(job["jobId"])
        self.assertEqual(retried["status"], JOB_DONE)
        self.assertIn("已重试", message)
        self.assertEqual(self.runtime.list_snapshots(TARGET2), [])
        self.assertEqual(self.ss.get_entry(5)["缓存状态"], "缓存已停用")
        # 对已失效作业再次重试会被拦下
        again, _ = self.runtime.retry_invalidation(job["jobId"])
        self.assertIsNone(again)

    def test_rule_level_and_target_level_status_diverge_cleanly(self) -> None:
        """同目标两条规则：停一条后该规则显示已停用，重新启用另一条后目标恢复正常。"""
        self.cs.run_action(2, "停用配置")
        rule2 = self.cs.get_entry(2)
        self.assertEqual(rule2["缓存状态"], "缓存已停用")
        self.assertEqual(rule2["目标缓存状态"], "缓存已停用")
        # 同目标的草稿规则走完生命周期重新启用
        self.cs.run_action(1, "提交校验")
        self.cs.run_action(1, "启用配置")
        rule2 = self.cs.get_entry(2)
        rule1 = self.cs.get_entry(1)
        self.assertEqual(rule2["缓存状态"], "缓存已停用")  # 停用结论不变
        self.assertEqual(rule1["目标缓存状态"], "待生效")  # 规则已启用但快照尚未重建
        # 首次取数回源重建快照后，目标口径恢复缓存正常
        self.runtime.simulate_hit(TARGET, QUERY_MODULE, 4)
        rule1 = self.cs.get_entry(1)
        self.assertEqual(rule1["目标缓存状态"], "缓存正常")
        # 下游跟目标口径走
        self.assertEqual(self.qs.get_entry(4)["缓存状态"], "缓存正常")

    def test_create_rule_requires_consistent_target(self) -> None:
        entry, _ = self.cs.create_entry({"规则编号": "CACH-0002", "缓存目标": TARGET, "缓存时间": "2026-10-01"})
        self.assertIsNone(entry)  # 规则编号重复
        entry, error = self.cs.create_entry(
            {"规则编号": "CACH-0009", "缓存目标": TARGET2, "缓存时间": "2026-10-01"}
        )
        self.assertIsNone(entry)  # 目标已归属别的规则编号
        self.assertIn("缓存目标与规则编号必须一致", error)
        entry, _ = self.cs.create_entry(
            {"规则编号": "CACH-0009", "缓存目标": "查询模板-新报表", "缓存时间": "2026-10-01"}
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "草稿")


if __name__ == "__main__":
    unittest.main()
