"""Provider 抽象层.

一个文件 = 一个供应商. 放进来即可用, 拔掉即卸载.

核心职责是把各家"OpenAI 兼容"接口的差异吃掉, 对上层只暴露 Delta(text, reasoning).
实测差异 (2026-09-05):
    qwen3.8-max / qwen3.8-flash (gether 网关)  -> delta.reasoning_content
    Qwen3.8-Flash-Next (自建 vLLM)             -> delta.reasoning      ← 非标准
    Qwen3.6-35B        (自建 vLLM)             -> delta.reasoning      ← 非标准
    多数模型                                    -> delta.content
Qwen3.x 在某些 prompt 下会把答案整个放进 reasoning 并包在 <answer></answer> 里,
content 全空 —— 老版 app.py 只读 delta.content, 因此前端一片空白.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Dict, Any, Optional

# 思考字段的所有已知命名, 按优先级探测
REASONING_KEYS = ("reasoning_content", "reasoning", "thinking", "thought")

_ANSWER_RE = re.compile(r"<answer>(.*?)(?:</answer>|$)", re.S)


@dataclass
class Delta:
    """流式增量的统一表示."""
    text: str = ""        # 正文
    reasoning: str = ""   # 思考过程 (前端折叠展示, 不进正文)

    def __bool__(self) -> bool:
        return bool(self.text or self.reasoning)


class ProviderError(RuntimeError):
    pass


class BaseProvider:
    id: str = "base"
    name: str = "Base"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.base_url: str = cfg["base_url"].rstrip("/")
        self.api_key: str = cfg.get("api_key") or "EMPTY"
        # 允许在配置里声明该网关的思考字段名; 为空则自动探测
        self.reasoning_field: Optional[str] = cfg.get("reasoning_field")

    # --- 子类需要实现 -------------------------------------------------
    def stream(self, messages: List[Dict[str, str]], **kw) -> Iterator[Delta]:
        raise NotImplementedError

    def models(self) -> List[str]:
        raise NotImplementedError

    # --- 通用工具 -----------------------------------------------------
    def adapt(self, delta: Dict[str, Any]) -> Delta:
        """把一个原始 delta dict 归一化成 Delta."""
        if self.reasoning_field:
            think = delta.get(self.reasoning_field) or ""
        else:
            think = ""
            for k in REASONING_KEYS:
                v = delta.get(k)
                if v:
                    think = v
                    break
        return Delta(text=delta.get("content") or "", reasoning=think or "")

    @staticmethod
    def salvage(reasoning: str) -> str:
        """兜底: content 全空但 reasoning 有货时, 只从 <answer> 标签里取答案.

        绝不把整段思考当正文返回 —— 那会把"我们需要回答中文用户…"这种内部独白
        写进稿子里 (实测踩过). 取不到就返回空, 由上层关思考重试.
        """
        if not reasoning:
            return ""
        m = _ANSWER_RE.search(reasoning)
        return m.group(1).strip() if m else ""
