#!/usr/bin/env python3
"""v3.2.0: Key 池 + 粘性轮换（per-key 冷却 + 粘性分配）

背景：官方把每 key 的 RPM 改成 1，传统「失败切 key」轮换在 RPM=1 下基本失效
（key1 刚 429 → 几秒后切 key2 → key2 也 429，根本拿不到可用的 key）。
本模块实现「**每任务粘性 + 跨 key 轮询**」策略：
- 每个新任务从 pool 里 acquire 一把 key，整段生命周期（create + poll）都用这把
- 选 key 策略：**last_used 最早的**（保证 4 把 key 轮换均匀）
- per-key 冷却窗口：60s（= RPM=1 的 1 分钟窗口）
- 真死 key（401/403 auth 错）才换下一把；429 限流不换 key，等冷却完再试

设计取舍：
- **纯内存版**（v3.2.0）：进程退出就清零，多进程并发可能撞 RPM
  → 简单，主人用「看股票 + 偶尔发视频」节奏完全够用
- **磁盘版**（TODO v3.3+）：加 fcntl 文件锁，跨进程共享
  → 多并发跑才需要

可观测：
- acquire 时打印 [key-pool] selected sk-XXX*** (last_used 87s ago, of 4 keys)
- agent 模式输出会带 KEY: sk-XXX*** 字段，主人能看到哪个 key 跑完的
"""

from __future__ import annotations

import sys
import time
from typing import Optional


# 冷却窗口（秒）= RPM 倒数。官方 RPM=1 → 60s
DEFAULT_COOLDOWN_SEC = 60.0

# 死 key 隔离时长（秒）—— 401/403 后多久内不再选它
# 设成 10 分钟：让主人有时间发现/换 key，但不会永远黑名单
DEFAULT_DEAD_TTL_SEC = 600.0


def _key_fingerprint(key: str) -> str:
    """把 key 缩短到 sk-XXX***...*** 形式，方便日志/输出不泄漏完整 key

    取前缀 sk- 后 3 个字符 + 后 2 个字符，中间 ***。
    例：sk-GyuVHxxxxxYx1P → sk-Gyu***1P
    """
    if not key or len(key) < 8:
        return "sk-***"
    # 跳过 "sk-" 前缀
    if key.startswith("sk-"):
        prefix = "sk-"
        rest = key[3:]
    else:
        prefix = ""
        rest = key
    if len(rest) < 6:
        return f"{prefix}{rest[:2]}***"
    return f"{prefix}{rest[:3]}***{rest[-2:]}"


