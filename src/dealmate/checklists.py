"""Category guides: the structured fields and the buyer's checklist for each category.

Seller and buyer Claudes both read the same guide (get_category_guide), so a seller's
spec `height_cm` and a buyer's condition `height_cm <= 83` use the same key, and the
backend can match them with plain rules.
"""

# key, label, unit ("" if none), type: number / text / bool
_COMMON = [("brand", "品牌", "", "text"), ("model", "型號", "", "text")]

GUIDES = {
    "冰箱": {
        "aliases": ["小冰箱", "冷藏櫃", "fridge"],
        "large": True,
        "fields": _COMMON + [
            ("height_cm", "高度", "cm", "number"), ("width_cm", "寬度", "cm", "number"),
            ("depth_cm", "深度", "cm", "number"), ("capacity_l", "容量", "L", "number"),
            ("doors", "門數", "", "number"), ("year", "出廠年份", "", "number"),
        ],
        "checklist": [
            "製冷是否正常、壓縮機運轉聲音大不大",
            "有沒有異味、漏水或生鏽",
            "用了幾年、有沒有維修過",
            "搬運需要幾個人、是否需要貨車",
        ],
    },
    "燈": {
        "aliases": ["落地燈", "立燈", "檯燈", "桌燈", "燈具", "lamp"],
        "large": False,
        "fields": _COMMON + [
            ("height_cm", "高度", "cm", "number"), ("socket", "燈座", "", "text"),
            ("max_watt", "適用燈泡最高瓦數", "W", "number"), ("led_ok", "可用 LED 燈泡", "", "bool"),
            ("dimmable", "可調光", "", "bool"), ("new_price", "新品價", "元", "number"),
        ],
        "checklist": ["可不可以調光", "燈座規格、能不能用 LED 燈泡", "有沒有附燈泡", "電線和開關是否正常"],
    },
    "手機": {
        "aliases": ["iphone", "智慧型手機", "android"],
        "large": False,
        "fields": _COMMON + [
            ("storage_gb", "容量", "GB", "number"), ("battery_health_pct", "電池健康度", "%", "number"),
            ("color", "顏色", "", "text"), ("warranty_until", "保固到期日", "", "text"),
        ],
        "checklist": [
            "iCloud／Google 帳號是否已登出、可以重置",
            "電池健康度",
            "有沒有維修過、換過副廠零件",
            "螢幕有沒有刮傷或烙印",
            "盒裝和配件是否齊全、還有沒有保固",
        ],
    },
    "床墊": {
        "aliases": ["mattress"],
        "large": True,
        "fields": _COMMON + [
            ("width_cm", "寬度", "cm", "number"), ("length_cm", "長度", "cm", "number"),
            ("thickness_cm", "厚度", "cm", "number"), ("type", "種類（獨立筒、乳膠…）", "", "text"),
            ("years_used", "使用年數", "年", "number"),
        ],
        "checklist": [
            "家裡有沒有養寵物、有沒有人抽菸",
            "有沒有污漬、塌陷",
            "是否一直有用保潔墊",
            "能不能捲起來、搬運是否需要貨車",
        ],
    },
    "腳踏車": {
        "aliases": ["自行車", "單車", "bike"],
        "large": True,
        "fields": _COMMON + [
            ("gears", "變速段數", "", "number"), ("wheel_inch", "輪徑", "吋", "number"),
            ("frame_size", "車架尺寸", "", "text"), ("type", "種類（公路、登山、小折…）", "", "text"),
        ],
        "checklist": ["有沒有變速、變速是否順", "煞車是否正常", "鏈條和輪胎狀況", "車架有沒有撞過"],
    },
    "書": {
        "aliases": ["課本", "教科書", "原文書", "book"],
        "large": False,
        "fields": [
            ("isbn", "ISBN", "", "text"), ("edition", "版次", "", "number"),
            ("language", "語言（中譯本、原文）", "", "text"), ("author", "作者", "", "text"),
            ("has_notes", "有筆記或劃線", "", "bool"),
        ],
        "checklist": ["第幾版", "中譯本還是原文", "有沒有筆記、劃線", "有沒有缺頁或水漬"],
    },
    "耳機": {
        "aliases": ["藍牙耳機", "headphones", "earbuds"],
        "large": False,
        "fields": _COMMON + [
            ("wireless", "無線", "", "bool"), ("anc", "主動降噪", "", "bool"),
            ("battery_hours", "續航", "小時", "number"),
        ],
        "checklist": ["電池續航還有多久", "有沒有解除原本的配對", "耳塞是否換新或清潔過", "盒裝和保固"],
    },
}

_DEFAULT = {
    "large": False,
    "fields": _COMMON + [("year", "購買年份", "", "number")],
    "checklist": ["用了多久", "有沒有損壞或瑕疵", "配件是否齊全"],
}


def canonical(category: str) -> str:
    """Map what the user said ("小冰箱", "IKEA 落地燈") to a guide's name, so both sides match."""
    c = category.strip()
    lc = c.lower()
    names = [(name, [name, *g["aliases"]]) for name, g in GUIDES.items()]
    for name, words in names:
        if lc in (w.lower() for w in words):
            return name
    # Longest word contained in the input wins: "IKEA 落地燈" -> 落地燈 -> 燈.
    best = max(
        ((len(w), name) for name, words in names for w in words if w.lower() in lc),
        default=None,
    )
    return best[1] if best else c


def guide(category: str) -> dict:
    name = canonical(category)
    g = GUIDES.get(name)
    result = {
        "category": name,
        "known_category": g is not None,
        "large_item": (g or _DEFAULT)["large"],
        "fields": [
            {"key": k, "label": label, "unit": unit, "type": type_}
            for k, label, unit, type_ in (g or _DEFAULT)["fields"]
        ],
        "buyer_checklist": (g or _DEFAULT)["checklist"],
    }
    if g is None:
        result["note"] = (
            "這個品類沒有專用欄位：沿用下面的通用欄位，其他規格自訂英文小寫 key（例如 weight_kg），"
            "買賣兩邊同一個品類要用同樣的 key。"
        )
    return result


def label_specs(category: str, specs: list[dict]) -> list[dict]:
    """Fill in a missing label and unit from the category's fields, so the card reads "燈座" not "socket"."""
    fields = {f["key"]: f for f in guide(category)["fields"]}
    out = []
    for sp in specs:
        f = fields.get(sp["key"])
        if f:
            sp = {"label": f["label"], **({"unit": f["unit"]} if f["unit"] else {}), **sp}
        out.append(sp)
    return out
