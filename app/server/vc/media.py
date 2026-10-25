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

# 浏览器 <video> 原生可播放的容器；其余（TS/MTS/AVI/FLV/RMVB…）预览前需无损转封装成 MP4
PLAYABLE_EXTS = {"mp4", "m4v", "mov", "webm", "mkv", "3gp"}

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
    if result["ext"] in PLAYABLE_EXTS:
        result["needsPreview"] = False
        result["previewReady"] = True
    else:
        result["needsPreview"] = True
        try:
            result["previewReady"] = os.path.exists(preview_path(path))
        except OSError:
            result["previewReady"] = False
    return result


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
# 预览转封装：浏览器放不了的容器（TS/MTS/AVI/FLV/RMVB…）→ 可播放的 MP4


def preview_folder():
    folder = os.path.join(config().thumb_dir, "preview")
    os.makedirs(folder, exist_ok=True)
    return folder


def preview_path(path):
    """预览副本的缓存路径（源文件 size/mtime 变化会换新缓存）。"""
    key = "%s|%s" % (os.path.realpath(path), _stat_key(path))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(preview_folder(), "%s.mp4" % digest)


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
