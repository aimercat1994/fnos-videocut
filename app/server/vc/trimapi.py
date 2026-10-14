"""飞牛 fnOS 开放 API 客户端。

后端通过 Unix Socket 调用 `POST /api/v1/trimapp`，token 由系统在调用应用脚本时
注入到环境变量 TRIM_API_TOKEN 中（每次现读，不落盘、不下发前端）。
"""

import http.client
import json
import os
import socket
import threading
import uuid

from .core import LOG, config

API_PATH = "/api/v1/trimapp"


class TrimApiError(Exception):
    def __init__(self, code, msg, http_status=0):
        super().__init__("trimapi code=%s msg=%s" % (code, msg))
        self.code = code
        self.msg = msg
        self.http_status = http_status


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


_lock = threading.Lock()
_last_error = None
_ok_count = 0


def available():
    cfg = config()
    return bool(cfg.api_token) and os.path.exists(cfg.api_socket)


def status():
    cfg = config()
    return {
        "socket": cfg.api_socket,
        "socketExists": os.path.exists(cfg.api_socket),
        "tokenPresent": bool(cfg.api_token),
        "okCalls": _ok_count,
        "lastError": _last_error,
    }


def call(req, data=None, timeout=20):
    """调用一个开放 API req，成功返回 data 字段，失败抛 TrimApiError。"""
    global _last_error, _ok_count
    cfg = config()
    # 每次调用都重新读取，token 可能在重装/重注册后变化
    token = os.environ.get("TRIM_API_TOKEN") or cfg.api_token
    if not token:
        raise TrimApiError(0, "缺少 TRIM_API_TOKEN，无法调用开放 API")
    if not os.path.exists(cfg.api_socket):
        raise TrimApiError(0, "开放 API Socket 不存在：%s" % cfg.api_socket)

    payload = {
        "reqId": uuid.uuid4().hex,
        "req": req,
        "appName": cfg.appname,
        "data": data if data is not None else {},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    with _lock:
        conn = _UnixHTTPConnection(cfg.api_socket, timeout)
        try:
            conn.request(
                "POST",
                API_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            status_code = resp.status
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            _last_error = "连接开放 API 失败：%s" % exc
            raise TrimApiError(0, _last_error, 0)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        msg = "开放 API 返回无法解析：%s" % raw[:200]
        _last_error = msg
        raise TrimApiError(0, msg, status_code)

    code = parsed.get("code", -1)
    msg = parsed.get("msg", "")
    if status_code != 200 or code != 0:
        text = msg or _http_message(status_code)
        _last_error = "code=%s msg=%s" % (code, text)
        LOG.warning("trimapi %s -> http=%s code=%s msg=%s", req, status_code, code, text)
        raise TrimApiError(code, text, status_code)

    _last_error = None
    _ok_count += 1
    return parsed.get("data")


def _http_message(status_code):
    return {
        400: "Invalid Params",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        500: "Internal Error",
    }.get(status_code, "Unknown")


# ---------------------------------------------------------------------------
# 具体接口封装
# ---------------------------------------------------------------------------


def get_platform_config():
    return call("trim.system.getPlatformConfig")


def check_user_acl(uid, path):
    """检查某个用户对路径的读/写/删权限。

    返回 [{path, readable, writable, deletable}, ...]
    """
    data = call("trim.file.checkUserACL", {"uid": str(uid), "path": path})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("list"), list):
            return data["list"]
        if "path" in data:
            return [data]
    return []


def get_user_accessible_folders(uid):
    """用户已授权给本应用的目录。"""
    data = call("trim.file.getUserAccessibleFolders", {"uid": str(uid)})
    return _as_path_list(data)


def get_shared_accessible_folders():
    """管理员授予本应用的共享目录（仅管理员可调用）。"""
    data = call("trim.file.getSharedAccessibleFolders")
    return _as_path_list(data)


def del_user_accessible_folder(uid, path):
    return call("trim.file.delUserAccessibleFolder", {"uid": str(uid), "path": path})


def convert_path(path, language=None):
    data = call("trim.file.convertPath", {"path": path, "language": language or config().sys_language})
    return data


def _as_path_list(data):
    out = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("paths") or data.get("list") or data.get("folders") or []
    else:
        items = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            p = it.get("path") or it.get("semanticPath")
            if p:
                out.append(p)
    return out
