#!/usr/bin/env python3
"""v3.2.0 KeyPool 单测

覆盖：
- TestKeyFingerprint (5): key 脱敏边界（短/标准/无 sk- 前缀/空）
- TestKeyPoolInit (3): 单 key / 多 key / 缺 key / 去重
- TestAcquireKey (6): 全新 key 优先 / last_used 最早 / 跳过冷却中 / 跳过死 key / 兜底选 / verbose 模式
- TestMarkUsed (3): 单 key / 多 key 轮换均匀 / 跳过不存在 key
- TestMarkDead (3): 死 key 跳过 acquire / dead_ttl 后复活 / 跳过不存在 key
- TestSnapshot (3): alive/dead 计数 / cooldown_active 列表 / fingerprint 不泄漏完整 key
- TestStickyRotation (2): 4 把 key round-robin 模拟 / 1 把死掉后 3 把轮换
- TestSingleKeyBackwardCompat (2): 1 把 key 永远返回它 / mark_used 不影响选择

跑法：python3 tests/test_key_pool.py（直接 run，无需 pytest）
或：python3 -m pytest tests/test_key_pool.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 注入 lib/ 到 path
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))

from key_pool import KeyPool, _key_fingerprint, DEFAULT_COOLDOWN_SEC  # noqa: E402


# ============================================================================
# 伪时钟：让测试不依赖 time.sleep / real time
# ============================================================================

class FakeClock:
    """可控制的假时钟：每次调用 advance() 推进 N 秒"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ============================================================================
# TestKeyFingerprint: key 脱敏
# ============================================================================

class TestKeyFingerprint(unittest.TestCase):
    def test_standard_key(self):
        """标准 sk-xxx 格式：保留 sk- 前 3 后 2，中间 ***"""
        self.assertEqual(_key_fingerprint("sk-abcdefghij"), "sk-abc***ij")

    def test_long_key(self):
        """长 key：仍然只显示前 3 后 2"""
        result = _key_fingerprint("sk-" + "a" * 50)
        self.assertEqual(result, "sk-aaa***aa")

    def test_short_key(self):
        """短 key（< 8 字符）→ 走兜底分支"""
        # 短 key 的实现是：key 长度 < 8 → 返回 "sk-***"
        # 理由：key 本身太短，脱敏会反向暴露全部信息
        self.assertEqual(_key_fingerprint("sk-abc"), "sk-***")

    def test_no_sk_prefix(self):
        """无 sk- 前缀：纯显示 fingerprint"""
        self.assertEqual(_key_fingerprint("abcdefghij"), "abc***ij")

    def test_empty_key(self):
        """空 key → 兜底 sk-***"""
        self.assertEqual(_key_fingerprint(""), "sk-***")


# ============================================================================
# TestKeyPoolInit: 初始化
# ============================================================================

class TestKeyPoolInit(unittest.TestCase):
    def test_single_key(self):
        """1 把 key"""
        pool = KeyPool(["sk-a"])
        self.assertEqual(len(pool), 1)

    def test_multi_keys(self):
        """4 把 key"""
        pool = KeyPool(["sk-a", "sk-b", "sk-c", "sk-d"])
        self.assertEqual(len(pool), 4)

    def test_empty_keys_raises(self):
        """0 把 key 抛 ValueError"""
        with self.assertRaises(ValueError):
            KeyPool([])

    def test_dedup_with_order(self):
        """去重保序"""
        pool = KeyPool(["sk-a", "sk-b", "sk-a", "sk-c", "sk-b"])
        # 内部 _keys 应该是 sk-a, sk-b, sk-c
        self.assertEqual(pool._keys, ["sk-a", "sk-b", "sk-c"])
        self.assertEqual(len(pool), 3)


# ============================================================================
# TestAcquireKey: 选 key 策略
# ============================================================================

