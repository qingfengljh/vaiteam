"""
图片解析服务 — 用 VL 模型解析图片内容，转为文字描述注入对话

流程：用户上传图片 → VL 模型解析 → 返回文字描述 → 注入主对话模型
"""

import base64
import logging
import time
from pathlib import Path

from app.services import model_pool

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 4 * 1024 * 1024  # 4MB，超过则压缩

IMAGE_ANALYSIS_PROMPT = """请详细描述这张图片的内容。如果是 UI 界面截图，请描述：
- 页面整体布局和结构
- 各个功能区域和模块
- 可见的文字内容
- 交互元素（按钮、输入框、菜单等）
- 数据展示内容

如果是流程图/架构图，请描述节点和关系。
如果是其他类型的图片，请如实描述看到的内容。
输出纯文字描述，不要用 markdown 格式。"""


async def analyze_image(image_data: bytes, filename: str = "", user_hint: str = "") -> str:
    """
    调用 VL 模型解析图片，返回文字描述。

    Args:
        image_data: 图片二进制数据
        filename: 文件名（用于判断格式）
        user_hint: 用户的附带说明
    """
    vision_model = _find_vision_model()
    if not vision_model:
        logger.warning("No vision model configured (supports_vision=True), image analysis skipped")
        return f"[图片: {filename or '未命名'}]（未配置图像解析模型，请在模型设置中勾选 supports_vision）"

    ext = Path(filename).suffix.lower() if filename else ".png"
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(ext, "image/png")

    if len(image_data) > MAX_IMAGE_SIZE:
        image_data = _compress_image(image_data, MAX_IMAGE_SIZE)
        logger.info(f"Image compressed to {len(image_data)} bytes")

    b64 = base64.b64encode(image_data).decode("utf-8")
    data_url = f"data:{mime};base64,{b64}"

    prompt = IMAGE_ANALYSIS_PROMPT
    if user_hint:
        prompt += f"\n\n用户补充说明：{user_hint}"

    client, actual_model = model_pool.get_client(vision_model)
    logger.info(f"Vision analysis: model={actual_model}, image_size={len(image_data)} bytes")

    t0 = time.monotonic()
    try:
        resp = await client.chat.completions.create(
            model=actual_model,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        )
        if isinstance(resp, str):
            description = resp
            usage = None
        elif isinstance(resp, dict):
            choices = resp.get("choices") or []
            msg = (choices[0] or {}).get("message") if choices else {}
            content = (msg or {}).get("content", "")
            description = content if isinstance(content, str) else str(content)
            usage = resp.get("usage")
        else:
            choices = getattr(resp, "choices", None) or []
            msg = getattr(choices[0], "message", None) if choices else None
            content = getattr(msg, "content", "") if msg else ""
            description = content if isinstance(content, str) else str(content)
            usage = getattr(resp, "usage", None)
        elapsed = int((time.monotonic() - t0) * 1000)
        if usage:
            if isinstance(usage, dict):
                in_tok = usage.get("prompt_tokens", 0) or 0
                out_tok = usage.get("completion_tokens", 0) or 0
            else:
                in_tok = getattr(usage, "prompt_tokens", 0) or 0
                out_tok = getattr(usage, "completion_tokens", 0) or 0
            logger.info(f"Vision done: model={actual_model}, {len(description)} chars, {elapsed}ms, tokens={in_tok}+{out_tok}")
            from app.services.ai_leader import _track_usage
            await _track_usage(actual_model, usage, elapsed, caller="vision.analyze_image")
        else:
            logger.info(f"Vision done: model={actual_model}, {len(description)} chars, {elapsed}ms")

        return description
    except Exception as e:
        logger.error(f"Vision analysis failed: {type(e).__name__}: {e}")
        return f"[图片: {filename or '未命名'}]（图片解析失败: {e}）"


def _compress_image(data: bytes, max_size: int) -> bytes:
    """压缩图片到 max_size 以内，减少 token 消耗"""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        quality = 85
        while quality > 20:
            buf = io.BytesIO()
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_size:
                return buf.getvalue()
            quality -= 15
        w, h = img.size
        img = img.resize((w // 2, h // 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return buf.getvalue()
    except ImportError:
        return data


def _find_vision_model() -> str | None:
    """查找用户明确标记了 supports_vision 的模型，按价格选最便宜的。"""
    candidates = []
    for model, params in model_pool._model_params.items():
        if params.get("supports_vision") and model in model_pool._model_to_provider:
            price = params.get("input_price", 999)
            candidates.append((model, price))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    chosen = candidates[0][0]
    all_names = [c[0] for c in candidates]
    logger.info(f"Vision model selected: {chosen} (candidates: {all_names})")
    return chosen