class KeyPool:
    """Key 池：粘性 + 冷却 + 死 key 隔离

    使用示例：
        pool = KeyPool(keys=["sk-a", "sk-b", "sk-c", "sk-d"])
        key = pool.acquire_key()       # 选 last_used 最早的那把
        try:
            response = request_with(key)
            pool.mark_used(key)         # 成功后登记
        except KeyDeadError:
            pool.mark_dead(key)        # 401/403 → 黑名单 10 分钟
            # 上层会再次 acquire 拿下一把
        except ApiError as e:
            if is_rate_limit(e):
                # 429 → 不换 key，等冷却；mark_used 也不调（让 last_used 不变）
                # 实际上 mark_used 应该在成功后调
                pass
    """

    def __init__(
        self,
        keys: list[str],
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        dead_ttl_sec: float = DEFAULT_DEAD_TTL_SEC,
        clock: Optional[callable] = None,  # 测试用：注入 time.monotonic
    ):
        if not keys:
            raise ValueError("KeyPool requires at least 1 key")
        # 去重保序
        seen: set = set()
        deduped: list = []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                deduped.append(k)
        self._keys: list[str] = deduped
        self._cooldown_sec = float(cooldown_sec)
        self._dead_ttl_sec = float(dead_ttl_sec)
        # per-key 状态
        # last_used[key] = 最后一次成功用过的 monotonic 时间；0 = 从未用过（优先选）
        # dead_until[key] = 死到什么时候（401/403 后设）；0 = 还活着
        self._last_used: dict[str, float] = {k: 0.0 for k in self._keys}
        self._dead_until: dict[str, float] = {k: 0.0 for k in self._keys}
        # 时钟（测试可注入）
        self._clock = clock or time.monotonic

    # ========================================================================
    # Public API
    # ========================================================================

    def acquire_key(self, verbose: bool = True) -> str:
        """选一把 key：按 last_used 升序，过滤掉冷却中 / 死掉的 key

        选 key 优先级（高 → 低）：
        1. 从未用过（last_used == 0）—— 全新 key
        2. 最早用过 + 已过冷却窗口 —— 旧 key 现在可以再用
        3. 早用过 + 还在冷却 —— 兜底选它（让上层拿到 429 自己等）

        Args:
            verbose: 是否打印 [key-pool] 日志（默认 True；测试时可设 False）

        Returns:
            选中的 key

        Note:
            单 key 池永远返回同一把（向后兼容）
        """
        now = self._clock()

        # 1. 优先选：从未用过 OR (已过冷却 + 没死)
        best_key: Optional[str] = None
        best_last_used: float = float("inf")
        for k in self._keys:
            if self._is_dead(k, now):
                continue
            lu = self._last_used[k]
            if lu == 0.0:
                # 从未用过，立刻选
                best_key = k
                best_last_used = 0.0
                break
            if (now - lu) >= self._cooldown_sec:
                # 已过冷却，记录候选
                if lu < best_last_used:
                    best_key = k
                    best_last_used = lu

        if best_key is None:
            # 2. 兜底：所有 key 都在冷却/死了 → 选最久没用的（让上层撞 429 自己退避）
            best_key = min(self._keys, key=lambda k: self._last_used.get(k, 0.0))

        if verbose:
            fp = _key_fingerprint(best_key)
            lu = self._last_used.get(best_key, 0.0)
            if lu == 0.0:
                ago = "never used"
            else:
                ago = f"last_used {now - lu:.0f}s ago"
            print(
                f"# [key-pool] selected {fp} ({ago}, of {len(self._keys)} keys)",
                file=sys.stderr,
            )
        return best_key

    def mark_used(self, key: str) -> None:
        """标记 key 已被使用（成功 / 进入限流都调）→ 60s 冷却开始

        注意：429 时虽然请求失败，但**不换 key**（换 key 也撞 RPM），所以也要
        mark_used，让这把 key 进冷却，下个任务就不会立刻又选中它。
        """
        if key not in self._last_used:
            return
        self._last_used[key] = self._clock()

    def mark_dead(self, key: str, verbose: bool = True) -> None:
        """标记 key 为死 key（401/403 鉴权失败）→ dead_ttl 内不再选它

        区别于 mark_used：
        - mark_dead 是「这把 key 不能用了」，换下一把
        - mark_used 是「这把 key 刚用过了」，等冷却
        """
        if key not in self._dead_until:
            return
        self._dead_until[key] = self._clock() + self._dead_ttl_sec
        if verbose:
            fp = _key_fingerprint(key)
            print(
                f"# [key-pool] marked {fp} as DEAD for {self._dead_ttl_sec:.0f}s "
                f"(401/403 quota, will skip until cooldown)",
                file=sys.stderr,
            )

    def is_dead(self, key: str) -> bool:
        """查询 key 当前是否在死状态（调试/测试用）"""
        return self._is_dead(key, self._clock())

    def snapshot(self) -> dict:
        """返回 pool 当前状态快照（调试/agent 输出用）

        Returns:
            {
              "total_keys": 4,
              "alive_keys": 3,
              "dead_keys": 1,
              "cooldown_active": ["sk-***"],
              "keys": [
                {"fp": "sk-***", "last_used_ago_s": 42, "dead_for_s": 0},
                ...
              ]
            }
        """
        now = self._clock()
        out = {
            "total_keys": len(self._keys),
            "cooldown_sec": self._cooldown_sec,
            "dead_ttl_sec": self._dead_ttl_sec,
            "keys": [],
        }
        alive = 0
        dead = 0
        cooldown_active: list[str] = []
        for k in self._keys:
            fp = _key_fingerprint(k)
            lu = self._last_used[k]
            du = self._dead_until[k]
            last_used_ago = -1 if lu == 0.0 else (now - lu)
            dead_for = max(0, du - now) if du > 0 else 0
            is_dead = dead_for > 0
            is_in_cooldown = (lu > 0) and (last_used_ago < self._cooldown_sec)
            if is_dead:
                dead += 1
            else:
                alive += 1
            if is_in_cooldown and not is_dead:
                cooldown_active.append(fp)
            out["keys"].append({
                "fp": fp,
                "last_used_ago_s": round(last_used_ago, 1) if last_used_ago >= 0 else None,
                "dead_for_s": round(dead_for, 1) if dead_for > 0 else 0,
                "in_cooldown": is_in_cooldown,
                "is_dead": is_dead,
            })
        out["alive_keys"] = alive
        out["dead_keys"] = dead
        out["cooldown_active"] = cooldown_active
        return out

    # ========================================================================
    # Private
    # ========================================================================

    def _is_dead(self, key: str, now: float) -> bool:
        du = self._dead_until.get(key, 0.0)
        return du > now

    def __len__(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return f"KeyPool({len(self._keys)} keys, cooldown={self._cooldown_sec}s)"
