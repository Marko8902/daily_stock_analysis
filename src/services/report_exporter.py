# -*- coding: utf-8 -*-
"""
===================================
Report Exporter - HTML / PDF 报告导出
===================================

把 Markdown 报告导出为 HTML 与 PDF 两种格式：

- HTML 复用 ``src.formatters.markdown_to_html_document``（带内联 CSS），零额外依赖。
- PDF 使用纯 Python 的 ``xhtml2pdf``，无需系统二进制（如 wkhtmltopdf）。

中文字体说明
-----------
xhtml2pdf 自带的 ``@font-face`` 加载器对 Windows 的 TTC 集合字体
（如微软雅黑 ``msyh.ttc``）支持不稳定，会丢失 ``subfontIndex`` 导致中文缺字。
本模块改为：先用 reportlab 直接注册 TTC 子字体（可正确嵌入），再把字体名
注入 xhtml2pdf 的 ``pisaContext.fontList``，从而让中文正常渲染并嵌入 PDF。

字体探测优先级（找到第一个可用即停止）：
	Windows: 微软雅黑 msyh.ttc -> 黑体 simhei.ttf -> 宋体 simsun.ttc
	Linux:   Noto Sans CJK -> 文泉驿正黑/微米黑
	全部缺失时回退内置 CID STSong-Light

任何一步失败都不会抛出异常，``render_pdf`` 返回 ``None``，调用方应回退（如仅保存
Markdown/HTML），保证主流程零中断。
"""

import logging
import os
import threading
from typing import List, Optional, Tuple

from src.formatters import markdown_to_html_document

logger = logging.getLogger(__name__)

# 默认在 PDF 中使用的字体族名（HTML/CSS 里引用这个名字）
PDF_FONT_FAMILY = "msyh"

