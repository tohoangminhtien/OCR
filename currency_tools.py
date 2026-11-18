"""
Currency normalization tool for Hoa Phat compliance/reporting.

Quy đổi một chuỗi tiền tệ ngoại tệ đơn lẻ (VD "1,234.56 USD") về VND theo tỷ giá
bán ra (Sell) của Vietcombank tại thời điểm gọi tool.

Thiết kế: xem openspec/changes/add-normalize-currency-to-vnd/design.md
"""

import os
import re
import json
import time
import logging
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, Optional, Tuple

from server_config import get_http_session, logger


class JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


# ============================================================================
# Hằng số cấu hình
# ============================================================================

VCB_RATES_URL = os.getenv(
    "VCB_RATES_URL",
    "https://portal.vietcombank.com.vn/UserControls/TVPortal.TyGia/pXML.aspx?b=10",
)

# Vietcombank khuyến cáo "Only one request every 5 minutes!" → cache 5 phút.
RATE_CACHE_TTL_SECONDS = int(os.getenv("VCB_RATES_CACHE_TTL", "300"))

# Các loại tỷ giá Vietcombank niêm yết.
VALID_RATE_TYPES = {"Sell", "Buy", "Transfer"}

# Map ký hiệu tiền tệ phổ biến → mã ISO (dùng khi chuỗi không có mã ISO rõ ràng).
SYMBOL_TO_CODE = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₫": "VND",
    "đ": "VND",
}

# Cache module-level: (rates_dict, rate_date_str, fetched_at_epoch)
_rates_cache: Optional[Tuple[Dict[str, Dict[str, Decimal]], str, float]] = None


class CurrencyError(Exception):
    """Lỗi nghiệp vụ quy đổi tiền tệ (parse / tỷ giá / mã không hỗ trợ)."""


# ============================================================================
# Parse chuỗi tiền tệ
# ============================================================================

