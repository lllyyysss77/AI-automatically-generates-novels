"""全局 + 项目级配置. 用户改 config/settings.yaml 即可统一调整所有项目."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "config" / "settings.yaml"

DEFAULTS: Dict[str, Any] = {
    "generation": {"chapter_words_min": 2200, "chapter_words_max": 3000,
                   "chapter_words_tolerance": 0.25, "outline_batch": 10,
                   "max_tokens_draft": 8192, "max_tokens_plan": 8000,
                   "context_budget": "auto", "output_reserve": 8192,
                   "safety_margin": 4000,
                   "min_context_budget": 32000, "max_context_budget": 100000, "temperature_draft": 0.92,
                   "temperature_plan": 0.80},
    "limits": {"max_chapters": 500, "max_total_words": 2_000_000, "daily_call_budget": 0},
    "quality": {"audit_pass_score": 70, "max_rewrites": 1, "hard_fail_on_blacklist": True},
    "memory": {"enabled": True, "top_k": 6, "recent_chapters": 3, "l2_every": 10,
               "index_chapters": True},
    "style_defaults": {"narration": "第三人称限制视角", "tense": "过去时", "extra": ""},
    "banned_global": [],
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(project_overrides: Dict[str, Any] | None = None) -> Dict[str, Any]:
    disk = {}
    if PATH.exists():
        disk = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    return _merge(_merge(DEFAULTS, disk), project_overrides or {})


def save(cfg: Dict[str, Any]) -> None:
    """供 UI 的「全局设置」面板回写."""
    PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
