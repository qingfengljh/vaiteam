"""文档生成用服务器当前日期（东八区），避免模型沿用训练截止日。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")


def doc_now_zh() -> str:
    now = datetime.now(_TZ)
    return f"{now.year}年{now.month}月{now.day}日"


def doc_time_prompt_block() -> str:
    d = doc_now_zh()
    return (
        "## 文档日期（必须遵守）\n"
        f"- 文中凡出现「编写日期、最后更新、更新于、文档日期、版本日期」等时间表述，须与本次生成一致，使用：**{d}**。\n"
        f"- 禁止使用你知识库中的「当前年份」或训练截止附近日期冒充今天；不得编造与 **{d}** 冲突的「现在」。\n"
    )
