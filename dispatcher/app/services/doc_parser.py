"""
通用文档解析器 — 可复用的文档→文本转换服务

支持格式：
  - PDF, Word(.docx), Excel(.xlsx), PPT(.pptx), CSV, HTML, JSON, XML
  - 图片(png/jpg/gif/webp) → VL 模型解析
  - OFD → easyofd 转 PDF → MarkItDown
  - UOF/WPS(.wps/.et/.dps) → LibreOffice 转换 → MarkItDown

用法：
  result = await parse_document(file_bytes, "report.pdf")
  print(result.text)       # Markdown 文本
  print(result.images)     # 提取的图片列表
  print(result.metadata)   # 页数、表格数等元信息
"""

import asyncio
import logging
import tempfile
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MARKITDOWN_FORMATS = {
    ".pdf", ".docx", ".xlsx", ".xls", ".pptx",
    ".csv", ".html", ".htm", ".json", ".xml", ".epub",
    ".txt", ".md", ".rst", ".rtf",
}

IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

OFD_FORMATS = {".ofd"}

LIBREOFFICE_FORMATS = {
    ".uof", ".uot", ".uos", ".uop",  # UOF 统一办公格式
    ".wps", ".et", ".dps",            # WPS 格式
    ".doc", ".ppt", ".xls",           # 旧版 Office（MarkItDown 对 .doc 支持差）
    ".odt", ".ods", ".odp",           # OpenDocument
}


@dataclass
class ParseResult:
    text: str
    format: str
    images: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


async def parse_document(
    data: bytes,
    filename: str,
    extract_images: bool = True,
    vision_analyze: bool = False,
) -> ParseResult:
    """
    解析文档，返回 Markdown 文本。

    Args:
        data: 文件二进制数据
        filename: 文件名（用于判断格式）
        extract_images: 是否提取文档中的图片
        vision_analyze: 是否用 VL 模型解析提取出的图片
    """
    ext = Path(filename).suffix.lower()

    if ext in IMAGE_FORMATS:
        return await _parse_image(data, filename, vision_analyze)

    if ext in OFD_FORMATS:
        return await _parse_ofd(data, filename)

    if ext in LIBREOFFICE_FORMATS:
        return await _parse_via_libreoffice(data, filename)

    if ext in MARKITDOWN_FORMATS:
        return await _parse_markitdown(data, filename)

    return ParseResult(
        text=f"[不支持的文件格式: {ext}]",
        format=ext,
        warnings=[f"格式 {ext} 暂不支持解析"],
    )


def supported_extensions() -> list[str]:
    """返回所有支持的文件扩展名"""
    all_exts = MARKITDOWN_FORMATS | IMAGE_FORMATS | OFD_FORMATS | LIBREOFFICE_FORMATS
    return sorted(all_exts)


# ── MarkItDown 解析 ──

async def _parse_markitdown(data: bytes, filename: str) -> ParseResult:
    ext = Path(filename).suffix.lower()

    def _do():
        from markitdown import MarkItDown
        md = MarkItDown()
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            result = md.convert(tmp)
            return result.text_content
        finally:
            Path(tmp).unlink(missing_ok=True)

    try:
        text = await asyncio.get_event_loop().run_in_executor(None, _do)
        metadata = _analyze_content(text)
        return ParseResult(text=text, format=ext, metadata=metadata)
    except Exception as e:
        logger.error(f"MarkItDown parse failed for {filename}: {e}")
        return ParseResult(
            text=f"[文档解析失败: {filename}]",
            format=ext,
            warnings=[f"MarkItDown 解析失败: {e}"],
        )


# ── OFD 解析 ──

