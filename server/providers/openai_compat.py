"""OpenAI 兼容供应商 (vLLM / 各类网关 / DeepSeek / 通义 / 302.AI / Ollama ...).

绝大多数国内外服务都走这个类, 差异靠 providers.yaml 里的字段声明吃掉.
"""
from __future__ import annotations

import json
from typing import Iterator, List, Dict, Any

import requests

from .base import BaseProvider, Delta, ProviderError


class OpenAICompatProvider(BaseProvider):
    id = "openai_compat"
    name = "OpenAI 兼容"

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def models(self) -> List[str]:
        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=15)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception as e:  # 网关不可用不应该炸掉整个应用
            raise ProviderError(f"拉取模型列表失败: {e}") from e

    def stream(self, messages: List[Dict[str, str]], **kw) -> Iterator[Delta]:
        """流式生成. 关键点见 base.py 的模块注释.

        kw: model / temperature / max_tokens / thinking(bool)
        """
        thinking = kw.pop("thinking", False)
        body: Dict[str, Any] = {
            "model": kw.pop("model", self.cfg.get("default_model")),
            "messages": messages,
            "stream": True,
            "temperature": kw.pop("temperature", 0.85),
            "max_tokens": kw.pop("max_tokens", self.cfg.get("max_tokens", 8192)),
        }
        # 关思考: 各家开关名不统一, 全都带上, 不认的会被忽略
        if not thinking:
            body["enable_thinking"] = False
            body["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            body["enable_thinking"] = True
        body.update(kw)

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
                stream=True,
                timeout=(20, 600),
            )
        except Exception as e:
            raise ProviderError(f"连接失败: {e}") from e

        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            payload = raw[6:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            d = self.adapt(choices[0].get("delta") or {})
            if d:
                yield d
