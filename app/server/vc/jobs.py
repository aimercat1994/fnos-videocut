"""导出任务：先无损剪切各片段，再用 concat 解复用器无损合并。"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from . import media
from .core import LOG, config, gateway_roots, under_any, validate_user_path, PathError

MAX_HISTORY = 60
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff .\-()\[\]（）\[\]]+", re.UNICODE)


class JobError(ValueError):
    pass


def sanitize_filename(name, default="output"):
    name = (name or "").strip()
    name = os.path.basename(name.replace("\\", "/"))
    name = _SAFE_NAME.sub("_", name)
    name = name.strip(" .")
    if not name:
        name = default
    return name[:180]


def split_ext(name):
    base = os.path.basename(name)
    if "." in base[1:]:
        stem, ext = base.rsplit(".", 1)
        return stem, ext.lower()
    return base, ""


def normalize_clip(raw):
    """校验并规范化单个片段。"""
    if not isinstance(raw, dict):
        raise JobError("片段格式不正确")
    path = validate_user_path(raw.get("path"))
    if not os.path.isfile(path):
        raise JobError("不是文件：%s" % path)
    if media.container_of(path) not in media.VIDEO_EXTS:
        raise JobError("不是支持的视频文件：%s" % os.path.basename(path))
    if not os.access(path, os.R_OK):
        raise JobError(
            "应用无权读取该文件（%s）。请在文件管理器中右键该文件→打开方式→无损剪辑，"
            "先授予访问权限。" % os.path.basename(path)
        )
    start = float(raw.get("start") or 0.0)
    end = raw.get("end")
    end = float(end) if end not in (None, "") else None
    if start < 0:
        start = 0.0
    return {"path": path, "start": start, "end": end}


def plan(clips, snap_keyframes=True):
    """为每个片段补充探测信息，得出实际剪切区间。"""
    out = []
    for clip in clips:
        info = media.probe(clip["path"])
        dur = float(info.get("duration") or 0.0)
        start = clip["start"]
        end = clip["end"]
        if end is None or end <= 0 or (dur and end > dur):
            end = dur
        if end is not None and end <= start:
            raise JobError("片段区间无效（起点不早于终点）：%s" % os.path.basename(clip["path"]))

        snapped_from = None
        if snap_keyframes and dur > 0 and start > 0.001:
            kfs = media.keyframes(clip["path"])
            if kfs:
                snapped = media.nearest_keyframe(kfs, start, "before")
                if abs(snapped - start) > 0.001:
                    snapped_from = start
                    start = snapped
        out.append(
            {
                "path": clip["path"],
                "name": os.path.basename(clip["path"]),
                "duration": round(dur, 3),
                "start": round(start, 3),
                "end": round(end, 3) if end is not None else dur,
                "span": round(max(0.0, (end or dur) - start), 3),
                "needCut": start > 0.001 or (end is not None and dur > 0 and end < dur - 0.001),
                "snappedFrom": round(snapped_from, 3) if snapped_from is not None else None,
                "probe": info,
            }
        )
    return out


def preflight(clips, snap_keyframes=True):
    """导出前的兼容性检查，供前端提示。"""
    plans = plan(clips, snap_keyframes=snap_keyframes)
    warnings = []
    keys = ("codec", "width", "height", "pixFmt")
    akeys = ("codec", "channels", "sampleRate")

    base_v = None
    base_a = None
    for p in plans:
        info = p["probe"]
        v = info.get("video") or {}
        a = info.get("audio") or None
        vk = tuple(v.get(k) for k in keys)
        ak = tuple(a.get(k) for k in akeys) if a else None
        if base_v is None:
            base_v, base_a = vk, ak
            if not v:
                warnings.append("%s 没有视频轨道" % p["name"])
        else:
            if vk != base_v:
                warnings.append(
                    "「%s」的视频参数与第一个片段不同（%s vs %s），无损合并后可能出现花屏或音画不同步"
                    % (p["name"], _fmt_v(v), _fmt_v(base_v))
                )
            if ak != base_a:
                warnings.append(
                    "「%s」的音频参数与第一个片段不同，无损合并后可能出现无声或音画不同步" % p["name"]
                )
        if p.get("snappedFrom") is not None:
            warnings.append(
                "「%s」起点已吸附到关键帧 %.3fs（原设定 %.3fs）" % (p["name"], p["start"], p["snappedFrom"])
            )

    total = round(sum(p["span"] for p in plans), 3)
    first = plans[0]["probe"] if plans else {}
    suggested = first.get("suggestedExt") or "mkv"

    return {
        "plans": [
            {
                "name": p["name"],
                "path": p["path"],
                "start": p["start"],
                "end": p["end"],
                "span": p["span"],
                "needCut": p["needCut"],
                "duration": p["duration"],
                "snappedFrom": p["snappedFrom"],
                "videoCodec": (p["probe"].get("video") or {}).get("codec"),
                "width": (p["probe"].get("video") or {}).get("width"),
                "height": (p["probe"].get("video") or {}).get("height"),
            }
            for p in plans
        ],
        "totalDuration": total,
        "warnings": warnings,
        "compatible": not any("不同" in w for w in warnings),
        "suggestedExt": suggested,
    }


def _fmt_v(v):
    if isinstance(v, tuple):
        codec, w, h, _pix = (list(v) + [None] * 4)[:4]
        return "%s %sx%s" % (codec or "?", w or "?", h or "?")
    return str(v)


def resolve_output_dir(raw, clips):
    """校验输出目录：必须存在、可写，且在已授权范围内。"""
    allowed_extra = set()
    for c in clips:
        try:
            allowed_extra.add(os.path.dirname(validate_user_path(c)))
        except PathError:
            pass

    if raw:
        out_dir = validate_user_path(raw)
    else:
        out_dir = allowed_extra and sorted(allowed_extra)[0] or config().data_dir

    if not os.path.isdir(out_dir):
        raise JobError("输出目录不存在：%s" % out_dir)
    if not os.access(out_dir, os.W_OK | os.X_OK):
        raise JobError(
            "应用无权写入该目录：%s。可改用应用自己的共享目录（输出目录留空），"
            "或在文件管理器中先授权该目录。" % out_dir
        )
    if not under_any(out_dir, gateway_roots()) and out_dir not in allowed_extra:
        raise JobError("输出目录不在已授权范围内：%s" % out_dir)
    return out_dir


def unique_output(out_dir, stem, ext, overwrite=False):
    name = "%s.%s" % (stem, ext)
    target = os.path.join(out_dir, name)
    if overwrite and os.path.exists(target):
        return target
    i = 1
    while os.path.exists(target):
        target = os.path.join(out_dir, "%s (%d).%s" % (stem, i, ext))
        i += 1
    return target


class Manager:
    def __init__(self):
        self._jobs = []
        self._lock = threading.RLock()
        self._save_timer = None
        self._load()

    # -- 持久化 ------------------------------------------------------------
    def _load(self):
        cfg = config()
        try:
            with open(cfg.jobs_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                for j in data:
                    if j.get("state") in ("pending", "running"):
                        j["state"] = "interrupted"
                        j["message"] = "服务重启，任务已中断"
                    j.pop("_proc", None)
                    self._jobs.append(j)
        except (OSError, ValueError):
            pass

    def _save(self):
        cfg = config()
        try:
            tmp = cfg.jobs_file + ".tmp"
            with self._lock:
                payload = [self._public(j) for j in self._jobs[-MAX_HISTORY:]]
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, cfg.jobs_file)
        except (OSError, ValueError) as exc:
            LOG.info("save jobs failed: %s", exc)

    def _schedule_save(self):
        def go():
            time.sleep(0.6)
            self._save()

        t = threading.Timer(0.6, go)
        t.daemon = True
        t.start()

    def _public(self, job):
        out = {}
        for k, v in job.items():
            if k.startswith("_"):
                continue
            out[k] = v
        return out

    # -- 查询 --------------------------------------------------------------
    def list(self):
        with self._lock:
            return [self._public(j) for j in reversed(self._jobs)]

    def get(self, jid):
        with self._lock:
            for j in self._jobs:
                if j["id"] == jid:
                    return j
        return None

    def public(self, jid):
        j = self.get(jid)
        return self._public(j) if j else None

    def remove(self, jid):
        with self._lock:
            job = None
            for j in self._jobs:
                if j["id"] == jid:
                    job = j
                    break
            if job is None:
                return False
            if job["state"] in ("pending", "running"):
                raise JobError("任务正在进行中，请先取消")
            self._jobs.remove(job)
        self._save()
        return True

    def cancel(self, jid):
        job = self.get(jid)
        if not job:
            raise JobError("任务不存在")
        if job["state"] not in ("pending", "running"):
            raise JobError("任务已结束")
        job["_cancel"].set()
        proc = job.get("_proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        job["message"] = "正在取消…"
        return True

    # -- 创建 --------------------------------------------------------------
    def create(self, clips, options, owner):
        if not clips:
            raise JobError("片段列表为空")
        if len(clips) > 200:
            raise JobError("片段数量过多（上限 200）")

        snap = bool(options.get("snapKeyframes", True))
        normalized = [normalize_clip(c) for c in clips]
        compatible = True
        try:
            pf = preflight(normalized, snap_keyframes=snap)
            compatible = pf["compatible"]
        except (JobError, PathError):
            raise
        if not compatible and not options.get("force"):
            raise JobError(
                "片段之间的编码参数不一致，无损合并可能失败。请确认后勾选「仍然合并」，"
                "或改为分别导出。"
            )

        out_dir = resolve_output_dir(options.get("outDir") or "", [c["path"] for c in normalized])

        if options.get("filename"):
            stem, forced_ext = split_ext(sanitize_filename(options["filename"]))
        else:
            stem, forced_ext = split_ext(normalized[0]["path"])
            if len(normalized) > 1:
                stem = "%s_合并" % stem
            else:
                stem = "%s_cut" % stem

        ext = (options.get("ext") or forced_ext or "").lower().lstrip(".")
        if ext not in media.VIDEO_EXTS:
            ext = "mkv"
        stem = sanitize_filename(stem) or "output"

        overwrite = bool(options.get("overwrite"))
        target = unique_output(out_dir, stem, ext, overwrite=overwrite)

        job = {
            "id": uuid.uuid4().hex[:12],
            "kind": "export",
            "state": "pending",
            "progress": 0.0,
            "message": "排队中",
            "createdAt": int(time.time()),
            "startedAt": None,
            "finishedAt": None,
            "owner": {"uid": str(owner.get("uid") or ""), "username": owner.get("username") or ""},
            "clips": [
                {"path": c["path"], "name": os.path.basename(c["path"]), "start": c["start"], "end": c["end"]}
                for c in normalized
            ],
            "options": {
                "snapKeyframes": snap,
                "keepAllStreams": bool(options.get("keepAllStreams")),
                "force": bool(options.get("force")),
            },
            "output": {"dir": out_dir, "name": os.path.basename(target), "path": target, "ext": ext},
            "warnings": pf["warnings"],
            "steps": [],
            "error": "",
            "log": "",
            "duration": pf["totalDuration"],
            "_cancel": threading.Event(),
        }

        with self._lock:
            self._jobs.append(job)
        self._schedule_save()

        t = threading.Thread(target=self._run, args=(job,), name="job-%s" % job["id"], daemon=True)
        t.start()
        return self._public(job)

    # -- 执行 --------------------------------------------------------------
    def _run(self, job):
        job["state"] = "running"
        job["startedAt"] = int(time.time())
        job["message"] = "准备中"
        self._schedule_save()

        cfg = config()
        tmpdir = os.path.join(cfg.temp_dir, job["id"])
        try:
            os.makedirs(tmpdir, exist_ok=True)
            plans = plan(
                [{"path": c["path"], "start": c["start"], "end": c["end"]} for c in job["clips"]],
                snap_keyframes=job["options"]["snapKeyframes"],
            )
            self._execute(job, plans, tmpdir)
        except PathError as exc:
            self._fail(job, str(exc))
            return
        except media.MediaError as exc:
            self._fail(job, str(exc))
            return
        except JobError as exc:
            self._fail(job, str(exc))
            return
        except Exception as exc:  # pragma: no cover
            LOG.exception("job %s crashed", job["id"])
            self._fail(job, "内部错误：%s" % exc)
            return
        finally:
            if not job["options"].get("keepTemp"):
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _execute(self, job, plans, tmpdir):
        out_path = job["output"]["path"]
        out_ext = job["output"]["ext"]
        total_span = sum(p["span"] for p in plans) or 1.0
        cuts = [p for p in plans if p["needCut"]]

        if job["_cancel"].is_set():
            return self._cancel_done(job)

        if len(plans) == 1 and cuts:
            # 单片段且有剪切：直接输出，省掉一次临时拷贝
            job["message"] = "无损剪切中"
            p = plans[0]
            args = media.cut_command(
                p["path"], out_path, p["start"], p["end"],
                keep_all_streams=job["options"]["keepAllStreams"],
            )
            ok = self._run_ffmpeg(job, args, p["span"], 0.0, 1.0)
            return self._finish(job, out_path) if ok else None

        if len(plans) == 1:
            # 单片段且无需剪切：仅做一次无损重封装到目标容器
            job["message"] = "无损重封装中"
            p = plans[0]
            list_file = os.path.join(tmpdir, "list.txt")
            _write_list(list_file, [p["path"]])
            args = media.concat_command(list_file, out_path, fmt=_container_fmt(out_ext))
            ok = self._run_ffmpeg(job, args, p["span"], 0.0, 1.0)
            return self._finish(job, out_path) if ok else None

        # 多片段：先剪切需要的片段，再合并
        cut_weight = 0.65 if cuts else 0.0
        concat_weight = 1.0 - cut_weight

        parts = []
        done = 0.0
        for idx, p in enumerate(plans):
            if job["_cancel"].is_set():
                return self._cancel_done(job)
            if p["needCut"]:
                part = os.path.join(tmpdir, "%03d.%s" % (idx, _part_ext(p, out_ext)))
                job["message"] = "剪切 %s（%d/%d）" % (p["name"], idx + 1, len(plans))
                base = cut_weight * (done / total_span)
                span_frac = cut_weight * (p["span"] / total_span)
                args = media.cut_command(
                    p["path"], part, p["start"], p["end"],
                    keep_all_streams=job["options"]["keepAllStreams"],
                )
                if not self._run_ffmpeg(job, args, p["span"], base, span_frac):
                    return None
                parts.append(part)
            else:
                parts.append(p["path"])
            done += p["span"]

        if job["_cancel"].is_set():
            return self._cancel_done(job)

        job["message"] = "无损合并 %d 个片段" % len(parts)
        job["steps"] = [{"name": os.path.basename(x), "path": x} for x in parts]
        list_file = os.path.join(tmpdir, "list.txt")
        _write_list(list_file, parts)
        args = media.concat_command(list_file, out_path, fmt=_container_fmt(out_ext))
        ok = self._run_ffmpeg(job, args, total_span, 1.0 - concat_weight, concat_weight)
        if not ok:
            return None
        return self._finish(job, out_path)

    def _run_ffmpeg(self, job, args, expected, base, weight):
        """执行 ffmpeg 并把进度映射到 [base, base+weight]。返回是否成功。"""
        job["args"] = args
        LOG.info("job %s exec: %s", job["id"], " ".join(args))
        errfile = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=errfile,
                bufsize=1,
                universal_newlines=True,
            )
        except OSError as exc:
            errfile.close()
            self._fail(job, "无法启动 ffmpeg：%s" % exc)
            return False

        job["_proc"] = proc
        last = 0.0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if job["_cancel"].is_set() and proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                kv = media.parse_progress_line(line)
                if not kv:
                    continue
                key, value = kv
                if key == "progress" and value == "end":
                    self._set_progress(job, base + weight)
                    continue
                t = media.out_time_seconds(key, value)
                if t is None or expected <= 0:
                    continue
                frac = max(0.0, min(1.0, t / expected))
                if frac - last >= 0.002 or frac >= 1.0:
                    last = frac
                    self._set_progress(job, base + weight * frac)
            rc = proc.wait()
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            job["_proc"] = None

        errfile.seek(0)
        err = errfile.read().decode("utf-8", "replace")
        errfile.close()
        job["log"] = err[-4000:]

        if job["_cancel"].is_set():
            self._cancel_done(job)
            return False
        if rc != 0:
            tail = "\n".join([ln for ln in err.strip().splitlines() if ln.strip()][-6:])
            self._fail(job, "ffmpeg 执行失败（退出码 %s）\n%s" % (rc, tail))
            return False
        self._set_progress(job, base + weight)
        return True

    def _set_progress(self, job, value):
        job["progress"] = round(max(0.0, min(100.0, value * 100.0)), 2)
        self._schedule_save()

    def _finish(self, job, out_path):
        try:
            size = os.path.getsize(out_path)
        except OSError:
            size = 0
        try:
            info = media.probe(out_path, use_cache=False)
            actual_duration = info.get("duration")
            job["result"] = {
                "duration": actual_duration,
                "videoCodec": (info.get("video") or {}).get("codec"),
                "width": (info.get("video") or {}).get("width"),
                "height": (info.get("video") or {}).get("height"),
            }
        except media.MediaError:
            job["result"] = None
        job["output"]["size"] = size
        job["state"] = "done"
        job["progress"] = 100.0
        job["message"] = "已完成"
        job["finishedAt"] = int(time.time())
        LOG.info("job %s done -> %s (%s bytes)", job["id"], out_path, size)
        self._save()

    def _fail(self, job, message):
        job["state"] = "failed"
        job["message"] = "失败"
        job["error"] = message
        job["finishedAt"] = int(time.time())
        LOG.warning("job %s failed: %s", job["id"], message)
        self._save()

    def _cancel_done(self, job):
        job["state"] = "canceled"
        job["message"] = "已取消"
        job["finishedAt"] = int(time.time())
        out = job.get("output", {}).get("path")
        if out and os.path.exists(out) and job.get("progress", 0) < 100:
            try:
                if os.path.getsize(out) > 0:
                    # 保留不完整文件的线索，但不删除用户已有数据
                    pass
            except OSError:
                pass
        self._save()


def _container_fmt(ext):
    return {
        "mp4": "mp4",
        "m4v": "mp4",
        "mov": "mov",
        "mkv": "matroska",
        "webm": "webm",
        "ts": "mpegts",
        "flv": "flv",
        "avi": "avi",
        "wmv": "asf",
    }.get(ext)


def _part_ext(p, out_ext):
    # 临时片段尽量沿用各自源容器，避免中间重封装带来的兼容问题
    src_ext = media.container_of(p["path"])
    if src_ext in ("mp4", "m4v", "mov", "mkv", "webm", "ts", "flv", "avi"):
        return src_ext
    return out_ext if out_ext in ("mp4", "mkv") else "mkv"


def _write_list(list_file, parts):
    with open(list_file, "w", encoding="utf-8") as fh:
        for p in parts:
            # concat 解复用器使用单引号内联路径，内部单引号需要转义
            fh.write("file '%s'\n" % p.replace("'", "'\\''"))


MANAGER = None


def manager():
    global MANAGER
    if MANAGER is None:
        MANAGER = Manager()
    return MANAGER
