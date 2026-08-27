"""AI 模块：DeepSeekProvider（遵循 AIProvider 接口，可插拔换通义/智谱/本地 Ollama）。
仅在你配置自己的 API Key 后调用；密钥本地加密存储，不对任何未授权平台上报。
第一期用于：选股偏好分析的建议生成。"""
from __future__ import annotations
import json
import urllib.request
from typing import Any

from app.services.interfaces import AIProvider


class DeepSeekProvider:
    def analyze(self, prompt: str, context: dict[str, Any]) -> str:
        api_key = context.get("api_key") or ""
        if not api_key:
            return "（未配置 AI API Key，跳过智能分析）"
        base_url = context.get("base_url", "https://api.deepseek.com").rstrip("/")
        model = context.get("model", "deepseek-chat")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是严谨的 A 股投资顾问助手，仅做专业建议基础，不替用户做投资决策，"
                 "始终强调风险优先、多指标共振、严格止损。回答简洁、结构化、可操作。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 1200,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode("utf-8"))
                return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"（AI 调用失败：{e}）"


def get_ai_provider(kind: str = "deepseek") -> AIProvider:
    return DeepSeekProvider()