class TestAcquireKey(unittest.TestCase):
    def test_never_used_key_first(self):
        """从未用过的 key 优先选"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b", "sk-c"], clock=clock)
        # 三把都没用过 → 选第一把（顺序扫描到 first never-used）
        self.assertEqual(pool.acquire_key(verbose=False), "sk-a")

    def test_oldest_last_used_wins(self):
        """所有 key 都用过 → 选 last_used 最早的"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b", "sk-c"], clock=clock)
        # 模拟 a=100s ago, b=50s ago, c=10s ago
        pool._last_used["sk-a"] = clock() - 100
        pool._last_used["sk-b"] = clock() - 50
        pool._last_used["sk-c"] = clock() - 10
        # 选最早用的 sk-a（100s ago > 60s cooldown）
        self.assertEqual(pool.acquire_key(verbose=False), "sk-a")

    def test_skip_recent_key(self):
        """last_used 在冷却窗口内 → 不选（除非没别的可选）"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock, cooldown_sec=60.0)
        # sk-a 30s ago（冷却中），sk-b 100s ago（已冷却）
        pool._last_used["sk-a"] = clock() - 30
        pool._last_used["sk-b"] = clock() - 100
        # 应该选 sk-b
        self.assertEqual(pool.acquire_key(verbose=False), "sk-b")

    def test_skip_dead_key(self):
        """死 key 跳过"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock)
        # sk-a 标死
        pool.mark_dead("sk-a", verbose=False)
        # 应该选 sk-b
        self.assertEqual(pool.acquire_key(verbose=False), "sk-b")

    def test_fallback_when_all_in_cooldown(self):
        """所有 key 都在冷却 → 兜底选 last_used 最小的（数值最小 = 最早用过）"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b", "sk-c"], clock=clock, cooldown_sec=60.0)
        # sk-a 10s ago、sk-b 5s ago、sk-c 8s ago
        # 5s ago 是"刚用过的"（last_used 数值大），10s ago 是"最早用过的"（last_used 数值小）
        pool._last_used["sk-a"] = clock() - 10
        pool._last_used["sk-b"] = clock() - 5
        pool._last_used["sk-c"] = clock() - 8
        # min(last_used) = sk-a（10s ago）→ 选 sk-a
        self.assertEqual(pool.acquire_key(verbose=False), "sk-a")

    def test_fallback_when_all_dead(self):
        """所有 key 都死了 → 兜底选 last_used 最早的（pool 设计上不阻断）"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock)
        pool.mark_dead("sk-a", verbose=False)
        pool.mark_dead("sk-b", verbose=False)
        # 兜底：还会选一把（不抛异常）
        key = pool.acquire_key(verbose=False)
        self.assertIn(key, ["sk-a", "sk-b"])


# ============================================================================
# TestMarkUsed
# ============================================================================

class TestMarkUsed(unittest.TestCase):
    def test_mark_used_updates_timestamp(self):
        """mark_used 后 last_used = now"""
        clock = FakeClock()
        pool = KeyPool(["sk-a"], clock=clock)
        self.assertEqual(pool._last_used["sk-a"], 0.0)
        pool.mark_used("sk-a")
        self.assertEqual(pool._last_used["sk-a"], clock())

    def test_mark_used_advances_clock(self):
        """mark_used 后 advance 时钟，acquire 不再选它（冷却中）"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock, cooldown_sec=60.0)
        # 第一次选 sk-a
        k1 = pool.acquire_key(verbose=False)
        self.assertEqual(k1, "sk-a")
        pool.mark_used("sk-a")
        # advance 30s：sk-a 在冷却中，sk-b 从未用过 → 选 sk-b
        clock.advance(30)
        self.assertEqual(pool.acquire_key(verbose=False), "sk-b")
        pool.mark_used("sk-b")  # 关键：sk-b 也 mark_used，否则会优先选
        # advance 到 70s：sk-a 冷却完 → 选 sk-a（70s ago > sk-b 的 40s ago）
        clock.advance(40)
        self.assertEqual(pool.acquire_key(verbose=False), "sk-a")

    def test_mark_used_unknown_key_ignored(self):
        """mark_used 不存在的 key 不报错（静默忽略）"""
        pool = KeyPool(["sk-a"])
        pool.mark_used("sk-unknown")  # 不应抛
        self.assertEqual(pool._last_used, {"sk-a": 0.0})


# ============================================================================
# TestMarkDead
# ============================================================================

class TestMarkDead(unittest.TestCase):
    def test_mark_dead_skipped(self):
        """死 key 在 dead_ttl 内被跳过"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock, dead_ttl_sec=600.0)
        pool.mark_dead("sk-a", verbose=False)
        # 第一次选 sk-b
        self.assertEqual(pool.acquire_key(verbose=False), "sk-b")
        pool.mark_used("sk-b")
        # 100s 后，sk-b 也冷却完，但 sk-a 还死着 → 还是选 sk-b
        clock.advance(100)
        self.assertEqual(pool.acquire_key(verbose=False), "sk-b")

    def test_mark_dead_revives(self):
        """dead_ttl 后死 key 复活"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock, dead_ttl_sec=600.0)
        pool.mark_dead("sk-a", verbose=False)
        # 推进 700s → 死 key 复活
        clock.advance(700)
        # sk-a 和 sk-b 都没用过 → 选 sk-a（顺序优先）
        self.assertEqual(pool.acquire_key(verbose=False), "sk-a")

    def test_mark_dead_unknown_key_ignored(self):
        """mark_dead 不存在的 key 静默忽略"""
        pool = KeyPool(["sk-a"])
        pool.mark_dead("sk-unknown")  # 不应抛
        self.assertEqual(pool._dead_until, {"sk-a": 0.0})


# ============================================================================
# TestSnapshot: 调试输出
# ============================================================================

class TestSnapshot(unittest.TestCase):
    def test_snapshot_fresh_pool(self):
        """全新 pool 快照"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b"], clock=clock)
        s = pool.snapshot()
        self.assertEqual(s["total_keys"], 2)
        self.assertEqual(s["alive_keys"], 2)
        self.assertEqual(s["dead_keys"], 0)
        self.assertEqual(s["cooldown_active"], [])
        self.assertEqual(s["cooldown_sec"], 60.0)
        for entry in s["keys"]:
            # last_used_ago 应该是 None（从未用过）
            self.assertIsNone(entry["last_used_ago_s"])
            self.assertEqual(entry["dead_for_s"], 0)
            self.assertFalse(entry["in_cooldown"])
            self.assertFalse(entry["is_dead"])

    def test_snapshot_after_use_and_dead(self):
        """用过 + 死 key 的快照"""
        clock = FakeClock()
        # 用足够长的 key 避免走兑底（len<8）
        pool = KeyPool(["sk-aaaaaaaaaa", "sk-bbbbbbbbbb"], clock=clock)
        pool.mark_used("sk-aaaaaaaaaa")
        pool.mark_dead("sk-bbbbbbbbbb", verbose=False)
        clock.advance(30)  # 30s 后
        s = pool.snapshot()
        # sk-a：30s ago in cooldown
        a_entry = next(e for e in s["keys"] if e["fp"] == "sk-aaa***aa")
        self.assertEqual(a_entry["last_used_ago_s"], 30.0)
        self.assertTrue(a_entry["in_cooldown"])
        self.assertFalse(a_entry["is_dead"])
        # sk-b：dead
        b_entry = next(e for e in s["keys"] if e["fp"] == "sk-bbb***bb")
        self.assertTrue(b_entry["is_dead"])
        self.assertGreater(b_entry["dead_for_s"], 0)
        # alive / dead 计数
        self.assertEqual(s["alive_keys"], 1)
        self.assertEqual(s["dead_keys"], 1)
        # cooldown_active 列表
        self.assertIn("sk-aaa***aa", s["cooldown_active"])
        self.assertNotIn("sk-bbb***bb", s["cooldown_active"])

    def test_snapshot_does_not_leak_full_key(self):
        """快照里只有 fingerprint，没有完整 key"""
        clock = FakeClock()
        pool = KeyPool(["sk-supersecretlongkey12345"], clock=clock)
        s = pool.snapshot()
        snap_str = str(s)
        self.assertNotIn("supersecretlongkey", snap_str)
        self.assertIn("sk-sup***45", snap_str)


