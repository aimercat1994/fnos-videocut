"""路径授权判定。

三层来源：
1. 启动时系统注入的 TRIM_DATA_ACCESSIBLE_PATHS / TRIM_DATA_SHARE_PATHS（应用自身目录）；
2. 运行期通过开放 API `trim.file.getUserAccessibleFolders` 取得的、系统授予当前用户的目录；
3. 用户从文件管理器「打开方式」显式交给本应用的单个文件（URL 上的 `path` 参数）。

第 3 层是必要的：文件打开方式注册后，系统会把该文件交给应用，但它未必出现在
目录级授权列表里。记录范围严格限定为「用户明确点击打开过的那个文件」。
"""

import json
import os
import threading
import time

from .core import LOG, config, gateway_roots, under_any
from . import trimapi

TTL = 15.0
MAX_OPENED = 300


class Grants:
    def __init__(self):
        self._lock = threading.RLock()
        self._cache = {}      # uid -> (ts, [paths])
        self._opened = []     # [{"uid":..., "path":..., "ts":...}]，最近的在前
        self._dirs = []       # 用户用系统选择器显式授权的目录，同上
        self._load()

    # -- 持久化 ------------------------------------------------------------
    def _store(self):
        return os.path.join(config().data_dir, "granted-files.json")

    def _load(self):
        try:
            with open(self._store(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, list):          # 兼容旧格式：只有文件列表
            self._opened = [d for d in data if isinstance(d, dict) and d.get("path")][:MAX_OPENED]
            return
        if isinstance(data, dict):
            for key, attr in (("files", "_opened"), ("dirs", "_dirs")):
                items = [d for d in (data.get(key) or []) if isinstance(d, dict) and d.get("path")]
                setattr(self, attr, items[:MAX_OPENED])

    def _save(self):
        try:
            tmp = self._store() + ".tmp"
            with self._lock:
                payload = {"files": self._opened[:MAX_OPENED], "dirs": self._dirs[:MAX_OPENED]}
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self._store())
        except (OSError, ValueError) as exc:
            LOG.info("save grants failed: %s", exc)

    # -- 用户显式打开的文件 -------------------------------------------------
    def note_opened(self, path, uid):
        """记录用户通过文件管理器显式交给本应用的文件。"""
        try:
            real = os.path.realpath(path)
        except OSError:
            return
        if not os.path.isfile(real) or not os.access(real, os.R_OK):
            return
        uid = str(uid or "")
        with self._lock:
            self._opened = [d for d in self._opened if not (d["path"] == real and d["uid"] == uid)]
            self._opened.insert(0, {"uid": uid, "path": real, "ts": int(time.time())})
            self._opened = self._opened[:MAX_OPENED]
        self._save()
        LOG.info("noted opened file for uid=%s: %s", uid, real)

    def _is_opened(self, path, uid):
        uid = str(uid or "")
        with self._lock:
            for d in self._opened:
                if d["uid"] == uid and d["path"] == path:
                    return True
        return False

    # -- 用户在本应用内显式授权的目录 --------------------------------------
    # 系统原生选择器把目录登记为「该用户授权给本应用」，但那一份数据要通过
    # 开放 API 再查一次才可见，而且 TRIM_DATA_ACCESSIBLE_PATHS 只在应用启动
    # 时注入一次。所以本应用自己记住用户刚授权的目录，浏览立即可用，
    # 不必等应用重启。
    def note_dir(self, path, uid):
        try:
            real = os.path.realpath(path)
        except OSError:
            return False
        if not os.path.isdir(real) or not os.access(real, os.R_OK | os.X_OK):
            return False
        uid = str(uid or "")
        with self._lock:
            self._dirs = [d for d in self._dirs if not (d["path"] == real and d["uid"] == uid)]
            self._dirs.insert(0, {"uid": uid, "path": real, "ts": int(time.time())})
            self._dirs = self._dirs[:MAX_OPENED]
            self._cache.pop(uid, None)
        self._save()
        LOG.info("noted granted dir for uid=%s: %s", uid, real)
        return True

    def _dirs_for(self, uid):
        uid = str(uid or "")
        with self._lock:
            return [d["path"] for d in self._dirs if d["uid"] == uid]

    # -- 目录级授权 --------------------------------------------------------
    def roots_for(self, uid):
        uid = str(uid or "")
        roots = _merge(gateway_roots(), self._dirs_for(uid))
        if not uid:
            return roots

        now = time.time()
        with self._lock:
            hit = self._cache.get(uid)
            if hit and now - hit[0] < TTL:
                return _merge(roots, hit[1])

        extra = []
        if trimapi.available():
            try:
                extra = trimapi.get_user_accessible_folders(uid)
            except trimapi.TrimApiError as exc:
                LOG.info("getUserAccessibleFolders failed: %s", exc)
            except Exception as exc:  # pragma: no cover
                LOG.info("getUserAccessibleFolders error: %s", exc)

        with self._lock:
            self._cache[uid] = (now, extra)
        return _merge(roots, extra)

    def invalidate(self, uid=None):
        """丢弃目录授权缓存，让下一次 roots_for 重新向系统查询。

        用户刚在系统原生选择器里授权了目录时调用：系统把它登记为
        「该用户授权给本应用的目录」，只有重新查询才能立刻看到。
        """
        with self._lock:
            if uid is None:
                self._cache.clear()
            else:
                self._cache.pop(str(uid or ""), None)

    def allowed(self, path, uid):
        """路径是否允许访问（目录级授权，或用户显式打开过该文件）。"""
        try:
            real = os.path.realpath(path)
        except OSError:
            return False
        if under_any(real, self.roots_for(uid)):
            return True
        return self._is_opened(real, uid)

    def describe(self, uid):
        """给前端的根目录列表。"""
        cfg = config()
        out = []
        seen = set()
        for r in self.roots_for(uid):
            if r in seen:
                continue
            seen.add(r)
            label = r
            if r == cfg.data_dir:
                label = "%s（应用数据）" % r
            out.append({"path": r, "label": label})
        return out


def _merge(base, extra):
    out = []
    seen = set()
    for r in list(base) + list(extra):
        try:
            rp = os.path.realpath(r)
        except OSError:
            continue
        if rp in seen or not os.path.exists(rp):
            continue
        seen.add(rp)
        out.append(rp)
    return out


GRANTS = None
_LOCK = threading.Lock()


def grants():
    global GRANTS
    with _LOCK:
        if GRANTS is None:
            GRANTS = Grants()
        return GRANTS
