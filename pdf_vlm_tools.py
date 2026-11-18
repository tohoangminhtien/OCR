import asyncio
import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
from openai import OpenAI

from s3_service import download_s3_or_url_bytes
from server_config import logger

STRUCTURED_MODEL = os.getenv("STRUCTURED_MODEL")

client = OpenAI(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=180.0,
    max_retries=0,
)


def ocr_page(
    page_number: int, image_base64: str, system_prompt: str, max_retries: int
) -> str:
    """OCR một trang, retry khi lỗi. Trả về text (hoặc thông báo lỗi)."""
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=STRUCTURED_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + image_base64
                                },
                            }
                        ],
                    },
                ],
                max_tokens=8192,
                temperature=0,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            logger.warning(
                "Trang %s lỗi lần %s/%s: %s", page_number, attempt, max_retries, exc
            )
            if attempt == max_retries:
                return f"[OCR FAILED] {exc}"
            time.sleep(min(2 ** (attempt - 1), 8))
    return "[OCR FAILED]"


def read_pdf_sync(
    pdf_bytes: bytes, batch_size: int, dpi: int, max_retries: int, system_prompt: str
) -> list[str]:
    """Trả về list kết quả OCR theo đúng thứ tự trang."""
    pages = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            for start in range(0, len(pdf), batch_size):
                futures = []
                for i in range(start, min(start + batch_size, len(pdf))):
                    pix = pdf[i].get_pixmap(
                        matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False
                    )
                    image_base64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
                    futures.append(
                        executor.submit(
                            ocr_page, i + 1, image_base64, system_prompt, max_retries
                        )
                    )
                pages += [f.result() for f in futures]  # giữ đúng thứ tự trang

    return "\n\n".join(pages)


async def read_pdf(
    file_path: str,
    batch_size: int,
    dpi: int,
    max_retries: int,
    system_prompt: str,
) -> str:
    """Đọc PDF từ S3/URL bằng VLM. Mỗi tool chỉ cần gọi hàm này với system_prompt riêng."""
    try:
        # pdf_bytes = await download_s3_or_url_bytes(file_path)
        pdf_bytes = Path(file_path).read_bytes()
        content = await asyncio.to_thread(
            read_pdf_sync, pdf_bytes, batch_size, dpi, max_retries, system_prompt
        )
        return json.dumps({"status": "success", "content": content}, ensure_ascii=False)
    except Exception as exc:
        logger.error("read_pdf lỗi: %s", exc)
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)
