"""统一网关的 HTTP 服务：监听 Unix Socket，提供静态页面与 JSON API。"""

import json
import mimetypes
import os
import posixpath
import re
import socket
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import grants, jobs, media, trimapi
from .core import (
    LOG,
    DeniedError,
    PathError,
    config,
    is_dir_readable,
    under_any,
    validate_user_path,
)

MAX_BODY = 2 * 1024 * 1024
STREAM_CHUNK = 256 * 1024


def _wait_readable(path, tries=6, interval=0.25):
    """系统刚授予的路径，本应用进程的可读性未必立即生效（授权登记与内核
    xattr 生效之间可能有毫秒级延迟），因此短暂重试后再判定失败。"""
    for attempt in range(tries):
        if os.access(path, os.R_OK):
            return True
        if attempt < tries - 1:
            time.sleep(interval)
    return False

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/wasm", ".wasm")


class Api:
    """与传输层无关的业务实现，便于单独测试。"""

    def __init__(self):
        self.grants = grants.grants()

    def require(self, raw, user):
        """校验路径合法、且已授权给当前用户。"""
        path = validate_user_path(raw)
        if not self.grants.allowed(path, user.get("uid")):
            raise DeniedError(
                "该路径未授权给本应用。请在文件管理器中右键文件 → 打开方式 → 无损剪辑，"
                "或先授权所在目录。"
            )
        return path

    def context(self, user):
        cfg = config()
        return {
            "appname": cfg.appname,
            "version": cfg.appver,
            "gatewayPrefix": cfg.gateway_prefix,
            "lang": cfg.sys_language,
            "system": {"version": cfg.sys_version, "arch": cfg.sys_arch},
            "user": {
                "uid": user.get("uid", ""),
                "username": user.get("username", ""),
                "isAdmin": user.get("isAdmin", False),
            },
            "ffmpeg": media.ffmpeg_version(),
            "openApi": trimapi.status(),
            "roots": self.grants.describe(user.get("uid")),
            "dataDir": cfg.data_dir,
            "shareDir": _default_share_dir(),
        }

    def authorize(self, payload, user):
        """登记前端用系统原生选择器（pickUserFile）刚刚取得的授权结果。

        系统对「文件」的授权不会出现在 trim.file.getUserAccessibleFolders 里，只能由
        前端把选择器返回的路径回传；对「目录」的授权则由系统登记，这里让缓存失效即可
        立刻查到。回传的路径仍要过一遍文件系统实际可读性，前端说什么不算数。
        """
        raw_paths = payload.get("paths")
        if not isinstance(raw_paths, list):
            raise jobs.JobError("paths 必须是数组")
        uid = user.get("uid")
        accepted, rejected, saw_dir = [], [], False
        for raw in raw_paths[:200]:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                path = validate_user_path(raw, must_exist=True)
            except PathError as exc:
                rejected.append({"path": raw, "reason": str(exc)})
                continue
            if os.path.isdir(path):
                saw_dir = True
                self.grants.note_dir(path, uid)
                accepted.append({"path": path, "type": "dir"})
                continue
            if not os.path.isfile(path):
                rejected.append({"path": path, "reason": "不是普通文件"})
                continue
            # 本应用只处理视频：非视频扩展名一律不接受登记，避免前端把任意
            # 可读文件（例如 /etc/passwd）塞进授权表。
            if media.container_of(path) not in media.VIDEO_EXTS:
                rejected.append({"path": path, "reason": "不是受支持的视频文件"})
                continue
            if not _wait_readable(path):
                rejected.append({"path": path, "reason": "本应用当前无权读取该文件，请在该文件上右键「打开方式 → 无损剪辑」重新授权"})
                continue
            self.grants.note_opened(path, uid)
            accepted.append({"path": path, "type": "file"})
        if saw_dir:
            self.grants.invalidate(uid)
        LOG.info("authorize: uid=%s accepted=%d rejected=%d", uid, len(accepted), len(rejected))
        return {
            "accepted": accepted,
            "rejected": rejected,
            "roots": self.grants.describe(uid),
        }

    def list_dir(self, raw, only_dirs, user):
        path = validate_user_path(raw)
        roots = self.grants.roots_for(user.get("uid"))
        if not under_any(path, roots):
            raise DeniedError("该目录未授权给本应用，请先在文件管理器中打开其中的文件，或授权该目录")
        if not is_dir_readable(path):
            raise PathError("无法读取目录：%s" % path)

        dirs, files = [], []
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
        except OSError as exc:
            raise PathError("无法读取目录：%s" % exc)

        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                dirs.append({"name": entry.name, "path": entry.path, "type": "dir"})
                continue
            if only_dirs:
                continue
            ext = media.container_of(entry.name)
            if ext not in media.VIDEO_EXTS:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": entry.name,
                    "path": entry.path,
                    "type": "file",
                    "ext": ext,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "readable": os.access(entry.path, os.R_OK),
                }
            )
        return {"path": path, "parent": _parent_within(path, roots), "dirs": dirs, "files": files}

    def file_info(self, raw, user):
        return media.probe(self.require(raw, user))

    def keyframes(self, raw, user):
        path = self.require(raw, user)
        kfs = media.keyframes(path)
        return {"path": path, "keyframes": kfs, "count": len(kfs)}

    def _resolve_clips(self, payload, user):
        raw = payload.get("clips") or []
        if not raw:
            raise jobs.JobError("片段列表为空")
        clips = []
        for c in raw:
            if not isinstance(c, dict):
                raise jobs.JobError("片段格式不正确")
            c = dict(c)
            c["path"] = self.require(c.get("path"), user)
            clips.append(c)
        return [jobs.normalize_clip(c) for c in clips]

    def preflight(self, payload, user):
        return jobs.preflight(
            self._resolve_clips(payload, user),
            snap_keyframes=bool(payload.get("snapKeyframes", True)),
        )

    def create_job(self, payload, user):
        return jobs.manager().create(
            self._resolve_clips(payload, user), payload.get("options") or {}, user
        )

    def list_jobs(self):
        return {"jobs": jobs.manager().list()}

    def get_job(self, jid):
        job = jobs.manager().public(jid)
        if job is None:
            raise jobs.JobError("任务不存在")
        return job

    def cancel_job(self, jid):
        jobs.manager().cancel(jid)
        return {"ok": True}

    def remove_job(self, jid):
        jobs.manager().remove(jid)
        return {"ok": True}


