import json
from fastmcp import FastMCP
import asyncio
from server_config import MCP_SERVER_HOST, MCP_SERVER_PORT, logger
from currency_tools import convert_quotation_to_vnd
from pdf_vlm_tools import read_pdf

mcp = FastMCP(name="OCR MCP Server")


def _to_number(value):
    """Chuyển Số lượng về số (en-US); không chuyển được -> None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        num = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return int(num) if num.is_integer() else num


def _group_items_by_material_code(items: list) -> list:
    """TH2: nhóm các dòng cùng Mã vật tư (và cùng Đơn vị tính) và cộng tổng Số lượng.
    Dòng không có Mã vật tư hoặc Số lượng không phải số thì giữ nguyên, không gộp.
    Cùng mã nhưng khác đơn vị tính -> giữ thành các nhóm riêng (không tự ý gộp)."""
    grouped: dict = {}
    result: list = []
    for item in items:
        code = str(item.get("Mã vật tư", "")).strip()
        qty = _to_number(item.get("Số lượng"))
        if not code or code == "-" or qty is None:
            result.append(item)
            continue
        key = (code, str(item.get("Đơn vị tính", "")).strip())
        if key in grouped:
            grouped[key]["Số lượng"] += qty
        else:
            entry = {**item, "Mã vật tư": code, "Số lượng": qty}
            grouped[key] = entry
            result.append(entry)
    return result


def _parse_json_objects(text: str) -> list[dict]:
    """Tách các object JSON nối tiếp nhau (mỗi trang một object) theo thứ tự xuất hiện.
    Bỏ qua fenced codeblock / text thừa xung quanh."""
    decoder = json.JSONDecoder()
    objects, pos = [], 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        pos = end
    return objects


def _is_empty_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _merge_key(target: dict, key: str, value) -> None:
    """Gộp value vào target[key]: value rỗng -> bỏ qua; dict -> gộp đệ quy theo key con;
    còn lại -> gom thành list."""
    if _is_empty_value(value):
        return
    if isinstance(value, dict):
        sub = target.setdefault(key, {})
        for k, v in value.items():
            _merge_key(sub, k, v)
        if not sub:
            del target[key]
        return
    target.setdefault(key, []).append(value)


@mcp.tool(name="READ_BAO_GIA", description="Công cụ dùng để trích xuất thông tin báo giá")
async def read_quotation_pdf_tool(
    file_path: str, batch_size: int = 16, dpi: int = 144, max_retries: int = 3,
) -> str:
    with open("./prompts/trich_xuat_bao_gia.md", "r", encoding="utf-8") as f:
        prompt = f.read()
    results = await read_pdf(file_path, batch_size, dpi, max_retries, prompt)

    # Hậu xử lý: parse JSON từng trang (đúng thứ tự trang) rồi gộp key
    payload = json.loads(results)
    if payload.get("status") != "success":
        return results

    merged: dict = {}
    for page in _parse_json_objects(payload.get("content", "")):
        for key, value in page.items():
            if key == "Nội dung công việc":
                if isinstance(value, dict):
                    value = [value]
                items = [
                    it for it in (value or [])
                    if not _is_empty_value(it)
                ]
                if items:
                    merged.setdefault("Nội dung công việc", []).extend(items)
            else:
                _merge_key(merged, key, value)

    merged = await convert_quotation_to_vnd(merged)

    return json.dumps({"status": "success", "content": merged}, ensure_ascii=False)


def _merge_price_quote_pages(content: str) -> dict:
    """Parse JSON từng trang rồi gộp: 'Nội dung công việc' nối list, key khác gộp bằng _merge_key."""
    merged: dict = {}
    for page in _parse_json_objects(content):
        for key, value in page.items():
            if key == "Nội dung công việc":
                if isinstance(value, dict):
                    value = [value]
                items = [it for it in (value or []) if not _is_empty_value(it)]
                if items:
                    merged.setdefault("Nội dung công việc", []).extend(items)
            else:
                _merge_key(merged, key, value)
    return merged


@mcp.tool(
    name="READ_CHAO_GIA_DICH_VU",
    description="""Công cụ dùng để trích xuất thông tin chào giá của loại hàng dịch vụ.

    Tham số:
        requirement_tech_path (str): đường dẫn file Yêu cầu kỹ thuật (bắt buộc).
        category_path (str): đường dẫn file Danh mục (tùy chọn, để trống nếu không có).
            - Không có danh mục (TH1): trích xuất hạng mục + thông tin chung từ file Yêu cầu kỹ thuật.
            - Có danh mục (TH2): hạng mục lấy từ file Danh mục (nhóm theo Mã vật tư, cộng tổng số lượng);
              người đề nghị, đầu mối liên hệ kỹ thuật, ngày cần hàng lấy từ file Yêu cầu kỹ thuật.
    """,
)
async def read_price_quote_pdf_tool(
    requirement_tech_path: str,
    category_path: str = "",
    batch_size: int = 16,
    dpi: int = 144,
    max_retries: int = 3,
) -> str:
    has_category = bool(category_path and category_path.strip())

    if not has_category:
        # TH1: chỉ có file Yêu cầu kỹ thuật
        with open("./prompts/trich_xuat_chao_gia_th1.md", "r", encoding="utf-8") as f:
            prompt = f.read()
        results = await read_pdf(
            requirement_tech_path, batch_size, dpi, max_retries, prompt
        )
        payload = json.loads(results)
        if payload.get("status") != "success":
            return results
        merged = _merge_price_quote_pages(payload.get("content", ""))
        return json.dumps({"status": "success", "content": merged}, ensure_ascii=False)

    # TH2: OCR riêng từng loại file với prompt riêng rồi gộp kết quả
    with open(
        "./prompts/trich_xuat_chao_gia_th2_danh_muc.md", "r", encoding="utf-8"
    ) as f:
        category_prompt = f.read()
    with open(
        "./prompts/trich_xuat_chao_gia_th2_yeu_cau_ky_thuat.md", "r", encoding="utf-8"
    ) as f:
        requirement_prompt = f.read()

    category_results, requirement_results = await asyncio.gather(
        read_pdf(category_path, batch_size, dpi, max_retries, category_prompt),
        read_pdf(
            requirement_tech_path, batch_size, dpi, max_retries, requirement_prompt
        ),
    )

    category_payload = json.loads(category_results)
    if category_payload.get("status") != "success":
        return json.dumps(
            {
                "status": "error",
                "message": f"File Danh mục ({category_path}): {category_payload.get('message', category_payload)}",
            },
            ensure_ascii=False,
        )
    requirement_payload = json.loads(requirement_results)
    if requirement_payload.get("status") != "success":
        return json.dumps(
            {
                "status": "error",
                "message": f"File Yêu cầu kỹ thuật ({requirement_tech_path}): {requirement_payload.get('message', requirement_payload)}",
            },
            ensure_ascii=False,
        )

    category_data = _merge_price_quote_pages(category_payload.get("content", ""))
    requirement_data = _merge_price_quote_pages(requirement_payload.get("content", ""))

    merged: dict = {}
    items = _group_items_by_material_code(category_data.get("Nội dung công việc", []))
    if items:
        merged["Nội dung công việc"] = items
    # Hạng mục chỉ lấy từ Danh mục; Yêu cầu kỹ thuật chỉ đóng góp thông tin chung
    for key, value in requirement_data.items():
        if key != "Nội dung công việc":
            merged[key] = value

    return json.dumps({"status": "success", "content": merged}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="sse", host=MCP_SERVER_HOST, port=MCP_SERVER_PORT)
