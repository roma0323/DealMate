"""Claude API calls for the agents' wording.

The LLM never decides a price. It only turns an offer the engine already
decided into a natural sentence, and writes the final report for each side.
Each call receives only what that side is allowed to know.

Without ANTHROPIC_API_KEY, a fixed template is used instead.
"""

import logging
import os

log = logging.getLogger("dealmate.llm")

MODEL = os.environ.get("DEALMATE_MODEL", "claude-haiku-5-5")

_client = None


def _get_client():
    global _client
    if _client is None and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _ask(system: str, prompt: str, fallback: str) -> str:
    client = _get_client()
    if client is None:
        return fallback
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip() or fallback
    except Exception:
        log.exception("Claude API call failed; using fallback text")
        return fallback


def offer_message(side: str, price: int, item: dict, last_message: str | None) -> str:
    """One negotiation turn, in Traditional Chinese. `side` is 'seller' or 'buyer'."""
    role = "賣家" if side == "seller" else "買家"
    fallback = f"（{role}代理人）我方出價 {price} 元。"
    system = (
        f"你是二手交易平台 DealMate 上代表{role}的議價代理人。"
        "用一到兩句自然、有禮貌的繁體中文寫出這一輪的出價。"
        "價格必須完全照給你的數字，不可更改，也不可透露任何底價或預算。"
    )
    prompt = (
        f"商品：{item.get('title')}，狀況：{item.get('condition') or '未提供'}\n"
        f"對方上一句：{last_message or '（尚無）'}\n"
        f"這一輪我方出價：{price} 元"
    )
    return _ask(system, prompt, fallback)


# TODO: final report for each side (process, final price, reference new-item price).
