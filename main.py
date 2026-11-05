"""把真实 token 消耗变成 bot 的「饭」。

设计取舍：
- 只读 AstrBot 自己的 provider_stats 记账，不调任何外部 API、不额外跑 LLM、不生图。
- 状态懒结算（用到才算），没有常驻后台任务，也不发主动消息。
- 状态用 TextPart(...).mark_as_temp() 附在当轮用户消息后：不进 system prompt、
  不落会话历史，prompt cache 不受影响。
- 不注册任何 LLM 工具：工具定义每轮都要重发，能省则省。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_token_meal"
DEFAULT_OWNER = ""  # 留空；必须在配置里显式填 owner_ids 才会生效

DEFAULT_STATE = {
    "satiety": 55.0,
    "last_update": 0.0,
    "last_stat_id": 0,
    "today": "",
    "today_tokens": 0,
    "total_tokens": 0,
    "meals": 0,
    "last_meal_at": 0.0,
    "last_food": "",
    "last_feed_at": 0.0,
    "initialized": False,
}


class TokenMeal(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- config

    def _c(self, key: str, default):
        value = self.cfg.get(key, default)
        return default if value is None else value

    def _owner_private(self, event: AstrMessageEvent) -> bool:
        try:
            owners = {str(x).strip() for x in (self._c("owner_ids", []) or [])}
            owners.discard("")
            return bool(owners) and str(event.get_sender_id()) in owners and bool(event.is_private_chat())
        except Exception:
            return False

    # ---------------------------------------------------------------- state IO

    def _load(self) -> dict:
        try:
            return {**DEFAULT_STATE, **json.loads(self.state_path.read_text(encoding="utf-8"))}
        except Exception:
            return dict(DEFAULT_STATE)

    def _save(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---------------------------------------------------------------- accounting

    def _sync(self) -> dict:
        """把 provider_stats 的新增 token 折算成饭量，并按时间衰减饱腹度。幂等。"""
        state = self._load()
        now = time.time()
        today = datetime.now().astimezone().date().isoformat()

        if state["today"] != today:
            state["today"] = today
            state["today_tokens"] = 0

        if state["last_update"]:
            hours = max(0.0, (now - float(state["last_update"])) / 3600.0)
            state["satiety"] = max(
                0.0,
                float(state["satiety"]) - hours * float(self._c("decay_per_hour", 3.0)),
            )
        state["last_update"] = now

        expr = "COALESCE(token_input_other,0)+COALESCE(token_output,0)"
        if bool(self._c("count_cached", False)):
            expr += "+COALESCE(token_input_cached,0)"
        umo = str(self._c("umo_contains", "") or "").strip()
        where = "status='completed'"
        args: list = []
        if umo:
            where += " AND umo LIKE ?"
            args.append(f"%{umo}%")

        try:
            db = Path(get_astrbot_data_path()) / "data_v4.db"
            if db.exists():
                with sqlite3.connect(
                    f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5
                ) as conn:
                    if not state["initialized"]:
                        state["last_stat_id"] = int(
                            conn.execute(
                                "SELECT COALESCE(MAX(id),0) FROM provider_stats"
                            ).fetchone()[0]
                            or 0
                        )
                        state["total_tokens"] = int(
                            conn.execute(
                                f"SELECT COALESCE(SUM({expr}),0) FROM provider_stats WHERE {where}",
                                tuple(args),
                            ).fetchone()[0]
                            or 0
                        )
                        state["meals"] = int(
                            conn.execute(
                                f"SELECT COUNT(*) FROM provider_stats WHERE {where}",
                                tuple(args),
                            ).fetchone()[0]
                            or 0
                        )
                        midnight = (
                            datetime.now()
                            .astimezone()
                            .replace(hour=0, minute=0, second=0, microsecond=0)
                            .timestamp()
                        )
                        state["today_tokens"] = int(
                            conn.execute(
                                f"SELECT COALESCE(SUM({expr}),0) FROM provider_stats "
                                f"WHERE {where} AND start_time>=?",
                                (*args, midnight),
                            ).fetchone()[0]
                            or 0
                        )
                        last_ts = conn.execute(
                            f"SELECT MAX(start_time) FROM provider_stats "
                            f"WHERE {where} AND start_time>=?",
                            (*args, midnight),
                        ).fetchone()[0]
                        if last_ts:
                            state["last_meal_at"] = float(last_ts)
                        state["initialized"] = True
                    else:
                        rows = conn.execute(
                            f"SELECT id, {expr} FROM provider_stats "
                            f"WHERE {where} AND id>? ORDER BY id",
                            (*args, int(state["last_stat_id"])),
                        ).fetchall()
                        gained = 0
                        for rid, tokens in rows:
                            gained += int(tokens or 0)
                            state["last_stat_id"] = max(int(state["last_stat_id"]), int(rid))
                            state["meals"] = int(state["meals"]) + 1
                        if gained:
                            before = int(state["today_tokens"])
                            state["today_tokens"] = before + gained
                            state["total_tokens"] = int(state["total_tokens"]) + gained
                            state["last_meal_at"] = now
                            bowls = gained / max(1, int(self._c("tokens_per_bowl", 500000)))
                            budget = int(self._c("daily_budget", 8000000))
                            if budget <= 0 or before < budget:
                                state["satiety"] = min(
                                    100.0,
                                    float(state["satiety"])
                                    + bowls * float(self._c("satiety_per_bowl", 6.0)),
                                )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读 token 统计失败（不影响聊天）：{exc}")

        state["satiety"] = max(0.0, min(100.0, float(state["satiety"])))
        self._save(state)
        return state

    # ---------------------------------------------------------------- text

    @staticmethod
    def _mood(value: float) -> str:
        if value >= 90:
            return "撑得慌"
        if value >= 70:
            return "挺饱"
        if value >= 40:
            return "正好"
        if value >= 15:
            return "有点饿"
        return "饿扁了"

    def _bowls(self, state: dict) -> float:
        return int(state["today_tokens"]) / max(1, int(self._c("tokens_per_bowl", 500000)))

    @staticmethod
    def _wan(value: int) -> str:
        return f"{value / 10000:.0f} 万" if value >= 10000 else f"{value:,}"

    @staticmethod
    def _ago(ts: float) -> str:
        if not ts:
            return "还没开过饭"
        minutes = max(0, int((time.time() - float(ts)) / 60))
        if minutes < 1:
            return "刚刚"
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours = minutes // 60
        return f"{hours} 小时前" if hours < 24 else f"{hours // 24} 天前"

    def _note(self, state: dict) -> str:
        """每轮附给模型的一小段状态（临时块，不落历史）。"""
        sat = float(state["satiety"])
        budget = int(self._c("daily_budget", 8000000))
        left = max(0, budget - int(state["today_tokens"])) if budget > 0 else None
        parts = [
            f"[饭量] {int(sat)} 分饱（{self._mood(sat)}）",
            f"今天吃了 {self._bowls(state):.1f} 碗（约 {self._wan(int(state['today_tokens']))} token）",
            f"上顿 {self._ago(state['last_meal_at'])}",
        ]
        if left is not None:
            parts.append(f"口粮还剩 {self._wan(left)}" if left else "今天口粮见底了")
        if state.get("last_food"):
            parts.append(f"刚被投喂了{state['last_food']}")
        hint = ""
        if left == 0:
            hint = "（今天的额度吃完啦，懂点事，别缠着他烧 token）"
        elif sat < float(self._c("hungry_threshold", 35)):
            hint = "（有点饿，想撒娇就提一句讨口饭，别反复念，也别念这串数字）"
        elif sat >= float(self._c("full_threshold", 90)):
            hint = "（吃撑了，可以打个嗝嫌他话多）"
        text = "｜".join(parts)
        if hint:
            text += "\n" + hint
        return text

    def _user_text(self, state: dict) -> str:
        budget = int(self._c("daily_budget", 8000000))
        left = max(0, budget - int(state["today_tokens"])) if budget > 0 else -1
        lines = [
            "Token 饭碗 · 账本",
            f"饱腹：{int(state['satiety'])} 分（{self._mood(state['satiety'])}）",
            f"今天：{self._bowls(state):.1f} 碗 / {int(state['today_tokens']):,} token",
            f"今日口粮：{budget:,}" + (f"，还剩 {left:,}" if left >= 0 else "（不限）"),
            f"累计：{int(state['total_tokens']):,} token / {int(state['meals'])} 顿",
            f"上一顿：{self._ago(state['last_meal_at'])}",
        ]
        if state.get("last_food"):
            lines.append(
                f"刚被投喂：{state['last_food']}（{self._ago(state['last_feed_at'])}）"
            )
        lines.append(
            f"换算：1 碗 = {int(self._c('tokens_per_bowl', 500000)):,} token，"
            f"饱腹每小时 -{float(self._c('decay_per_hour', 3.0)):g}"
        )
        return "\n".join(lines)

    # ---------------------------------------------------------------- hooks

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._c("enabled", True):
            return
        try:
            async with self._lock:
                state = await asyncio.to_thread(self._sync)
            if self._c("inject_always", True) and self._owner_private(event):
                req.extra_user_content_parts.append(
                    TextPart(text=self._note(state)).mark_as_temp()
                )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 注入饭量失败（不影响聊天）：{exc}")

    @filter.command("饭量", alias={"查饭", "吃饱没"})
    async def cmd_meal(self, event: AstrMessageEvent):
        if not self._owner_private(event):
            yield event.plain_result("饭碗只跟主人私聊时才给看~")
            return
        async with self._lock:
            state = await asyncio.to_thread(self._sync)
        yield event.plain_result(self._user_text(state))

    @filter.command("投喂", alias={"喂饭", "加餐"})
    async def cmd_feed(self, event: AstrMessageEvent, food: str | None = None):
        if not self._owner_private(event):
            yield event.plain_result("只让主人喂~")
            return
        food = (food or "").strip() or "一碗白米饭"
        async with self._lock:
            state = await asyncio.to_thread(self._sync)
            gain = float(self._c("feed_bowls", 1.0)) * float(
                self._c("satiety_per_bowl", 6.0)
            )
            state["satiety"] = min(100.0, float(state["satiety"]) + gain)
            state["last_food"] = food
            state["last_feed_at"] = time.time()
            await asyncio.to_thread(self._save, state)
        yield event.plain_result(
            f"投喂成功：{food}。饱腹 +{gain:.0f}，"
            f"现在 {int(state['satiety'])} 分饱（{self._mood(state['satiety'])}）"
        )
