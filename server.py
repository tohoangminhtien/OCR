import json
from fastmcp import FastMCP
from server_config import MCP_SERVER_HOST, MCP_SERVER_PORT, logger
from currency_tools import convert_quotation_to_vnd
from pdf_vlm_tools import read_pdf

mcp = FastMCP(name="OCR MCP Server")


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


if __name__ == "__main__":
    mcp.run(transport="sse", host=MCP_SERVER_HOST, port=MCP_SERVER_PORT)
