"""ffmpeg / ffprobe 封装：探测、关键帧、缩略图、无损剪切与合并命令构造。"""

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import threading
import time

from .core import LOG, config

VIDEO_EXTS = {
    "mp4", "mkv", "mov", "avi", "flv", "ts", "m2ts", "mts", "webm", "wmv",
    "mpg", "mpeg", "m4v", "3gp", "vob", "rmvb", "rm", "asf", "ogv", "divx",
    "f4v", "m2v", "mxf", "dv", "trp", "tp", "m3u8", "mp2", "mpe", "mpv",
}

# mp4 容器能承载的编解码（用于推荐输出容器）
MP4_VIDEO = {"h264", "hevc", "av1", "mpeg4", "mjpeg", "vp9"}
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "mp2"}

# 浏览器 <video> 原生可播放的容器；其余（TS/MTS/AVI/FLV/RMVB/MKV…）预览前需先转封装成 MP4
# 注意：Chromium 不解 Matroska，.mkv 不能算「浏览器可播放的容器」
PLAYABLE_EXTS = {"mp4", "m4v", "mov", "webm", "3gp"}

# 浏览器内核（Chromium/Chrome）能解码的视频参数；超出这个范围就必须转码，转封装没有用
BROWSER_VIDEO_CODECS = {"h264"}
BROWSER_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12"}

# 媒体扩展名 → Content-Type（Python 自带映射把 .ts 当成 Qt 语言文件，必须覆盖）
CTYPES = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "3gp": "video/3gpp",
    "ts": "video/mp2t",
    "m2ts": "video/mp2t",
    "mts": "video/mp2t",
    "trp": "video/mp2t",
    "tp": "video/mp2t",
    "avi": "video/x-msvideo",
    "divx": "video/x-msvideo",
    "flv": "video/x-flv",
    "f4v": "video/mp4",
    "wmv": "video/x-ms-wmv",
    "asf": "video/x-ms-asf",
    "mpg": "video/mpeg",
    "mpeg": "video/mpeg",
    "mpe": "video/mpeg",
    "m2v": "video/mpeg",
    "mpv": "video/mpeg",
    "mp2": "video/mpeg",
    "vob": "video/mpeg",
    "rmvb": "application/vnd.rn-realmedia-vbr",
    "rm": "application/vnd.rn-realmedia",
    "ogv": "video/ogg",
    "mxf": "application/mxf",
    "dv": "video/x-dv",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}

_PROBE_CACHE = {}
_PROBE_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 30.0


class MediaError(RuntimeError):
    pass


def _bin(name):
    cfg = config()
    path = getattr(cfg, name, "")
    if not path:
        raise MediaError("未找到 %s 可执行文件，请确认飞牛系统媒体组件已安装" % name)
    return path


