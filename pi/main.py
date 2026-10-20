"""入口：把相机、管线、录制、网页服务串起来。

    python3 main.py                 # 用同目录的 config.toml
    python3 main.py -v              # 打印调试日志
    python3 main.py --port 9000     # 临时改端口
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import sys
import threading
import time
import tomllib
from pathlib import Path

from csi import CsiSource
from pipeline import Pipeline
from recorder import EventManager
from server import App, serve
from util import cget, pulse_url

log = logging.getLogger("vision")


def load_cfg(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="事件触发守夜相机")
    ap.add_argument("--config", default=str(here / "config.toml"))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-camera", action="store_true", help="不启动相机，只起网页（调试用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_cfg(Path(args.config))

    # 运行时覆盖（网页上切换画质档位时写入，避免把 config.toml 改坏）
    ov_path = here / "runtime.json"
    if ov_path.exists():
        try:
            ov = json.loads(ov_path.read_text(encoding="utf-8"))
            for sec, kv in ov.items():
                if isinstance(kv, dict):
                    cfg.setdefault(sec, {}).update(kv)
            log.info("已应用运行时覆盖 %s: %s", ov_path.name,
                     {k: v for k, v in ov.items()})
        except Exception:  # noqa: BLE001
            log.exception("runtime.json 解析失败，忽略")

    host = args.host or str(cget(cfg, "server.host", "0.0.0.0"))
    port = args.port or int(cget(cfg, "server.port", 8080))

    events = EventManager(cfg)
    pipeline = Pipeline(cfg, events)

    # ── 第二机位：XIAO ESP32S3，走 USB 串口 ──────────────────────────────
    sec_link = None
    if bool(cget(cfg, "secondary.enabled", False)) and \
            str(cget(cfg, "secondary.mode", "serial")).lower() == "serial":
        from esp32 import Esp32Link
        sec_link = Esp32Link(cfg, pipeline)
        sec_link.start()

    lamp_url = str(cget(cfg, "secondary.lamp_url", "") or "")

    def _on_event_start() -> None:
        """事件开始：点补光灯（ESP32 的 GPIO 和/或外接的灯控 URL）。"""
        if sec_link is not None and sec_link.lamp_ms > 0:
            sec_link.lamp()
            log.info("已让 ESP32 开补光灯 %d ms", sec_link.lamp_ms)
        if lamp_url:
            pulse_url(lamp_url)

    events.on_start = _on_event_start

    # 事件录完 → 管线进入冷却，避免"录完立刻又录"
    events.on_finish = lambda info: pipeline.note_event_finished()

    source = None
    if not args.no_camera:
        source = CsiSource(cfg, pipeline)
        source.start()
    else:
        log.warning("已跳过相机启动（--no-camera）")

    # 健康检查线程：相机断流时把状态翻成异常
    stop = threading.Event()

    def health_loop():
        while not stop.wait(2.0):
            try:
                pipeline.health()
            except Exception:  # noqa: BLE001
                log.exception("健康检查异常")

    threading.Thread(target=health_loop, name="health", daemon=True).start()

    app = App(cfg, pipeline, events, source)
    srv = serve(app, host, port)

    hostname = socket.gethostname()
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print()
    print("  ┌──────────────────────────────────────────────┐")
    print(f"  │  看板   http://{shown}:{port}".ljust(48) + "│")
    print(f"  │  局域网 http://{hostname}.local:{port}".ljust(48) + "│")
    print("  │  Ctrl-C 退出                                  │")
    print("  └──────────────────────────────────────────────┘")
    print()

    def _bye(signum, frame):  # noqa: ARG001
        # 坑：信号处理函数跑在主线程，而 serve_forever() 也占着主线程。
        # 在这里直接调 srv.shutdown() 会死锁——它会等 serve_forever 退出，
        # 而 serve_forever 正等着信号处理函数返回。所以另开线程去关。
        log.info("收到信号 %s，准备退出", signum)
        stop.set()
        if source:
            source.stop()
        if sec_link is not None:
            sec_link.stop()
        threading.Thread(target=srv.shutdown, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    try:
        srv.serve_forever(poll_interval=0.3)
    finally:
        stop.set()
        if source:
            source.stop()
            source.join(timeout=3)
        if sec_link is not None:
            sec_link.stop()
            sec_link.join(timeout=2)
        log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
