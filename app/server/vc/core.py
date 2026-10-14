"""运行时配置、日志、路径校验。

本模块只依赖标准库。所有 TRIM_* 环境变量由飞牛 fnOS 在调用 cmd/main 时注入，
再通过进程环境传递给本服务；VC_* 前缀用于本地开发时覆盖。
"""

import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading

LOG = logging.getLogger("videocut")

# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def env(*names, default=""):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def split_paths(value):
    return [p for p in (value or "").split(":") if p]


class Config:
    """一次性读取运行环境。"""

    def __init__(self):
        self.appname = env("TRIM_APPNAME", default="videocut")
        self.appver = env("TRIM_APPVER", default="0.0.0")

        # 安装后布局: target/server/vc/core.py -> target/server -> target
        # 开发布局:   <proj>/app/server/vc/core.py -> <proj>/app/server -> <proj>/app
        server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_dest = os.path.dirname(server_dir)

        self.appdest = env("VC_APPDEST", "TRIM_APPDEST", default=default_dest)
        self.appdest = os.path.abspath(self.appdest)

        self.socket_path = env("VC_SOCKET", default=os.path.join(self.appdest, "app.sock"))
        self.www_dir = env("VC_WWW", default=os.path.join(self.appdest, "www"))
        self.data_dir = env("VC_DATA", "TRIM_PKGVAR", default=os.path.join(self.appdest, "data"))
        self.temp_dir = env("TRIM_PKGTMP", default=os.path.join(self.data_dir, "tmp"))
        self.gateway_prefix = env("VC_GATEWAY_PREFIX", default="/app/" + self.appname).rstrip("/")

        self.uid = env("TRIM_RUN_UID", "TRIM_UID", default="")
        self.gid = env("TRIM_RUN_GID", "TRIM_GID", default="")
        self.username = env("TRIM_RUN_USERNAME", "TRIM_USERNAME", default="")

        self.sys_version = env("TRIM_SYS_VERSION", default="")
        self.sys_arch = env("TRIM_SYS_ARCH", default="")
        self.sys_language = env("TRIM_SYS_LANGUAGE", default="zh-CN")

        self.api_socket = env(
            "VC_TRIM_API_SOCKET", default="/var/run/trim_open_gateway_apiscope.socket"
        )
        self.api_token = env("TRIM_API_TOKEN", default="")

        # 系统授予本应用的路径（启动时的快照；运行期授权走开放 API）
        self.share_paths = split_paths(env("TRIM_DATA_SHARE_PATHS"))
        self.accessible_paths = split_paths(env("TRIM_DATA_ACCESSIBLE_PATHS"))

        self.ffmpeg = self._find_bin("ffmpeg", "VC_FFMPEG")
        self.ffprobe = self._find_bin("ffprobe", "VC_FFPROBE")

        self.pid_file = os.path.join(self.data_dir, "app.pid")
        self.jobs_file = os.path.join(self.data_dir, "jobs.json")
        self.settings_file = os.path.join(self.data_dir, "settings.json")
        self.thumb_dir = os.path.join(self.data_dir, "thumbs")

        for d in (self.data_dir, self.temp_dir, self.thumb_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:  # pragma: no cover
                pass

    @staticmethod
    def _find_bin(name, override):
        explicit = os.environ.get(override)
        if explicit and os.access(explicit, os.X_OK):
            return explicit
        for c in (
            "/bin/" + name,
            "/usr/bin/" + name,
            "/usr/local/bin/" + name,
            "/var/apps/" + name + "/target/bin/" + name,
            "/vol1/@appcenter/" + name + "/bin/" + name,
        ):
            if os.access(c, os.X_OK):
                return c
        found = shutil.which(name)
        return found or ""

    def socket_dir(self):
        return os.path.dirname(self.socket_path) or "."


CFG = None  # type: Config
_LOCK = threading.Lock()


def config():
    global CFG
    with _LOCK:
        if CFG is None:
            CFG = Config()
        return CFG


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logging():
    cfg = config()
    root = logging.getLogger("videocut")
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(cfg.data_dir, "server.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return root


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

_TRAVERSAL = re.compile(r"(^|/)\.\.($|/)")


class PathError(ValueError):
    pass


class DeniedError(PathError):
    """路径格式合法，但未授权给当前用户/应用。"""


def validate_user_path(raw, must_exist=True):
    """把用户输入路径规范化为绝对路径，并做基础安全检查。

    这里只做语法层面校验（绝对路径、无 .. 穿越、无 NUL）；是否有权访问
    由操作系统权限 + 开放 API 的按用户 ACL 共同决定。
    """
    if not raw or not isinstance(raw, str):
        raise PathError("路径为空")
    if "\x00" in raw:
        raise PathError("路径包含非法字符")
    p = raw.strip()
    if not p.startswith("/"):
        raise PathError("必须是绝对路径")
    if _TRAVERSAL.search(p):
        raise PathError("路径不允许包含 ..")
    # 折叠重复斜杠
    p = re.sub(r"/{2,}", "/", p)
    real = os.path.realpath(p)
    if must_exist and not os.path.exists(real):
        raise PathError("路径不存在：%s" % p)
    return real


def gateway_roots(cfg=None):
    """允许浏览/读取的根目录（应用自身已获授权的路径）。"""
    cfg = cfg or config()
    roots = []
    roots.extend(cfg.accessible_paths)
    roots.extend(cfg.share_paths)
    roots.append(cfg.data_dir)
    out = []
    seen = set()
    for r in roots:
        try:
            rp = os.path.realpath(r)
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        out.append(rp)
    return out


def under_any(path, roots):
    path = os.path.realpath(path)
    for r in roots:
        r = os.path.realpath(r)
        if path == r or path.startswith(r.rstrip("/") + "/"):
            return True
    return False


def is_dir_readable(path):
    return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)
