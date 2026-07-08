# -*- coding: utf-8 -*-
"""Synology Chat notification sender."""

from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from src.config import Config


logger = logging.getLogger(__name__)


def resolve_synology_chat_webhook_url(webhook_url: Optional[str]) -> Optional[str]:
	"""Validate a Synology Chat incoming webhook URL and return it verbatim.

	Synology Chat 的标准 Incoming Webhook 形态为::

		.../webapi/entry.cgi?api=SYNO.Chat.External&method=incoming&version=2&token="xxx"

	token 由 Synology 生成并通常带引号，必须原样保留（不重建 query），
	否则鉴权会失败。这里只做基础校验：必须是 http(s) URL 且 query 中含 token。
	"""
	raw_url = (webhook_url or "").strip()
	if not raw_url:
		return None

	parsed = urlparse(raw_url)
	if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
		return None

	query_params = parse_qs(parsed.query)
	if not any(token.strip().strip('"') for token in query_params.get("token", [])):
		return None

	return raw_url


class SynologyChatSender:
	"""Send text notifications through a Synology Chat incoming webhook."""

	def __init__(self, config: Config):
		self._synology_chat_webhook_url = getattr(config, "synology_chat_webhook_url", None)
		self._webhook_verify_ssl = getattr(config, "webhook_verify_ssl", True)

	def _is_synology_chat_configured(self) -> bool:
		return resolve_synology_chat_webhook_url(self._synology_chat_webhook_url) is not None

	def send_to_synology_chat(
		self,
		content: str,
		title: Optional[str] = None,
		*,
		timeout_seconds: Optional[float] = None,
	) -> bool:
		"""Publish a notification to Synology Chat as a form-encoded ``payload``."""
		endpoint = resolve_synology_chat_webhook_url(self._synology_chat_webhook_url)
		if not endpoint:
			logger.warning("Synology Chat 配置不完整或 Webhook URL 无效，跳过推送")
			return False

		text = content
		if title:
			text = f"**{title}**\n\n{content}"

		# Synology Chat 标准 Incoming Webhook 要求 application/x-www-form-urlencoded，
		# body 为 payload=<URL 编码后的 JSON>，JSON 至少包含 text 字段。
		payload = json.dumps({"text": text}, ensure_ascii=False)
		headers = {"User-Agent": "daily_stock_analysis"}

		try:
			response = requests.post(
				endpoint,
				data={"payload": payload},
				headers=headers,
				timeout=timeout_seconds or 10,
				verify=self._webhook_verify_ssl,
			)
			if 200 <= response.status_code < 300:
				# Synology 在 HTTP 200 下仍可能返回 success=false（如 token 无效）。
				try:
					body = response.json()
				except ValueError:
					body = None
				if isinstance(body, dict) and body.get("success") is False:
					logger.error("Synology Chat 请求被拒绝: %s", body.get("error"))
					return False
				logger.info("Synology Chat 消息发送成功")
				return True

			logger.error("Synology Chat 请求失败: HTTP %s", response.status_code)
			logger.debug("Synology Chat 响应内容: %s", response.text)
			return False
		except requests.exceptions.Timeout:
			logger.error("发送 Synology Chat 消息失败: 请求超时")
			return False
		except requests.exceptions.RequestException as exc:
			logger.error("发送 Synology Chat 消息失败: 网络请求异常")
			logger.debug("Synology Chat 请求异常类型: %s", type(exc).__name__)
			return False
		except Exception as exc:
			logger.error("发送 Synology Chat 消息失败: 未知异常")
			logger.debug("Synology Chat 未知异常类型: %s", type(exc).__name__)
			return False