def _normalize_number(raw: str) -> Decimal:
    """Chuẩn hóa phần số theo heuristic vị trí dấu.

    - Có cả '.' và ',' → dấu XUẤT HIỆN SAU CÙNG là thập phân, dấu kia là phân cách nghìn.
    - Chỉ một loại dấu, xuất hiện > 1 lần → phân cách nghìn.
    - Chỉ một dấu duy nhất: nếu đúng 3 chữ số phía sau → phân cách nghìn; ngược lại → thập phân.
    """
    s = raw.strip()
    # Giữ lại dấu trừ ở đầu nếu có
    negative = s.startswith("-")
    s = s.lstrip("+-").strip()

    if not re.search(r"\d", s):
        raise CurrencyError(f"Không tìm thấy số hợp lệ trong '{raw}'.")

    # Chỉ giữ chữ số, '.' và ','
    s = re.sub(r"[^\d.,]", "", s)

    has_dot = "." in s
    has_comma = "," in s

    if has_dot and has_comma:
        # Dấu xuất hiện sau cùng là thập phân
        if s.rfind(".") > s.rfind(","):
            dec_sep, thou_sep = ".", ","
        else:
            dec_sep, thou_sep = ",", "."
        s = s.replace(thou_sep, "").replace(dec_sep, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        count = s.count(sep)
        last = s.rsplit(sep, 1)[-1]
        if count > 1:
            # Nhiều dấu cùng loại → chắc chắn phân cách nghìn
            s = s.replace(sep, "")
        elif len(last) == 3 and s[0] != sep:
            # Một dấu + đúng 3 chữ số phía sau → phân cách nghìn (VD "1.234" → 1234)
            s = s.replace(sep, "")
        else:
            # Ngược lại → dấu thập phân
            s = s.replace(sep, ".")

    try:
        value = Decimal(s)
    except (InvalidOperation, ValueError):
        raise CurrencyError(f"Không parse được số từ '{raw}'.")

    return -value if negative else value


def parse_amount_string(text: str) -> Tuple[Decimal, str]:
    """Tách (số lượng, mã tiền tệ ISO) từ chuỗi đầu vào.

    Ưu tiên mã ISO 3 ký tự; nếu không có thì thử ký hiệu tiền tệ.
    """
    if not text or not str(text).strip():
        raise CurrencyError("Chuỗi đầu vào rỗng.")

    original = str(text).strip()

    # 1. Tìm mã ISO 3 ký tự (đứng riêng như một từ)
    code = None
    m = re.search(r"(?<![A-Za-z])([A-Za-z]{3})(?![A-Za-z])", original)
    if m:
        code = m.group(1).upper()

    # 2. Nếu không có mã ISO, thử ký hiệu tiền tệ
    if code is None:
        for sym, mapped in SYMBOL_TO_CODE.items():
            if sym in original:
                code = mapped
                break

    if code is None:
        raise CurrencyError(
            f"Không xác định được mã tiền tệ trong '{original}'. "
            f"Hãy truyền mã ISO (VD 'USD', 'EUR', 'JPY')."
        )

    # 3. Bỏ mã ISO khỏi chuỗi trước khi parse số (tránh nuốt chữ số)
    number_part = re.sub(r"(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])", " ", original)
    amount = _normalize_number(number_part)

    return amount, code


# ============================================================================
# Lấy tỷ giá Vietcombank + cache
# ============================================================================

async def fetch_vcb_rates(force: bool = False) -> Tuple[Dict[str, Dict[str, Decimal]], str]:
    """Trả về (rates, rate_date). Dùng cache trong RATE_CACHE_TTL_SECONDS.

    rates: {"USD": {"Buy": Decimal|None, "Transfer": Decimal|None, "Sell": Decimal}, ...}
    Raise CurrencyError nếu không lấy/parse được và không có cache hợp lệ.
    """
    global _rates_cache

    if not force and _rates_cache is not None:
        rates, rate_date, fetched_at = _rates_cache
        if (time.time() - fetched_at) < RATE_CACHE_TTL_SECONDS:
            return rates, rate_date

    try:
        session = await get_http_session()
        async with session.get(VCB_RATES_URL) as resp:
            if resp.status != 200:
                raise CurrencyError(
                    f"Vietcombank trả HTTP {resp.status} khi lấy tỷ giá."
                )
            xml_text = await resp.text()
        rates, rate_date = _parse_vcb_xml(xml_text)
    except CurrencyError:
        raise
    except Exception as e:
        raise CurrencyError(f"Không lấy được tỷ giá Vietcombank: {e}")

    if not rates:
        raise CurrencyError("Bảng tỷ giá Vietcombank rỗng hoặc không hợp lệ.")

    _rates_cache = (rates, rate_date, time.time())
    return rates, rate_date


def _parse_decimal_or_none(value: Optional[str]) -> Optional[Decimal]:
    if not value or value.strip() in ("", "-"):
        return None
    try:
        # Tỷ giá VCB niêm yết kiểu US: "26,470.00"
        return Decimal(value.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _parse_vcb_xml(xml_text: str) -> Tuple[Dict[str, Dict[str, Decimal]], str]:
    root = ET.fromstring(xml_text)
    rate_date = ""
    dt = root.find("DateTime")
    if dt is not None and dt.text:
        rate_date = dt.text.strip()

    rates: Dict[str, Dict[str, Decimal]] = {}
    for node in root.findall("Exrate"):
        code = (node.get("CurrencyCode") or "").strip().upper()
        if not code:
            continue
        rates[code] = {
            "Buy": _parse_decimal_or_none(node.get("Buy")),
            "Transfer": _parse_decimal_or_none(node.get("Transfer")),
            "Sell": _parse_decimal_or_none(node.get("Sell")),
        }
    return rates, rate_date


# ============================================================================
# Quy đổi + tool wrapper
# ============================================================================

def _round_vnd(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def normalize_currency_to_vnd_tool(amount_str: str) -> str:
    """Quy đổi một chuỗi tiền tệ đơn lẻ về VND theo tỷ giá Sell của Vietcombank.

    Trả về chuỗi JSON. Xem spec/design của change add-normalize-currency-to-vnd.
    """
    try:
        amount, currency = parse_amount_string(amount_str)

        # Đầu vào đã là VND → trả nguyên, không gọi VCB.
        if currency == "VND":
            result = {
                "status": "success",
                "input": amount_str,
                "amount": amount,
                "currency": "VND",
                "rate": 1,
                "rate_type": "Sell",
                "rate_date": None,
                "source": "Vietcombank",
                "amount_vnd": _round_vnd(amount),
            }
            return json.dumps(result, ensure_ascii=False, cls=JsonEncoder)

        rates, rate_date = await fetch_vcb_rates()

        if currency not in rates:
            return json.dumps(
                {
                    "status": "error",
                    "input": amount_str,
                    "message": f"Mã tiền tệ '{currency}' không có trong bảng tỷ giá Vietcombank.",
                },
                ensure_ascii=False,
            )

        sell = rates[currency].get("Sell")
        if sell is None:
            return json.dumps(
                {
                    "status": "error",
                    "input": amount_str,
                    "message": f"Vietcombank không niêm yết tỷ giá bán (Sell) cho '{currency}'.",
                },
                ensure_ascii=False,
            )

        amount_vnd = _round_vnd(amount * sell)

        result = {
            "status": "success",
            "input": amount_str,
            "amount": amount,
            "currency": currency,
            "rate": sell,
            "rate_type": "Sell",
            "rate_date": rate_date,
            "source": "Vietcombank",
            "amount_vnd": amount_vnd,
        }
        return json.dumps(result, ensure_ascii=False, cls=JsonEncoder)

    except CurrencyError as e:
        return json.dumps(
            {"status": "error", "input": amount_str, "message": str(e)},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.exception(f"Error in normalize_currency_to_vnd_tool: {e}")
        return json.dumps(
            {"status": "error", "input": amount_str, "message": str(e)},
            ensure_ascii=False,
        )

QUOTE_MONEY_FIELDS = ("Giảm giá", "Thuế nhập khẩu", "Cước vận chuyển", "Chi phí khác")


def _convert_money_value(value, rate: Decimal):
    """Nhân giá trị tiền (số hoặc chuỗi en-US) với tỷ giá, làm tròn VND. Không parse được → giữ nguyên."""
    if isinstance(value, list):
        return [_convert_money_value(v, rate) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    try:
        if isinstance(value, (int, float)):
            num = Decimal(str(value))
        elif isinstance(value, str):
            num = Decimal(value.replace(",", "").strip())
        else:
            return value
    except (InvalidOperation, ValueError):
        return value
    vnd = _round_vnd(num * rate)
    return str(vnd) if isinstance(value, str) else vnd


def _first_currency(raw) -> str:
    if isinstance(raw, list):
        raw = next((x for x in raw if isinstance(x, str) and x.strip()), "")
    return raw.strip().upper() if isinstance(raw, str) else ""


async def convert_quotation_to_vnd(merged: dict) -> dict:
    """Đổi các field tiền trong báo giá (đã gộp) sang VND theo tỷ giá bán (Sell) Vietcombank hiện tại.

    - "Tiền tệ" rỗng hoặc VND → giữ nguyên.
    - Ngược lại: nhân Đơn giá (từng hạng mục), Giảm giá, Thuế nhập khẩu, Cước vận chuyển,
      Chi phí khác với tỷ giá; thêm field "Tỉ giá <ISO> → VND"; đặt "Tiền tệ" = "VND".
    - Lỗi lấy tỷ giá → giữ nguyên dữ liệu gốc và thêm "Lỗi quy đổi VND".
    """
    currency = _first_currency(merged.get("Tiền tệ"))
    if not currency or currency == "VND":
        return merged

    try:
        rates, _ = await fetch_vcb_rates()
        if currency not in rates:
            raise CurrencyError(f"Mã tiền tệ '{currency}' không có trong bảng tỷ giá Vietcombank.")
        rate = rates[currency].get("Sell")
        if rate is None:
            raise CurrencyError(f"Vietcombank không niêm yết tỷ giá bán (Sell) cho '{currency}'.")
    except CurrencyError as e:
        merged["Lỗi quy đổi VND"] = str(e)
        return merged

    for item in merged.get("Nội dung công việc", []) or []:
        if isinstance(item, dict) and "Đơn giá" in item:
            item["Đơn giá"] = _convert_money_value(item["Đơn giá"], rate)
    for key in QUOTE_MONEY_FIELDS:
        if key in merged:
            merged[key] = _convert_money_value(merged[key], rate)

    # Giữ nguyên kiểu gốc của field (list → ["VND"], chuỗi → "VND")
    merged["Tiền tệ"] = ["VND"] if isinstance(merged.get("Tiền tệ"), list) else "VND"
    merged[f"Tỉ giá {currency} → VND"] = [float(rate)]
    return merged


async def get_vcb_exchange_rate_tool(currency: str = "USD", rate_type: str = "Sell") -> str:
    """Trả tỷ giá THÔ của Vietcombank cho một mã tiền tệ (mặc định USD/Sell).

    Dùng cho biểu mẫu tự tính công thức (VD form FeSi/P.NL) để điền Ô TỶ GIÁ,
    thay vì quy đổi sẵn ra VND như normalize_currency_to_vnd_tool.

    currency: mã ISO 3 ký tự (VD 'USD', 'EUR', 'JPY'). 'VND' → rate=1.
    rate_type: 'Sell' | 'Buy' | 'Transfer' (mặc định 'Sell' theo tỷ giá bán ra).
    """
    try:
        currency_norm = (currency or "").strip().upper()
        rate_type_norm = (rate_type or "").strip().capitalize()

        # 1. Validate rate_type
        if rate_type_norm not in VALID_RATE_TYPES:
            return json.dumps(
                {
                    "status": "error",
                    "currency": currency_norm,
                    "message": (
                        f"rate_type '{rate_type}' không hợp lệ. "
                        f"Chỉ chấp nhận: {sorted(VALID_RATE_TYPES)}."
                    ),
                },
                ensure_ascii=False,
            )

        # 2. Validate currency
        if not currency_norm:
            return json.dumps(
                {
                    "status": "error",
                    "currency": currency_norm,
                    "message": "Thiếu mã tiền tệ. Hãy truyền mã ISO (VD 'USD').",
                },
                ensure_ascii=False,
            )

        # 3. VND → rate=1, không gọi Vietcombank
        if currency_norm == "VND":
            return json.dumps(
                {
                    "status": "success",
                    "currency": "VND",
                    "rate": 1,
                    "rate_type": rate_type_norm,
                    "rate_date": None,
                    "source": "Vietcombank",
                },
                ensure_ascii=False,
                cls=JsonEncoder,
            )

        rates, rate_date = await fetch_vcb_rates()

        if currency_norm not in rates:
            return json.dumps(
                {
                    "status": "error",
                    "currency": currency_norm,
                    "message": f"Mã tiền tệ '{currency_norm}' không có trong bảng tỷ giá Vietcombank.",
                },
                ensure_ascii=False,
            )

        rate = rates[currency_norm].get(rate_type_norm)
        if rate is None:
            return json.dumps(
                {
                    "status": "error",
                    "currency": currency_norm,
                    "message": (
                        f"Vietcombank không niêm yết tỷ giá '{rate_type_norm}' cho '{currency_norm}'."
                    ),
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "status": "success",
                "currency": currency_norm,
                "rate": rate,
                "rate_type": rate_type_norm,
                "rate_date": rate_date,
                "source": "Vietcombank",
            },
            ensure_ascii=False,
            cls=JsonEncoder,
        )

    except CurrencyError as e:
        return json.dumps(
            {"status": "error", "currency": currency, "message": str(e)},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.exception(f"Error in get_vcb_exchange_rate_tool: {e}")
        return json.dumps(
            {"status": "error", "currency": currency, "message": str(e)},
            ensure_ascii=False,
        )
