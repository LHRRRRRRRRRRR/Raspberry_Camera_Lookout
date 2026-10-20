"""ESP32 第二机位：走 USB 串口（CDC），不用 WiFi。

职责：
  · 维护串口连接（断了自动重连）
  · 持续收 JPEG 帧，交给 Pipeline 的副摄环缓冲
  · 事件触发时点补光灯

和主摄的关系：互不依赖。ESP32 掉线只影响 secondary 那一份数据，
主流录制不受影响。
"""

from __future__ import annotations

import logging
import struct
import threading
import time

from util import cget

log = logging.getLogger("vision.esp")

MAGIC0 = 0xA5
MAGIC1 = 0x5A
TYPE_JPEG = 0x01
TYPE_SOUND = 0x02


class Esp32Link(threading.Thread):
    def __init__(self, cfg: dict, pipeline):
        super().__init__(name="esp32", daemon=True)
        self.cfg = cfg
        self.pipeline = pipeline
        self.port = str(cget(cfg, "secondary.port", "/dev/ttyACM0"))
        self.baud = int(cget(cfg, "secondary.baud", 115200))
        self.fps = int(cget(cfg, "secondary.fps", 10))
        self.lamp_ms = int(cget(cfg, "secondary.lamp_ms", 6000))
        # 声音阈值下发给 ESP32，0 = 关闭声音触发
        self.mic_threshold = int(cget(cfg, "sound.threshold", 0)) \
            if bool(cget(cfg, "sound.enabled", False)) else 0

        self._stop = threading.Event()
        self._ser = None
        self._wlock = threading.Lock()
        self._buf = bytearray()          # 串口接收缓冲（帧解析用）
        self.connected = False
        self.frames = 0
        self.sounds = 0
        self.errors = 0

    # ------------------------------------------------------------------ 对外
    def stop(self) -> None:
        self._stop.set()
        self._close()

    def lamp(self, ms: int | None = None) -> None:
        """点亮补光灯。没有灯时这个命令也没害处（只是 GPIO 变化）。"""
        self._cmd(f"LAMP {self.lamp_ms if ms is None else int(ms)}")

    # ------------------------------------------------------------------ 内部
    def _cmd(self, line: str) -> bool:
        with self._wlock:
            if self._ser is None:
                return False
            try:
                self._ser.write((line + "\n").encode())
                self._ser.flush()
                return True
            except Exception:  # noqa: BLE001
                log.debug("串口写入失败", exc_info=True)
                return False

    def _close(self) -> None:
        with self._wlock:
            ser, self._ser = self._ser, None
        self.connected = False
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass

    def _open(self) -> bool:
        try:
            import serial
        except ImportError:
            log.error("缺少 pyserial：sudo apt install -y python3-serial")
            self._stop.set()
            return False
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.5)
        except Exception as exc:  # noqa: BLE001
            log.debug("打开 %s 失败: %s", self.port, exc)
            return False

        time.sleep(0.4)
        ser.reset_input_buffer()
        with self._wlock:
            self._ser = ser

        # 关键：先让它停止推流。
        # 否则（比如服务重启后）ESP32 还在往串口灌 JPEG，
        # 缓冲里全是二进制数据，握手时永远读不到 "PONG"。
        try:
            ser.write(b"STREAM 0\n")
            ser.flush()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)

        # 把残留数据排空（最多等 1 秒）
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n:
                ser.read(n)
            else:
                break
        ser.reset_input_buffer()

        # 握手（允许两次，第一次可能撞上刚排空的尾巴）
        got_pong = False
        for _ in range(2):
            if not self._cmd("PING"):
                break
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                line = self._readline()
                if not line:
                    break
                if line == "PONG":
                    got_pong = True
                    break
                if line.startswith("PONG"):
                    got_pong = True
                    break
            if got_pong:
                break

        if not got_pong:
            log.warning("ESP32 无响应（PONG 超时）")
            self._close()
            return False

        self._cmd(f"FPS {self.fps}")
        self._cmd(f"SMIC {self.mic_threshold}")
        self._cmd("STREAM 1")
        self.connected = True
        self.pipeline.source_ok["secondary"] = True
        log.info("ESP32 已连接 %s @%d，%d fps", self.port, self.baud, self.fps)
        return True

    def _readline(self) -> str:
        ser = self._ser
        if ser is None:
            return ""
        buf = b""
        try:
            while len(buf) < 256:
                ch = ser.read(1)
                if not ch:
                    break
                if ch == b"\n":
                    return buf.decode("utf-8", "replace").strip()
                if ch != b"\r":
                    buf += ch
        except Exception:  # noqa: BLE001
            pass
        return buf.decode("utf-8", "replace").strip()

    def _read_packet(self, timeout: float = 3.0):
        """从串口缓冲里解析出一个完整包，返回 (类型, 负载)；超时返回 None。

        按块读取再在内存里找帧头，避免"逐字节 read(1)"——
        串口跑到 160KB/s 时逐字节循环会慢到把超时吃光，触发假重连。
        """
        ser = self._ser
        if ser is None:
            return None
        deadline = time.monotonic() + timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            chunk = ser.read(4096)
            if chunk:
                self._buf += chunk

            while True:
                i = self._buf.find(b"\xa5\x5a")
                if i < 0:
                    # 保留最后 1 字节，可能是半个帧头
                    if len(self._buf) > 1:
                        del self._buf[:-1]
                    break
                if i > 0:
                    del self._buf[:i]          # 丢掉帧头前的文本/垃圾
                if len(self._buf) < 7:
                    break
                ftype = self._buf[2]
                (length,) = struct.unpack_from("<I", self._buf, 3)
                if ftype not in (TYPE_JPEG, TYPE_SOUND) or length > 2_000_000:
                    del self._buf[:2]          # 假帧头，往后挪两字节再找
                    continue
                if len(self._buf) < 7 + length:
                    break                      # 还没收满，等下一块
                payload = bytes(self._buf[7:7 + length])
                del self._buf[:7 + length]
                return ftype, payload
        return None

    # ------------------------------------------------------------------ 线程
    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if self._ser is None or not self.connected:
                if not self._open():
                    self.pipeline.source_ok["secondary"] = False
                    self.errors += 1
                    self._stop.wait(min(backoff, 10.0))
                    backoff = min(backoff * 1.5, 10.0)
                    continue
                backoff = 1.0

            jpg = self._read_packet(timeout=5.0)
            if jpg is None:
                log.warning("ESP32 超时，准备重连")
                self.pipeline.source_ok["secondary"] = False
                self._close()
                continue

            ftype, payload = jpg
            if ftype == TYPE_JPEG:
                self.frames += 1
                self.pipeline.on_secondary(payload, time.monotonic())
            elif ftype == TYPE_SOUND:
                if len(payload) >= 4:
                    level = struct.unpack("<f", payload[:4])[0]
                    self.sounds += 1
                    self.pipeline.on_sound(level, time.monotonic())

        self._close()
        self._buf.clear()
        log.info("ESP32 已断开")
