"""通知模块：多通道可插拔（遵循 Notifier 接口）。
内置：console（本地打印，零依赖）、serverchan（Server酱）、pushplus（推送加）。
预留：企业微信/邮件（实现同接口即可）。
隐私：仅向你自己的通知服务推送，不经过无关第三方中转（除非你自选第三方通道）。"""
from __future__ import annotations
import json
import urllib.request
import urllib.parse
from typing import Any

from app.services.interfaces import Notifier


def _post_json(url: str, payload: dict) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"ok": True, "status": r.status, "body": r.read().decode("utf-8", "ignore")[:200]}
    except Exception as e:  # 网络/服务异常不阻断系统，仅记录
        return {"ok": False, "error": str(e)}


class ConsoleNotifier:
    """默认通道：本地打印，便于无外部依赖验证。"""
    def send(self, title: str, content: str, config: dict[str, Any]) -> dict[str, Any]:
        print(f"[NOTIFY][{title}]\n{content}")
        return {"ok": True, "channel": "console"}


class ServerChanNotifier:
    def send(self, title: str, content: str, config: dict[str, Any]) -> dict[str, Any]:
        token = config.get("token", "")
        if not token:
            return {"ok": False, "error": "未配置 Server酱 token"}
        return _post_json(f"https://sctapi.ftqq.com/{token}.send",
                          {"title": title, "desp": content})


class PushPlusNotifier:
    def send(self, title: str, content: str, config: dict[str, Any]) -> dict[str, Any]:
        token = config.get("token", "")
        if not token:
            return {"ok": False, "error": "未配置 推送加 token"}
        return _post_json("https://www.pushplus.plus/send",
                          {"token": token, "title": title, "content": content})


_REGISTRY = {
    "console": ConsoleNotifier,
    "serverchan": ServerChanNotifier,
    "pushplus": PushPlusNotifier,
}


def get_notifier(channel: str) -> Notifier:
    cls = _REGISTRY.get(channel, ConsoleNotifier)
    return cls()