async def _parse_ofd(data: bytes, filename: str) -> ParseResult:
    def _do():
        try:
            import base64 as b64mod
            from easyofd import OFD
        except ImportError:
            return None, "easyofd 未安装，请运行: pip install easyofd"

        try:
            b64str = b64mod.b64encode(data).decode("ascii")
            ofd = OFD()
            ofd.read(b64str)

            doc = ofd.data[0] if ofd.data else None
            if not doc:
                return "[OFD 文档无数据]", None

            page_info = doc.get("page_info", {})
            total_pages = len(page_info)
            text_parts = []

            for page_idx in range(total_pages):
                pi = page_info.get(page_idx, {})
                text_list = pi.get("text_list", [])
                if not text_list:
                    continue

                items = []
                for t in text_list:
                    pos = t.get("pos", [0, 0])
                    if isinstance(pos, list) and len(pos) >= 2:
                        x, y = float(pos[0]), float(pos[1])
                    else:
                        x, y = 0, 0
                    items.append((y, x, t.get("text", "")))

                items.sort(key=lambda i: (i[0], i[1]))

                lines = []
                cur_y = -999.0
                cur_line: list[str] = []
                for y, x, text in items:
                    if abs(y - cur_y) > 2:
                        if cur_line:
                            lines.append("".join(cur_line))
                        cur_line = [text]
                        cur_y = y
                    else:
                        cur_line.append(text)
                if cur_line:
                    lines.append("".join(cur_line))

                page_text = "\n".join(lines)
                if page_text.strip():
                    text_parts.append(page_text)

            full_text = "\n\n---\n\n".join(text_parts) if text_parts else "[OFD 文档无可提取文本]"
            return full_text, None
        except Exception as e:
            return None, f"OFD 解析失败: {e}"

    text, error = await asyncio.get_event_loop().run_in_executor(None, _do)

    if error:
        return ParseResult(text=f"[OFD 解析失败: {error}]", format=".ofd", warnings=[error])

    metadata = _analyze_content(text)
    metadata["page_count"] = text.count("---") + 1
    return ParseResult(text=text, format=".ofd", metadata=metadata)


# ── LibreOffice 转换 ──

async def _parse_via_libreoffice(data: bytes, filename: str) -> ParseResult:
    ext = Path(filename).suffix.lower()

    converted = await _libreoffice_convert(data, filename, target="pdf")
    if not converted:
        return ParseResult(
            text=f"[{ext} 格式需要 LibreOffice 转换，但转换失败]",
            format=ext,
            warnings=[f"LibreOffice 转换失败，请确保已安装 libreoffice"],
        )

    result = await _parse_markitdown(converted, filename.rsplit(".", 1)[0] + ".pdf")
    result.format = ext
    result.metadata["converted_via"] = "libreoffice"
    return result


async def _libreoffice_convert(data: bytes, filename: str, target: str = "pdf") -> bytes | None:
    """用 LibreOffice headless 转换文档格式"""
    tmpdir = tempfile.mkdtemp()
    try:
        src = Path(tmpdir) / filename
        src.write_bytes(data)

        proc = await asyncio.create_subprocess_exec(
            "libreoffice", "--headless", "--convert-to", target,
            "--outdir", tmpdir, str(src),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            logger.error(f"LibreOffice convert failed: {stderr.decode()}")
            return None

        stem = Path(filename).stem
        out_path = Path(tmpdir) / f"{stem}.{target}"
        if out_path.exists():
            return out_path.read_bytes()

        for f in Path(tmpdir).iterdir():
            if f.suffix == f".{target}" and f.name != filename:
                return f.read_bytes()

        return None
    except asyncio.TimeoutError:
        logger.error("LibreOffice convert timed out (120s)")
        return None
    except FileNotFoundError:
        logger.error("LibreOffice not installed")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── 图片解析 ──

async def _parse_image(data: bytes, filename: str, vision_analyze: bool) -> ParseResult:
    if vision_analyze:
        try:
            from app.services.vision import analyze_image
            description = await analyze_image(data, filename)
            return ParseResult(
                text=description,
                format=Path(filename).suffix.lower(),
                metadata={"type": "image", "size": len(data), "vision_analyzed": True},
            )
        except Exception as e:
            logger.warning(f"Vision analysis failed, returning placeholder: {e}")

    return ParseResult(
        text=f"[图片: {filename}, {len(data)} bytes]",
        format=Path(filename).suffix.lower(),
        images=[{"filename": filename, "size": len(data)}],
        metadata={"type": "image", "size": len(data)},
    )


# ── 辅助函数 ──

def _analyze_content(text: str) -> dict:
    """分析解析结果的元信息"""
    lines = text.split("\n")
    table_count = sum(1 for line in lines if line.strip().startswith("|") and "|" in line[1:])
    heading_count = sum(1 for line in lines if line.strip().startswith("#"))
    return {
        "char_count": len(text),
        "line_count": len(lines),
        "table_rows": table_count,
        "heading_count": heading_count,
        "has_tables": table_count > 2,
        "has_headings": heading_count > 0,
    }


def check_dependencies() -> dict[str, bool]:
    """检查各解析依赖是否可用"""
    deps = {}

    try:
        import markitdown
        deps["markitdown"] = True
    except ImportError:
        deps["markitdown"] = False

    try:
        from easyofd import OFD
        deps["easyofd"] = True
    except ImportError:
        deps["easyofd"] = False

    try:
        result = subprocess.run(
            ["libreoffice", "--version"],
            capture_output=True, timeout=5,
        )
        deps["libreoffice"] = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        deps["libreoffice"] = False

    return deps