def _default_share_dir():
    cfg = config()
    for p in cfg.share_paths:
        if os.path.isdir(p):
            return p
    return cfg.data_dir


def _parent_within(path, roots):
    parent = os.path.dirname(path.rstrip("/"))
    if parent and parent != path and under_any(parent, roots):
        return parent
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "videocut"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 ----------------------------------------------------------
    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _user(self):
        uid = (self.headers.get("X-Trim-Userid") or "").strip()
        name = (self.headers.get("X-Trim-Username") or "").strip()
        is_admin = (self.headers.get("X-Trim-Isadmin") or "").strip().lower() == "true"
        if not uid:
            if os.environ.get("VC_DEV"):
                return {"uid": "0", "username": name or "dev", "isAdmin": True}
            return None
        return {"uid": uid, "username": name, "isAdmin": is_admin}

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False))

    def _error(self, status, message, code=None):
        self._send(status, json.dumps({"error": message, "code": code or status}, ensure_ascii=False))

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise jobs.JobError("Content-Length 无效")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise jobs.JobError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise jobs.JobError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise jobs.JobError("请求体必须是 JSON 对象")
        return data

    def _path(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        path = posixpath.normpath(path)
        prefix = config().gateway_prefix
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix):]
        query = urllib.parse.parse_qs(parsed.query)
        return path, {k: v[-1] for k, v in query.items()}

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            path, query = self._path()
        except Exception:
            return self._error(400, "请求路径无效")

        try:
            if path.startswith("/api/"):
                return self._api(method, path, query)
            if method == "GET":
                return self._static(path, query)
            return self._error(405, "不支持的方法")
        except DeniedError as exc:
            return self._error(403, str(exc))
        except PathError as exc:
            return self._error(400, str(exc))
        except jobs.JobError as exc:
            return self._error(400, str(exc))
        except media.MediaError as exc:
            return self._error(500, str(exc))
        except trimapi.TrimApiError as exc:
            return self._error(403 if exc.code in (1000002, 200003) else 502, exc.msg, exc.code)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover
            LOG.exception("request failed: %s %s", method, path)
            return self._error(500, "内部错误：%s" % exc)

    def _clientlog(self):
        body = self._read_body()
        kind = str(body.get("kind") or "client")[:60]
        try:
            text = json.dumps(body.get("data"), ensure_ascii=False)
        except Exception:
            text = repr(body.get("data"))
        LOG.info("client[%s] %s", kind, text[:4000])
        return self._json({"ok": True})

    def _api(self, method, path, query):
        # 前端诊断日志：不需要登录用户，便于排查宿主 SDK / 选择器在真机上的行为
        if path == "/api/clientlog" and method == "POST":
            return self._clientlog()

        user = self._user()
        if user is None:
            return self._error(401, "未获取到登录用户信息，请从飞牛桌面或文件管理器打开本应用")
        api = self.server.api

        if path == "/api/context" and method == "GET":
            return self._json(api.context(user))

        if path == "/api/health":
            return self._json({"ok": True, "version": config().appver})

        if path == "/api/browse" and method == "GET":
            return self._json(
                api.list_dir(query.get("path") or "", query.get("dirs") == "1", user)
            )

        if path == "/api/authorize" and method == "POST":
            return self._json(api.authorize(self._read_body(), user))

        if path == "/api/file" and method == "GET":
            return self._json(api.file_info(query.get("path") or "", user))

        if path == "/api/keyframes" and method == "GET":
            return self._json(api.keyframes(query.get("path") or "", user))

        if path == "/api/preflight" and method == "POST":
            return self._json(api.preflight(self._read_body(), user))

        if path == "/api/jobs":
            if method == "GET":
                return self._json(api.list_jobs())
            if method == "POST":
                return self._json(api.create_job(self._read_body(), user), status=201)

        m = re.match(r"^/api/jobs/([A-Za-z0-9_-]{4,32})(?:/(cancel))?$", path)
        if m:
            jid, action = m.group(1), m.group(2)
            if action == "cancel" and method == "POST":
                return self._json(api.cancel_job(jid))
            if method == "GET":
                return self._json(api.get_job(jid))
            if method == "DELETE":
                return self._json(api.remove_job(jid))

        if path == "/api/thumb" and method == "GET":
            return self._thumbnail(query, user)

        if path == "/api/stream" and method == "GET":
            return self._serve_media(query.get("path") or "", False, user)

        if path == "/api/download" and method == "GET":
            return self._serve_media(query.get("path") or "", True, user)

        return self._error(404, "接口不存在：%s" % path)

    # -- 静态与媒体 --------------------------------------------------------
    def _static(self, path, query):
        cfg = config()
        if path in ("/", "/index.html", "/app"):
            return self._index(query)
        rel = path.lstrip("/")
        if not rel or rel.startswith("api/"):
            return self._error(404, "资源不存在")
        target = validate_user_path(os.path.join(cfg.www_dir, rel), must_exist=True)
        if not under_any(target, [cfg.www_dir]) or not os.path.isfile(target):
            return self._error(404, "资源不存在")
        return self._file_response(target)

    def _index(self, query):
        cfg = config()
        index = os.path.join(cfg.www_dir, "index.html")
        self._note_opened((query or {}).get("path"))
        try:
            with open(index, "r", encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            return self._error(500, "前端资源缺失：%s" % index)
        html = html.replace("__PREFIX__", cfg.gateway_prefix).replace("__VERSION__", cfg.appver)
        return self._send(200, html, "text/html; charset=utf-8")

    def _note_opened(self, raw):
        """文件管理器「打开方式」会把 path 附加到入口 URL，这里登记该文件。"""
        if not raw:
            return
        user = self._user()
        if not user:
            return
        try:
            path = validate_user_path(raw)
            self.server.api.grants.note_opened(path, user.get("uid"))
        except Exception as exc:  # 记录失败不影响页面
            LOG.info("ignore opened path %r: %s", raw, exc)

    def _file_response(self, target):
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error(404, "资源不存在")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        return self._send(200, body, ctype)

    def _thumbnail(self, query, user):
        raw = query.get("path") or ""
        path = validate_user_path(raw)
        if not self.server.api.grants.allowed(path, user.get("uid")):
            return self._error(403, "该文件未授权给本应用")
        try:
            t = float(query.get("t") or 0)
        except ValueError:
            t = 0.0
        try:
            width = max(48, min(640, int(query.get("w") or 160)))
        except ValueError:
            width = 160
        out = media.thumbnail(path, t, width)
        if not out:
            return self._error(404, "无法生成缩略图")
        return self._file_response(out)

    def _serve_media(self, raw, download, user):
        path = validate_user_path(raw)
        if not os.path.isfile(path):
            return self._error(404, "文件不存在")
        if not self.server.api.grants.allowed(path, user.get("uid")):
            return self._error(403, "该文件未授权给本应用，请先授予访问权限")
        self._send_file(path, download=download)

    def _send_file(self, path, download=False):
        try:
            st = os.stat(path)
        except OSError:
            return self._error(404, "文件不存在")
        total = st.st_size
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"

        start, end = 0, max(0, total - 1)
        status = 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.split(",")[0].strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), total - 1)
                elif m.group(2):
                    start = max(0, total - int(m.group(2)))
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % total)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        if download:
            name = os.path.basename(path)
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + urllib.parse.quote(name),
            )
        self.end_headers()
        if self.command == "HEAD":
            return

        remaining = length
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client aborted transfer: %s", path)


class UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, socket_path):
        self.api = Api()
        self.socket_path = socket_path
        super().__init__(socket_path, Handler)

    def server_bind(self):
        if os.path.exists(self.socket_path):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.5)
                probe.connect(self.socket_path)
                probe.close()
                raise RuntimeError("Socket 已被占用：%s" % self.socket_path)
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                pass
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        parent = os.path.dirname(self.socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 绕开 HTTPServer.server_bind：它会把 Unix 路径当作 host/port 解析
        socketserver.TCPServer.server_bind(self)
        self.server_name = "videocut"
        self.server_port = 0
        try:
            os.chmod(self.socket_path, 0o660)
        except OSError:
            pass

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)

    def server_close(self):
        try:
            super().server_close()
        finally:
            try:
                if os.path.exists(self.socket_path):
                    os.unlink(self.socket_path)
            except OSError:
                pass


def serve(socket_path, ready=None):
    server = UnixHTTPServer(socket_path)
    if ready:
        ready(server)
    LOG.info("listening on unix socket %s", socket_path)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def serve_in_thread(socket_path, ready=None):
    t = threading.Thread(target=serve, args=(socket_path, ready), daemon=True)
    t.start()
    return t