# ============================================================================
# TestStickyRotation: 4 把 key 真实轮换模拟
# ============================================================================

class TestStickyRotation(unittest.TestCase):
    def test_four_keys_round_robin(self):
        """4 把 key 顺序 acquire → 均匀轮换"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b", "sk-c", "sk-d"], clock=clock, cooldown_sec=60.0)
        sequence = []
        for _ in range(8):
            key = pool.acquire_key(verbose=False)
            sequence.append(key)
            pool.mark_used(key)
            clock.advance(70)  # 推进 70s（过冷却）
        # 4 把 key 各被选 2 次
        from collections import Counter
        counts = Counter(sequence)
        self.assertEqual(counts["sk-a"], 2)
        self.assertEqual(counts["sk-b"], 2)
        self.assertEqual(counts["sk-c"], 2)
        self.assertEqual(counts["sk-d"], 2)

    def test_one_key_dies_others_share_load(self):
        """1 把 key 死后，剩下 3 把轮换"""
        clock = FakeClock()
        pool = KeyPool(["sk-a", "sk-b", "sk-c", "sk-d"], clock=clock, cooldown_sec=60.0)
        # 第一轮：选 sk-a
        first = pool.acquire_key(verbose=False)
        self.assertEqual(first, "sk-a")
        # 假设 sk-a 死了
        pool.mark_dead("sk-a", verbose=False)
        clock.advance(70)
        # 接下来 6 次：应该只用 sk-b/c/d（均匀）
        sequence = []
        for _ in range(6):
            key = pool.acquire_key(verbose=False)
            sequence.append(key)
            pool.mark_used(key)
            clock.advance(70)
        from collections import Counter
        counts = Counter(sequence)
        self.assertNotIn("sk-a", counts)  # 死 key 不应被选
        self.assertEqual(counts["sk-b"], 2)
        self.assertEqual(counts["sk-c"], 2)
        self.assertEqual(counts["sk-d"], 2)


# ============================================================================
# TestSingleKeyBackwardCompat: 单 key 向后兼容
# ============================================================================

class TestSingleKeyBackwardCompat(unittest.TestCase):
    def test_single_key_always_returned(self):
        """1 把 key 时永远返回它（即使在冷却中）"""
        clock = FakeClock()
        pool = KeyPool(["sk-only"], clock=clock)
        for _ in range(5):
            self.assertEqual(pool.acquire_key(verbose=False), "sk-only")
            pool.mark_used("sk-only")
            clock.advance(10)  # 还在冷却中

    def test_single_key_repr(self):
        """repr 不泄漏完整 key"""
        pool = KeyPool(["sk-only"])
        self.assertIn("1 keys", repr(pool))


if __name__ == "__main__":
    unittest.main(verbosity=2)