def run(args, timeout=120, capture=True):
    """同步执行外部命令，返回 (rc, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise MediaError("命令执行超时：%s" % args[0])
    except OSError as exc:
        raise MediaError("无法执行命令 %s：%s" % (args[0], exc))
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    return proc.returncode, out, err


def ffmpeg_version():
    try:
        rc, out, _ = run([_bin("ffmpeg"), "-version"], timeout=15)
        if rc == 0 and out:
            return out.splitlines()[0]
    except MediaError:
        pass
    return ""


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


def _stat_key(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def probe(path, use_cache=True):
    key = _stat_key(path)
    if use_cache:
        with _PROBE_CACHE_LOCK:
            hit = _PROBE_CACHE.get(path)
            if hit and hit[0] == key and time.time() - hit[1] < _CACHE_TTL:
                return hit[2]

    rc, out, err = run(
        [
            _bin("ffprobe"),
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        timeout=120,
    )
    if rc != 0:
        raise MediaError("无法解析媒体信息：%s" % (err.strip().splitlines()[-1] if err.strip() else "未知错误"))

    try:
        raw = json.loads(out)
    except ValueError:
        raise MediaError("ffprobe 输出无法解析")

    info = _summarize(path, raw)
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[path] = (key, time.time(), info)
    return info


def _summarize(path, raw):
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []

    video = None
    audios = []
    others = []
    for s in streams:
        ctype = s.get("codec_type")
        entry = {
            "index": s.get("index"),
            "type": ctype,
            "codec": s.get("codec_name") or "unknown",
            "codecLong": s.get("codec_long_name") or "",
            "profile": s.get("profile") or "",
            "bitRate": _int(s.get("bit_rate")),
            "language": (s.get("tags") or {}).get("language") or "",
            "title": (s.get("tags") or {}).get("title") or "",
        }
        if ctype == "video":
            entry.update(
                {
                    "width": _int(s.get("width")),
                    "height": _int(s.get("height")),
                    "pixFmt": s.get("pix_fmt") or "",
                    "fps": _fps(s.get("avg_frame_rate") or "")
                    or _fps(s.get("r_frame_rate") or ""),
                    "timeBase": s.get("time_base") or "",
                    "nbFrames": _int(s.get("nb_frames")),
                    "dvProfile": s.get("dv_profile"),
                    "colorTransfer": s.get("color_transfer") or "",
                }
            )
            if entry["width"]:
                if video is None:
                    video = entry
                else:
                    others.append(entry)
            else:
                others.append(entry)
        elif ctype == "audio":
            entry.update(
                {
                    "channels": _int(s.get("channels")),
                    "channelLayout": s.get("channel_layout") or "",
                    "sampleRate": _int(s.get("sample_rate")),
                }
            )
            audios.append(entry)
        else:
            others.append(entry)

    duration = _float(fmt.get("duration"))
    if not duration and video:
        duration = _float(video.get("duration"))
    if not duration:
        # 回退：用最后一个包的时长不可靠，直接报 0
        duration = 0.0

    size = _int(fmt.get("size")) or _stat_key(path)[1]
    bitrate = _int(fmt.get("bit_rate"))

    st = os.stat(path)
    result = {
        "path": path,
        "name": os.path.basename(path),
        "dir": os.path.dirname(path),
        "ext": _ext(path),
        "size": size,
        "mtime": int(st.st_mtime),
        "duration": round(duration, 3),
        "startTime": round(_float(fmt.get("start_time")), 3),
        "bitRate": bitrate,
        "formatName": fmt.get("format_name") or "",
        "formatLongName": fmt.get("format_long_name") or "",
        "formatTags": {
            k: v for k, v in (fmt.get("tags") or {}).items() if k.lower() in
            ("title", "artist", "comment", "creation_time", "encoder")
        },
        "video": video,
        "audio": audios[0] if audios else None,
        "audios": audios,
        "streams": others,
        "suggestedExt": suggest_ext(video, audios),
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
    }
    return result


def file_info(path):
    """探测 + 预览方案：前端拿到结果就知道该直接播原片、转封装还是转码。"""
    info = probe(path)
    plan = preview_plan(path)
    info["previewMode"] = plan["mode"]
    info["previewReady"] = plan["ready"]
    info["previewReason"] = plan["reason"]
    info["needsPreview"] = plan["mode"] != "play"
    return info


def content_type(path):
    """按扩展名返回 Content-Type（覆盖 Python 的错误映射，例如 .ts）。"""
    ext = _ext(path)
    return CTYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def needs_preview(path):
    """浏览器 <video> 放不了这个容器，预览前需要先转封装成 MP4。"""
    return _ext(path) not in PLAYABLE_EXTS


def _ext(path):
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[1].lower()


def suggest_ext(video, audios):
    vcodec = (video or {}).get("codec") or ""
    if vcodec in MP4_VIDEO:
        acodecs = {a.get("codec") for a in audios}
        if not acodecs or acodecs <= MP4_AUDIO:
            return "mp4"
    return "mkv"


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fps(v):
    if not v or v == "0/0":
        return 0.0
    try:
        if "/" in v:
            num, den = v.split("/", 1)
            den = float(den)
            return round(float(num) / den, 3) if den else 0.0
        return round(float(v), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ---------------------------------------------------------------------------
# 关键帧
# ---------------------------------------------------------------------------


_KF_NEAR_CACHE = {}


def keyframes_near(path, t, before=20.0, after=10.0, timeout=30):
    """只扫描 t 附近一小段，返回其中的关键帧时间点。

    整片 `-skip_frame nokey` 扫描在几分钟的 4K/2K 录像要几十秒，导出请求会
    看起来「点了没反应」；吸附只需要 t 之前最近的那个关键帧，扫一个 GOP 就够。
    """
    try:
        st = os.stat(path)
    except OSError:
        return []
    t = max(0.0, float(t or 0.0))
    t0 = max(0.0, t - before)
    span = (t - t0) + after
    if span <= 0:
        return []
    key = (os.path.realpath(path), st.st_mtime_ns, st.st_size, round(t0, 1), round(span, 1))
    hit = _KF_NEAR_CACHE.get(key)
    if hit is not None:
        return list(hit)
    try:
        rc, out, err = run(
            [
                _bin("ffprobe"),
                "-v", "error",
                "-select_streams", "v:0",
                "-skip_frame", "nokey",
                "-read_intervals", "%.3f%%+%.3f" % (t0, span),
                "-show_frames",
                "-show_entries", "frame=pts_time",
                "-of", "csv=p=0",
                path,
            ],
            timeout=timeout,
        )
    except MediaError:
        return []
    if rc != 0:
        LOG.info("keyframes_near failed for %s: %s", path, err.strip()[:200])
        return []
    times = []
    for line in out.splitlines():
        # 央视/直播录像的关键帧带 SEI 边数据，csv 行会变成 "578.718000,H.26[45] ..."
        tok = line.split(",", 1)[0].strip()
        try:
            times.append(round(float(tok), 3))
        except ValueError:
            continue
    times.sort()
    if len(_KF_NEAR_CACHE) > 512:
        _KF_NEAR_CACHE.clear()
    _KF_NEAR_CACHE[key] = tuple(times)
    return times


def keyframes(path, limit=400, timeout=90):
    """返回视频关键帧时间点（秒）。失败返回 []。"""
    try:
        rc, out, err = run(
            [
                _bin("ffprobe"),
                "-v", "error",
                "-select_streams", "v:0",
                "-skip_frame", "nokey",
                "-show_frames",
                "-show_entries", "frame=pts_time",
                "-of", "csv=p=0",
                path,
            ],
            timeout=timeout,
        )
    except MediaError:
        return []
    if rc != 0:
        LOG.info("keyframes failed for %s: %s", path, err.strip()[:200])
        return []

    times = []
    for line in out.splitlines():
        tok = line.strip().rstrip(",")
        if not tok or tok == "N/A":
            continue
        try:
            times.append(round(float(tok), 3))
        except ValueError:
            continue

    if limit and len(times) > limit:
        step = len(times) / float(limit)
        picked = []
        i = 0.0
        while int(i) < len(times):
            picked.append(times[int(i)])
            i += step
        if picked and picked[-1] != times[-1]:
            picked.append(times[-1])
        times = picked
    return times


def nearest_keyframe(times, t, mode="before"):
    """在关键帧列表中找 <= t（before）或 >= t（after）的最近关键帧。"""
    if not times:
        return t
    if mode == "after":
        for x in times:
            if x >= t - 1e-6:
                return x
        return times[-1]
    best = times[0]
    for x in times:
        if x <= t + 1e-6:
            best = x
        else:
            break
    return best


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------


def thumb_path(path, t, width=160):
    cfg = config()
    key = "%s|%s|%s|%s" % (path, _stat_key(path), round(float(t), 3), width)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(cfg.thumb_dir, "%s.jpg" % digest)


def thumbnail(path, t, width=160):
    """生成（或复用）指定时间点的缩略图，返回文件路径或 None。"""
    out = thumb_path(path, t, width)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    tmp = out + ".%d.part" % os.getpid()
    try:
        rc, _o, err = run(
            [
                _bin("ffmpeg"),
                "-hide_banner", "-loglevel", "error", "-y",
                "-ss", "%.3f" % max(0.0, float(t)),
                "-i", path,
                "-frames:v", "1",
                "-vf", "scale=%d:-2" % width,
                "-f", "image2",
                "-q:v", "4",
                tmp,
            ],
            timeout=60,
        )
        if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            LOG.info("thumbnail failed t=%s: %s", t, err.strip()[:200])
            return None
        os.replace(tmp, out)
        return out
    except (MediaError, OSError) as exc:
        LOG.info("thumbnail error t=%s: %s", t, exc)
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


# ---------------------------------------------------------------------------
# 预览副本：浏览器放不了的容器（TS/MTS/AVI/FLV/RMVB…）→ 可播放的 MP4
#   两种做法：
#     remux  —— -c copy 无损转封装，只改容器，秒级完成
#     encode —— 重新编码成 H.264/AAC，码流本身浏览器解不了时使用（坏帧、10bit、4:2:2…）
#   源文件永远不动，剪切/合并仍然只针对原文件做 -c copy。


def preview_folder():
    folder = os.path.join(config().thumb_dir, "preview")
    os.makedirs(folder, exist_ok=True)
    return folder


def preview_path(path, kind="remux"):
    """预览副本的缓存路径（源文件 size/mtime 变化会换新缓存）。"""
    key = "%s|%s" % (os.path.realpath(path), _stat_key(path))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    name = "%s.mp4" % digest if kind != "encode" else "%s-compat.mp4" % digest
    return os.path.join(preview_folder(), name)


# 预览任务进度：{预览副本路径: {"progress": 0-100, "started": ts, "mode": "encode", "phase": str}}
_PREVIEW_STATE = {}
_STATE_LOCK = threading.Lock()


def preview_state(out):
    with _STATE_LOCK:
        st = _PREVIEW_STATE.get(out)
        return dict(st) if st else None


def _set_preview_state(out, **kw):
    with _STATE_LOCK:
        st = _PREVIEW_STATE.setdefault(out, {})
        st.update(kw)


def clear_preview_state(out):
    with _STATE_LOCK:
        _PREVIEW_STATE.pop(out, None)


def set_preview_error(out, message):
    """标记预览生成失败，前端据此停下来提示，而不是无限轮询。"""
    with _STATE_LOCK:
        st = _PREVIEW_STATE.setdefault(out, {})
        st.update({"phase": "失败", "error": str(message)[:300]})


# 转封装出来的副本浏览器还是解不了的文件：记住这次失败，别再重试转封装
_FORCE_ENCODE = set()


def force_encode(path, remux_out=""):
    """转封装行不通（副本码流依旧不被浏览器接受）时改走转码，并清掉没用的副本。"""
    _FORCE_ENCODE.add(path)
    mark_needs_encode(path)
    if remux_out:
        try:
            if os.path.exists(remux_out):
                os.remove(remux_out)
        except OSError:
            pass
    LOG.info("preview: %s 转封装副本不可播，改为转码", path)


# 抽样严格解码：用于校验「转封装出来的副本浏览器能不能播」。
# Chromium 的解码器遇到一个坏包就整条流水线 PIPELINE_ERROR_DECODE 放弃，
# 而 ffmpeg 命令行会隐藏坏帧继续解码，所以只能用 -err_detect explode + -xerror 还原浏览器的严格程度。
# 注意：抽样不可能覆盖整条码流，判定「不能播」可信，判定「能播」只是大概率——
# 所以「浏览器能不能直接播原片」这件事最终以播放器自己的报错为准（见 mark_needs_encode）。
DECODE_PROBE_WINDOWS = (0.02, 0.5)      # 抽样位置（占时长比例）
DECODE_PROBE_SECONDS = 2.5
_DECODE_CACHE = {}
_DECODE_LOCK = threading.Lock()


def decode_probe(path):
    """抽样严格解码，返回 (ok, 首个出问题的秒数)。结果按 文件+size/mtime 缓存。"""
    key = _stat_key(path)
    with _DECODE_LOCK:
        hit = _DECODE_CACHE.get(path)
        if hit and hit[0] == key:
            return hit[1]

    info = probe(path)
    video = info.get("video") or {}
    codec = (video.get("codec") or "").lower()
    pix = (video.get("pixFmt") or "").lower()
    if codec and codec not in BROWSER_VIDEO_CODECS:
        result = (False, 0.0)
        LOG.info("decode probe: %s 编码 %s 浏览器不支持", path, codec)
    elif pix and pix not in BROWSER_PIX_FMTS:
        result = (False, 0.0)
        LOG.info("decode probe: %s 像素格式 %s 浏览器不支持", path, pix)
    else:
        duration = float(info.get("duration") or 0)
        offsets = (
            [round(duration * r, 3) for r in DECODE_PROBE_WINDOWS]
            if duration > 0 else [0.0]
        )
        args0 = [
            _bin("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
            "-xerror", "-err_detect", "explode",
        ]
        result = (True, None)
        for off in offsets:
            args = args0 + [
                "-ss", "%.3f" % off, "-t", "%.3f" % DECODE_PROBE_SECONDS, "-i", path,
                "-map", "0:v:0?", "-f", "null", "-",
            ]
            try:
                rc, _o, err = run(args, timeout=180)
            except MediaError as exc:
                LOG.info("decode probe skipped for %s: %s", path, exc)
                result = (True, None)
                break
            if rc != 0:
                result = (False, off)
                LOG.info(
                    "decode probe failed at %.1fs for %s: %s",
                    off, path, (err or "").strip()[:200],
                )
                break

    with _DECODE_LOCK:
        _DECODE_CACHE[path] = (key, result)
    return result


_ENCODER_LOCK = threading.Lock()
_ENCODER_CACHE = {}


def video_encoder():
    """挑一个可用的 H.264 编码器：优先 Intel QSV 硬编，退回 libx264。

    QSV 除了 ffmpeg 里编进 h264_qsv，还要能打开 /dev/dri/renderD128；
    应用以低权包用户运行时若没加入 render 组（config/privilege 的 join-groups），
    硬编会初始化失败，所以这里先看设备权限。
    """
    with _ENCODER_LOCK:
        if "enc" in _ENCODER_CACHE:
            return _ENCODER_CACHE["enc"]
        enc = "libx264"
        try:
            rc, out, _err = run([_bin("ffmpeg"), "-hide_banner", "-encoders"], timeout=30)
            if rc == 0 and "h264_qsv" in out and _render_device_ok():
                enc = "h264_qsv"
            elif rc == 0 and "h264_qsv" in out:
                LOG.info("h264_qsv 可用但没有 /dev/dri 访问权限（应用用户缺 render 组？），改用 libx264")
        except MediaError:
            pass
        _ENCODER_CACHE["enc"] = enc
        return enc


def _render_device_ok():
    """当前进程能不能读写 Intel 核显的渲染节点（QSV/VAAPI 都要它）。"""
    for dev in ("/dev/dri/renderD128", "/dev/dri/renderD129"):
        if os.path.exists(dev):
            return os.access(dev, os.R_OK | os.W_OK)
    return False


def encode_attempts():
    """按优先级返回 (编码器名, 编码参数) 列表，前一个失败就自动换下一个。"""
    libx264 = ("libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"])
    if video_encoder() != "h264_qsv":
        return [libx264]
    return [
        ("h264_qsv", ["-c:v", "h264_qsv", "-global_quality", "24", "-preset", "fast"]),
        libx264,
    ]


# 已经确认「浏览器直接播不了」的文件（播放器报过错）：记在应用数据目录里，
# 下次打开同一个文件直接走兼容预览，不再让用户先看一次黑屏。
_HINT_LOCK = threading.RLock()
_HINT_CACHE = {"file": None, "data": None}


def _hint_path():
    return os.path.join(config().data_dir, "preview-hints.json")


def _hints():
    with _HINT_LOCK:
        path = _hint_path()
        if _HINT_CACHE["file"] == path and _HINT_CACHE["data"] is not None:
            return _HINT_CACHE["data"]
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
        _HINT_CACHE["file"] = path
        _HINT_CACHE["data"] = data
        return data


def hinted_encode(path):
    """这个文件之前被浏览器明确拒绝过（且文件没变），直接走兼容预览。"""
    hints = _hints()
    return hints.get(os.path.realpath(path)) == "%s|%s" % _stat_key(path)


def mark_needs_encode(path):
    """记下「浏览器解不了这份码流」，下次打开直接生成兼容预览。"""
    real = os.path.realpath(path)
    with _HINT_LOCK:
        data = _hints()
        key = "%s|%s" % _stat_key(path)
        if data.get(real) == key:
            return
        data[real] = key
        try:
            os.makedirs(os.path.dirname(_hint_path()), exist_ok=True)
            tmp = _hint_path() + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, _hint_path())
            _HINT_CACHE["file"] = _hint_path()
            _HINT_CACHE["data"] = data
        except OSError as exc:
            LOG.info("save preview hint failed: %s", exc)


# 播放区提示里显示的几句人话
ENCODE_REASON = "浏览器解码失败，转码一份只用于预览的兼容副本"
REMUX_REASON = "容器不受支持，先无损转封装"


def preview_plan(path, want="auto"):
    """决定这个文件怎么交给浏览器播放。

    返回 {"mode": "play"|"remux"|"encode", "out": 预览副本路径, "ready": bool, "reason": 说明}
    """
    want = (want or "auto").lower()
    enc = preview_path(path, "encode")
    if os.path.exists(enc):
        return {"mode": "encode", "out": enc, "ready": True, "reason": "已有兼容预览"}
    if want == "encode":
        return {"mode": "encode", "out": enc, "ready": False, "reason": ENCODE_REASON}
    if want != "remux" and (path in _FORCE_ENCODE or hinted_encode(path)):
        return {"mode": "encode", "out": enc, "ready": False, "reason": ENCODE_REASON}
    if _ext(path) not in PLAYABLE_EXTS:
        rem = preview_path(path, "remux")
        return {
            "mode": "remux", "out": rem, "ready": os.path.exists(rem),
            "reason": REMUX_REASON,
        }
    if want == "remux":
        rem = preview_path(path, "remux")
        return {"mode": "remux", "out": rem, "ready": os.path.exists(rem), "reason": ""}
    return {"mode": "play", "out": "", "ready": True, "reason": ""}


def prune_previews(keep_days=14):
    """清掉长期没用的预览副本，避免缓存无限增长。"""
    try:
        folder = preview_folder()
        names = os.listdir(folder)
    except OSError:
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for name in names:
        if not name.endswith(".mp4"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def build_preview(path, out, timeout=3600):
    """无损转封装（-c copy）成浏览器可播放的 MP4，并保留原始时间轴。"""
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    tmp = "%s.%d.part" % (out, os.getpid())
    args = [
        _bin("ffmpeg"),
        "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-fflags", "+genpts", "-i", path,
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy", "-copyts",
        "-movflags", "+faststart", "-f", "mp4", tmp,
    ]
    rc, _out, err = run(args, timeout=timeout)
    if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise MediaError(
            "预览转封装失败：%s"
            % ((err or "").strip()[:300] or ("ffmpeg 退出码 %s" % rc))
        )
    os.replace(tmp, out)
    return out


def build_encode_preview(path, out, timeout=14400):
    """把源重新编码成浏览器一定能播的 H.264/AAC MP4（只做预览，源文件不动）。"""
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out

    info = probe(path)
    duration = float(info.get("duration") or 0)
    audio = info.get("audio") or {}
    if (audio.get("codec") or "").lower() in ("aac", "mp3"):
        aenc = ["-c:a", "copy"]
    else:
        aenc = ["-c:a", "aac", "-b:a", "160k"]

    tmp = "%s.%d.part" % (out, os.getpid())
    last_err = ""
    for enc, venc in encode_attempts():
        args = [
            _bin("ffmpeg"),
            "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-fflags", "+genpts", "-i", path,
            "-map", "0:v:0?", "-map", "0:a:0?",
        ] + venc + [
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        ] + aenc + [
            "-copyts", "-movflags", "+faststart", "-f", "mp4",
            "-progress", "pipe:1", "-nostats", tmp,
        ]
        LOG.info("encode preview: %s -> %s (%s)", path, out, enc)
        _set_preview_state(out, progress=0.0, started=time.time(), mode="encode", phase="编码中")
        rc, err = _run_with_progress(args, duration, out, timeout)
        if rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            _set_preview_state(out, progress=100.0, phase="完成")
            return out
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        last_err = (err or "").strip()
        LOG.warning("encode preview with %s failed: %s", enc, last_err[-300:])

    clear_preview_state(out)
    tail = [ln for ln in last_err.splitlines() if ln.strip()][-1:]
    raise MediaError(
        "兼容预览转码失败：%s" % (tail[0][:300] if tail else "ffmpeg 没有输出")
    )


def _run_with_progress(args, expected, state_key, timeout):
    """执行带 -progress 输出的 ffmpeg，把进度写进 _PREVIEW_STATE，返回 (rc, stderr)。"""
    import tempfile

    errfile = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=errfile,
            bufsize=1, universal_newlines=True,
        )
    except OSError as exc:
        errfile.close()
        return -1, "无法启动 ffmpeg：%s" % exc

    started = time.time()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.time() - started > timeout:
                try:
                    proc.kill()
                except OSError:
                    pass
                break
            kv = parse_progress_line(line)
            if not kv:
                continue
            key, value = kv
            if key == "progress" and value == "end":
                _set_preview_state(state_key, progress=100.0)
                continue
            t = out_time_seconds(key, value)
            if t is None or expected <= 0:
                continue
            progress = max(0.0, min(100.0, t / expected * 100.0))
            _set_preview_state(state_key, progress=round(progress, 1), phase="编码中")
        rc = proc.wait(timeout=timeout)
    finally:
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        errfile.seek(0)
        err = errfile.read().decode("utf-8", "replace")
        errfile.close()
    return rc, err


def preview_stream_variants(path, start=0.0):
    """按优先级返回 [(编码器名, ffmpeg 参数)]：QSV 起不来时自动换 libx264 再试。"""
    return [
        (enc, preview_stream_command(path, start, encoder=enc))
        for enc, _venc in encode_attempts()
    ]


def preview_stream_command(path, start=0.0, encoder=None):
    """用 ffmpeg 一边解码一边重编码，吐出能直接喂给 <video> 的分片 MP4。

    浏览器自己的 H.264 解码器遇到坏包就整条流水线报错，那就把解码搬到服务端：
    ffmpeg 解码（顺手把坏帧藏掉）→ H.264/AAC 分片 MP4 → 浏览器只负责渲染。
    每 0.5 秒 flush 一个分片，所以请求发出后大约 1 秒就能出画面；拖动进度时
    前端带着新的 start 重新请求一次即可，不需要先整片转码。
    """
    enc = encoder or video_encoder()
    if enc == "h264_qsv":
        venc = ["-c:v", "h264_qsv", "-global_quality", "24", "-preset", "veryfast"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"]

    args = [
        _bin("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-fflags", "+genpts",
    ]
    try:
        start = float(start)
    except (TypeError, ValueError):
        start = 0.0
    if start > 0:
        # 输入侧 seek：ffmpeg 会从最近的关键帧开始解码并丢掉 start 之前的帧，出画时间只多几十毫秒
        args += ["-ss", "%.3f" % start]
    args += ["-i", path, "-map", "0:v:0?", "-map", "0:a:0?"]
    args += venc + ["-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1"]
    args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    args += [
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "500000",
        "-avoid_negative_ts", "make_zero",
        "-f", "mp4", "pipe:1",
    ]
    return args


def fmt_time(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return "%02d:%02d:%06.3f" % (h, m, s)


def cut_command(src, out, start, end, keep_all_streams=False, faststart=None):
    """构造无损剪切命令（流复制，切点对齐关键帧）。"""
    duration = max(0.0, float(end) - float(start))
    args = [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-y"]
    args += ["-ss", "%.3f" % float(start)]
    args += ["-i", src]
    args += ["-t", "%.3f" % duration]
    if keep_all_streams:
        args += ["-map", "0"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a?"]
    args += ["-c", "copy"]
    args += ["-map_metadata", "0", "-map_chapters", "-1"]
    args += ["-avoid_negative_ts", "make_zero"]
    if faststart is None:
        faststart = out.lower().endswith((".mp4", ".m4v", ".mov"))
    if faststart:
        args += ["-movflags", "+faststart"]
    args += _progress_args()
    args += [out]
    return args


def concat_command(list_file, out, fmt=None, faststart=None):
    args = [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-y"]
    args += ["-f", "concat", "-safe", "0", "-i", list_file]
    args += ["-c", "copy"]
    args += ["-map_metadata", "0"]
    args += ["-avoid_negative_ts", "make_zero"]
    if fmt:
        args += ["-f", fmt]
    if faststart is None:
        faststart = out.lower().endswith((".mp4", ".m4v", ".mov"))
    if faststart:
        args += ["-movflags", "+faststart"]
    args += _progress_args()
    args += [out]
    return args


def _progress_args():
    return ["-progress", "pipe:1", "-nostats"]


def parse_progress_line(line):
    """解析 ffmpeg -progress 输出行，返回 (key, value) 或 None。"""
    if "=" not in line:
        return None
    k, v = line.split("=", 1)
    return k.strip(), v.strip()


def out_time_seconds(key, value):
    """把 -progress 的输出时间字段换算为秒；非时间字段返回 None。"""
    if key in ("out_time_us", "out_time_ms"):
        try:
            return int(value) / 1_000_000.0
        except (TypeError, ValueError):
            return None
    if key == "out_time":
        parts = value.split(":")
        if len(parts) == 3:
            try:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except (TypeError, ValueError):
                return None
    return None


def container_of(path):
    return _ext(path)
