import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.models import Session


def initial_session_title(objective: str) -> str:
    value = " ".join(objective.split())
    return value[:80] or "新对话"


def _clean_title(value: str, fallback: str) -> str:
    title = value.strip().splitlines()[0] if value.strip() else ""
    title = re.sub(r"^[#*\-\d.、\s]+", "", title)
    title = title.strip(" \t\r\n\"'“”‘’《》")
    return (title[:40] or fallback[:40] or "新对话")


async def generate_session_title(
    session: Session,
    *,
    objective: str,
    answer: str,
    client: Any,
) -> bool:
    """Name only an untouched first-turn session; manual titles always win."""
    fallback = initial_session_title(objective)
    if session.title != fallback:
        return False

    prompt = (
        "请给下面这段学习对话生成一个简洁、具体的中文标题。"
        "标题应概括真实意图，不超过20个汉字，不加引号、序号或句号。\n\n"
        f"用户：{objective[:2000]}\n助手：{answer[:3000]}"
    )
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.MODEL_NAME,
                messages=[
                    {"role": "system", "content": "你是学习会话标题编辑，只输出标题。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            ),
            timeout=settings.AGENT_SESSION_TITLE_TIMEOUT_SECONDS,
        )
        generated = response.choices[0].message.content or ""
    except Exception:
        return False

    session.title = _clean_title(generated, fallback)
    session.updated_at = datetime.now(timezone.utc)
    return True
