"""Claude API calls for the agents' wording.

The LLM never decides a price. It only turns an offer the engine already
decided into a natural sentence, and writes the report for each side.
Each call receives only what that side is allowed to know.

Without ANTHROPIC_API_KEY (or if a call fails), fixed template text is used,
so the same code works before and after the key is added.
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


def _ask(system: str, prompt: str, fallback: str, max_tokens: int = 200) -> str:
    client = _get_client()
    if client is None:
        return fallback
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip() or fallback
    except Exception:
        log.exception("Claude API call failed; using fallback text")
        return fallback


def _offer_template(side: str, price: int, item: dict, round_: int, holding: bool) -> str:
    title = item.get("title") or "這件商品"
    condition = item.get("condition")
    if holding:
        return f"{price} 元已經是我們能給的最好價格了。"
    if side == "seller":
        if round_ == 1:
            detail = f"，{condition}" if condition else ""
            return f"您好，這個{title}{detail}，開價 {price} 元。"
        return f"我們可以再讓一點，{price} 元。"
    if round_ == 1:
        return f"您好，我對{title}有興趣，{price} 元可以嗎？"
    return f"我再加一些，{price} 元如何？"


def offer_message(
    side: str, price: int, item: dict, last_message: str | None, round_: int, holding: bool = False
) -> str:
    """One negotiation turn, in Traditional Chinese. `side` is 'seller' or 'buyer'.

    `holding` means this offer is the same as last round's (the side has reached its limit).
    """
    role = "賣家" if side == "seller" else "買家"
    system = (
        f"你是二手交易平台 DealMate 上代表{role}的議價代理人。"
        "用一到兩句自然、有禮貌的繁體中文寫出這一輪的出價。"
        "價格必須完全照給你的數字，不可更改，也不可提到任何底價、預算或其他數字。"
    )
    prompt = (
        f"商品：{item.get('title')}，狀況：{item.get('condition') or '未提供'}\n"
        f"第 {round_} 輪\n"
        f"對方上一句：{last_message or '（尚無，這是開場）'}\n"
        f"這一輪我方出價：{price} 元"
        + ("（和上一輪相同，不再讓步）" if holding else "")
    )
    return _ask(system, prompt, _offer_template(side, price, item, round_, holding))


def _report_template(side: str, f: dict) -> str:
    seller_first, seller_last = f["seller_prices"][0], f["seller_prices"][-1]
    buyer_first, buyer_last = f["buyer_prices"][0], f["buyer_prices"][-1]
    if side == "seller":
        return (
            f"「{f['title']}」談成 {f['price']} 元。\n"
            f"您開價 {seller_first} 元，經過 {f['rounds']} 輪議價，"
            f"買家從 {buyer_first} 元加到 {buyer_last} 元，我們讓到 {seller_last} 元。\n"
            f"成交價比您設定的最低價 {f['floor_price']} 元高 {f['price'] - f['floor_price']} 元。"
        )
    return (
        f"「{f['title']}」談成 {f['price']} 元。\n"
        f"賣家開價 {seller_first} 元，經過 {f['rounds']} 輪議價降到 {seller_last} 元，"
        f"我們從 {buyer_first} 元加到 {buyer_last} 元。\n"
        f"成交價比您的預算 {f['budget']} 元少 {f['budget'] - f['price']} 元，"
        f"比賣家開價便宜 {seller_first - f['price']} 元。"
    )


def deal_report(side: str, facts: dict) -> str:
    """Report for one side after a price is agreed. `facts` holds only that side's private limit."""
    role = "賣家" if side == "seller" else "買家"
    system = (
        f"你是 DealMate 上代表{role}的議價代理人，向你的主人回報議價結果。"
        "用三到四句繁體中文，只能使用提供的事實與數字，不可編造。"
    )
    return _ask(system, f"議價事實：{facts}", _report_template(side, facts), max_tokens=400)
