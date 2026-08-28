"""腾讯云 COS 云备份客户端：把整库备份文件异地存储，防 Cloud Studio 重置丢数据。

配置优先级：环境变量 > backend/data/cos.json（本地文件，不入库）。
  环境变量:  COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET / COS_REGION / COS_PREFIX
  cos.json:  {"secret_id": "...", "secret_key": "...", "bucket": "xxx-125xxxx",
              "region": "ap-guangzhou", "prefix": "milktea/backup/"}

不引入新依赖，直接用 httpx + 腾讯云 COS XML API 的 sha1 签名（q-sign-algorithm=sha1）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from app.config import BASE_DIR

# ---------- 配置 ----------
_DEFAULT_PREFIX = "milktea/backup/"
_CONFIG_FILE = BASE_DIR / "data" / "cos.json"


def _load_config() -> dict:
    """合并环境变量与本地配置文件（环境变量优先）。"""
    cfg: dict = {}
    p = Path(_CONFIG_FILE)
    if p.is_file():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    env_map = {
        "secret_id": os.getenv("COS_SECRET_ID"),
        "secret_key": os.getenv("COS_SECRET_KEY"),
        "bucket": os.getenv("COS_BUCKET"),
        "region": os.getenv("COS_REGION"),
        "prefix": os.getenv("COS_PREFIX"),
    }
    for k, v in env_map.items():
        if v:
            cfg[k] = v.strip()
    return cfg


def cos_enabled() -> bool:
    c = _load_config()
    return bool(c.get("secret_id") and c.get("secret_key") and c.get("bucket") and c.get("region"))


def cos_status() -> dict:
    c = _load_config()
    return {
        "enabled": cos_enabled(),
        "bucket": c.get("bucket", ""),
        "region": c.get("region", ""),
        "prefix": c.get("prefix", _DEFAULT_PREFIX),
    }


def _host(cfg: dict) -> str:
    return f"{cfg['bucket']}.cos.{cfg['region']}.myqcloud.com"


def _quote(s: str, safe: str = "") -> str:
    return urllib.parse.quote(s, safe=safe)


def _hmac_sha1_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).hexdigest()


def _sign(cfg: dict, method: str, path: str, query: dict, headers: dict, now: int | None = None) -> str:
    """生成 COS XML API 的 Authorization 头（q-sign-algorithm=sha1）。

    只签 host 头 + query 参数；path 需 URL 编码。
    """
    start = int(now if now is not None else time.time())
    end = start + 600
    key_time = f"{start};{end}"
    sign_key = _hmac_sha1_hex(cfg["secret_key"], key_time)

    # query 参数：按 key 排序，urlencode
    param_items = sorted((k, v) for k, v in query.items() if v is not None)
    param_str = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in param_items)
    # headers：按 key 小写排序
    header_items = sorted((k.lower(), v) for k, v in headers.items())
    header_str = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in header_items)

    http_string = f"{method.lower()}\n{path}\n{param_str}\n{header_str}\n"
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode('utf-8')).hexdigest()}\n"
    signature = _hmac_sha1_hex(sign_key, string_to_sign)

    header_keys = ";".join(k for k, _ in header_items)
    param_keys = ";".join(k for k, _ in param_items)
    return (
        f"q-sign-algorithm=sha1&q-ak={cfg['secret_id']}&q-sign-time={key_time}"
        f"&q-key-time={key_time}&q-header-list={header_keys}"
        f"&q-url-param-list={param_keys}&q-signature={signature}"
    )


def _request(method: str, path: str, query: dict | None = None, body: bytes | None = None,
             timeout: float = 60.0) -> httpx.Response:
    cfg = _load_config()
    if not cos_enabled():
        raise RuntimeError("COS 未配置（缺少 secret_id/secret_key/bucket/region）")
    query = query or {}
    path = path if path.startswith("/") else "/" + path
    host = _host(cfg)
    headers = {"host": host}
    if body is not None:
        headers["content-type"] = "application/json"
    headers["authorization"] = _sign(cfg, method, path, query, headers)
    url = f"https://{host}{path}"
    if query:
        url += "?" + "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, headers=headers, content=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"COS {method} {path} 失败 HTTP {resp.status_code}: {resp.text[:300]}")
    return resp


# ---------- 业务操作 ----------
def object_key(name: str) -> str:
    """备份文件名 -> COS 对象 key（带 prefix）。"""
    cfg = _load_config()
    prefix = cfg.get("prefix") or _DEFAULT_PREFIX
    if not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{name}"


def upload_bytes(name: str, data: bytes) -> dict:
    """上传备份文件内容到 COS。返回对象 key。"""
    key = object_key(name)
    _request("PUT", key, body=data)
    return {"ok": True, "key": key}


def upload_file(name: str, local_path: Path) -> dict:
    data = Path(local_path).read_bytes()
    return upload_bytes(name, data)


def list_backups() -> list[dict]:
    """列出 COS 中 prefix 下的所有备份（新 → 旧）。返回 [{name,size,last_modified}]。"""
    cfg = _load_config()
    prefix = cfg.get("prefix") or _DEFAULT_PREFIX
    if not prefix.endswith("/"):
        prefix += "/"
    resp = _request("GET", "/", {
        "list-type": "2",
        "prefix": prefix,
        "max-keys": "1000",
        "encoding-type": "url",
    })
    root = ET.fromstring(resp.text)
    ns = {"cos": "http://www.qcloud.com/document/product/436/7751"}
    items = []
    for content in root.findall(".//cos:Contents", ns):
        key = content.findtext("cos:Key", default="", namespaces=ns)
        key = urllib.parse.unquote(key)  # encoding-type=url
        if not key.startswith(prefix) or not key.endswith(".json"):
            continue
        name = key[len(prefix):]
        if not name:
            continue
        size = int(content.findtext("cos:Size", default="0", namespaces=ns) or 0)
        last = content.findtext("cos:LastModified", default="", namespaces=ns) or ""
        items.append({"name": name, "size": size, "last_modified": last})
    items.sort(key=lambda x: x["name"], reverse=True)
    return items


def download_bytes(name: str) -> bytes:
    key = object_key(name)
    return _request("GET", key).content


def download_file(name: str, local_path: Path) -> Path:
    data = download_bytes(name)
    Path(local_path).write_bytes(data)
    return Path(local_path)