# 候选字体：(reportlab 字体名, 文件路径, TTC 子字体索引或 None 表示普通 TTF)
# Windows 字体在前（保持本地行为不变）；Linux 字体在后（GitHub Actions 等云端环境）。
_FONT_CANDIDATES: List[Tuple[str, str, Optional[int]]] = [
	("msyh", r"C:\Windows\Fonts\msyh.ttc", 0),       # 微软雅黑（首选）
	("msyhl", r"C:\Windows\Fonts\msyhl.ttc", 0),     # 微软雅黑 Light
	("simhei", r"C:\Windows\Fonts\simhei.ttf", None),  # 黑体
	("simsun", r"C:\Windows\Fonts\simsun.ttc", 0),   # 宋体
	# --- Linux（fonts-noto-cjk / fonts-wqy-zenhei）---
	("notosanscjk", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
	("notosanscjksc", "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", None),
	("notosanscjk-otf", "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 0),
	("wqyzenhei", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
	("wqymicrohei", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
	# --- macOS（本地开发兜底）---
	("pingfang", "/System/Library/Fonts/PingFang.ttc", 0),
]

# 常见 CJK 字体族别名，统一映射到已注册字体，确保未显式声明字体的文本也用中文字体
_FONT_ALIASES = (
	"microsoft yahei", "msyh", "yahei", "simhei", "simsun",
	"sans-serif", "serif", "helvetica", "arial",
)

_FONT_LOCK = threading.Lock()
_FONT_READY = False              # 字体环境是否已成功初始化
_REGISTERED_FONT_NAME: Optional[str] = None  # 实际注册成功的字体名


def _register_chinese_font() -> Optional[str]:
	"""注册可用的中文字体到 reportlab，返回字体名；全部失败返回 None。"""
	from reportlab.pdfbase import pdfmetrics
	from reportlab.pdfbase.ttfonts import TTFont

	for font_name, path, subfont_index in _FONT_CANDIDATES:
		if not os.path.isfile(path):
			continue
		try:
			if subfont_index is None:
				pdfmetrics.registerFont(TTFont(font_name, path))
			else:
				pdfmetrics.registerFont(TTFont(font_name, path, subfontIndex=subfont_index))
			logger.debug("PDF 中文字体已注册: %s (%s)", font_name, path)
			return font_name
		except Exception as e:  # noqa: BLE001 - 字体损坏/格式不支持时继续尝试下一个
			logger.debug("注册字体失败 %s (%s): %s", font_name, path, e)
			continue

	# 所有 TTF/TTC 均失败，回退到 reportlab 内置 CID 字体（无需文件）
	try:
		from reportlab.pdfbase.cidfonts import UnicodeCIDFont
		pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
		logger.info("PDF 回退使用内置 CID 字体 STSong-Light")
		return "STSong-Light"
	except Exception as e:  # noqa: BLE001
		logger.warning("无法注册任何中文字体，PDF 中文可能缺字: %s", e)
		return None


def _patch_pisa_font_list(font_name: str) -> None:
	"""把已注册字体注入 xhtml2pdf 的每个 pisaContext.fontList（含常见别名）。"""
	from xhtml2pdf import context as pisa_context_mod

	if getattr(pisa_context_mod.pisaContext, "_dsa_font_patched", False):
		return

	original_init = pisa_context_mod.pisaContext.__init__

	def patched_init(self, *args, **kwargs):
		original_init(self, *args, **kwargs)
		try:
			self.fontList[font_name.lower()] = font_name
			for alias in _FONT_ALIASES:
				self.fontList[alias] = font_name
		except Exception:  # noqa: BLE001 - 注入失败不影响渲染（退化为默认字体）
			pass

	patched_init._dsa_wrapped = True  # type: ignore[attr-defined]
	pisa_context_mod.pisaContext.__init__ = patched_init
	pisa_context_mod.pisaContext._dsa_font_patched = True


def _ensure_pdf_fonts() -> Optional[str]:
	"""惰性初始化 PDF 字体环境（线程安全，仅执行一次有效注册）。"""
	global _FONT_READY, _REGISTERED_FONT_NAME

	if _FONT_READY:
		return _REGISTERED_FONT_NAME

	with _FONT_LOCK:
		if _FONT_READY:
			return _REGISTERED_FONT_NAME
		font_name = _register_chinese_font()
		if font_name:
			try:
				_patch_pisa_font_list(font_name)
			except Exception as e:  # noqa: BLE001
				logger.debug("注入 pisa 字体表失败: %s", e)
		_REGISTERED_FONT_NAME = font_name
		_FONT_READY = True
		return font_name


def render_html(markdown_text: str) -> str:
	"""把 Markdown 渲染为完整 HTML 文档字符串（复用通知/邮件同款样式）。"""
	return markdown_to_html_document(markdown_text or "")


def _inject_pdf_font_css(html_document: str, font_name: str) -> str:
	"""在 HTML 文档 <head> 注入 body 字体声明，确保正文使用中文字体。"""
	font_css = (
		"<style>"
		f"body, table, th, td, h1, h2, h3, p, li, blockquote, code, pre {{ font-family: '{font_name}'; }}"
		"</style>"
	)
	if "</head>" in html_document:
		return html_document.replace("</head>", font_css + "</head>", 1)
	# 没有 head 时直接前置
	return font_css + html_document


def render_pdf(markdown_text: str) -> Optional[bytes]:
	"""
	把 Markdown 渲染为 PDF 字节。

	Args:
		markdown_text: Markdown 报告内容。

	Returns:
		PDF 文件字节；失败（依赖缺失/渲染异常）返回 None，调用方应回退。
	"""
	try:
		from xhtml2pdf import pisa
	except ImportError:
		logger.warning("xhtml2pdf 未安装，跳过 PDF 生成。安装：pip install xhtml2pdf")
		return None

	import io

	font_name = _ensure_pdf_fonts() or PDF_FONT_FAMILY
	html_document = render_html(markdown_text)
	html_document = _inject_pdf_font_css(html_document, font_name)

	try:
		buffer = io.BytesIO()
		result = pisa.CreatePDF(src=html_document, dest=buffer, encoding="utf-8")
		if result.err:
			logger.warning("PDF 渲染存在错误 (err=%s)，返回 None 回退", result.err)
			return None
		data = buffer.getvalue()
		if not data:
			logger.warning("PDF 渲染结果为空，返回 None 回退")
			return None
		return data
	except Exception as e:  # noqa: BLE001 - 渲染失败不阻断主流程
		logger.warning("PDF 渲染失败: %s", e)
		return None
