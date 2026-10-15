#!/usr/bin/env python3
"""videocut 服务端入口。

由 cmd/main 以包用户身份启动，监听统一网关声明的 Unix Socket。
"""

import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vc import httpd, jobs, media  # noqa: E402
from vc.core import LOG, config, setup_logging  # noqa: E402


def report_fatal(message):
    LOG.error(message)
    logfile = os.environ.get("TRIM_TEMP_LOGFILE")
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except OSError:
            pass
    sys.stderr.write(message + "\n")


def write_pid(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%d\n" % os.getpid())
    except OSError as exc:
        LOG.warning("write pid failed: %s", exc)


def main():
    setup_logging()
    cfg = config()

    LOG.info(
        "videocut %s starting: user=%s uid=%s socket=%s www=%s data=%s",
        cfg.appver, cfg.username, cfg.uid, cfg.socket_path, cfg.www_dir, cfg.data_dir,
    )
    if not cfg.ffmpeg or not cfg.ffprobe:
        report_fatal("未找到 ffmpeg/ffprobe，无法处理视频。请确认飞牛系统的媒体组件可用。")
        return 1
    LOG.info("ffmpeg=%s (%s)", cfg.ffmpeg, media.ffmpeg_version())
    if not os.path.isdir(cfg.www_dir):
        LOG.warning("前端目录不存在：%s", cfg.www_dir)

    jobs.manager()  # 触发历史任务加载

    try:
        server = httpd.UnixHTTPServer(cfg.socket_path)
    except RuntimeError as exc:
        report_fatal(str(exc))
        return 1
    except OSError as exc:
        report_fatal("无法创建 Socket %s：%s" % (cfg.socket_path, exc))
        return 1

    write_pid(cfg.pid_file)

    stop = threading.Event()

    def on_signal(signum, _frame):
        LOG.info("received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="http", daemon=True
    )
    worker.start()
    LOG.info("ready: %s", cfg.socket_path)

    tcp = None
    tcp_worker = None
    if cfg.port:
        try:
            tcp = httpd.TcpHTTPServer(cfg.bind, cfg.port)
        except OSError as exc:
            LOG.warning("端口 %s 无法监听（%s），仅保留统一网关访问", cfg.port, exc)
            tcp = None
        else:
            tcp_worker = threading.Thread(
                target=tcp.serve_forever, kwargs={"poll_interval": 0.5}, name="tcp", daemon=True
            )
            tcp_worker.start()
            LOG.info(
                "端口访问已开启：http://%s:%s/（无登录态，请勿暴露到公网）", cfg.bind, cfg.port
            )
    else:
        LOG.info("端口访问已关闭（VC_PORT off），仅统一网关可用")

    while not stop.is_set():
        time.sleep(0.5)

    server.shutdown()
    worker.join(timeout=5)
    server.server_close()
    if tcp is not None:
        tcp.shutdown()
        if tcp_worker is not None:
            tcp_worker.join(timeout=5)
        tcp.server_close()
    try:
        os.unlink(cfg.pid_file)
    except OSError:
        pass
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
