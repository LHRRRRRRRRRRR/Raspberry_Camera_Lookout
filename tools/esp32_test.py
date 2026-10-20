#!/usr/bin/env python3
"""esp32_test.py —— 验证 Pi 与 XIAO ESP32S3 的串口链路，并抓一张图。

用法（在 Pi 上）：
    sudo apt install -y python3-serial        # 只需要一次
    python3 esp32_test.py                     # 默认 /dev/ttyACM0
    python3 esp32_test.py --port /dev/ttyACM0 --snap test.jpg
    python3 esp32_test.py --lamp 3000         # 顺便点灯 3 秒

协议（见 xiao_cam.ino）：
    文本命令换行结尾；图像帧 = 0xA5 0x5A | type | len(4B LE) | JPEG
"""
from __future__ import annotations

import argparse
import struct
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    print("缺少 pyserial：sudo apt install -y python3-serial")
    sys.exit(1)

MAGIC = b"\xa5\x5a"
TYPE_JPEG = 0x01


def read_line(ser: serial.Serial, timeout: float = 2.0) -> str:
    """读一行文本，超时返回空串。"""
    ser.timeout = 0.2
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(1)
        if not chunk:
            continue
        if chunk == b"\n":
            return buf.decode("utf-8", "replace").strip()
        if chunk != b"\r":
            buf += chunk
        if len(buf) > 512:
            break
    return buf.decode("utf-8", "replace").strip()


def send_cmd(ser: serial.Serial, cmd: str) -> None:
    ser.write((cmd + "\n").encode())
    ser.flush()


def wait_reply(ser: serial.Serial, prefix: str, timeout: float = 3.0) -> str | None:
    """一直读到以 prefix 开头的行（跳过大头针之外的心跳/日志）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = read_line(ser, timeout=deadline - time.monotonic())
        if not line:
            continue
        if line.startswith(prefix):
            return line
    return None


def read_frame(ser: serial.Serial, timeout: float = 5.0) -> bytes | None:
    """等一个二进制落帧：MAGIC + type + len + payload。"""
    ser.timeout = 0.2
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        if b != MAGIC[:1]:
            continue
        b2 = ser.read(1)
        if b2 != MAGIC[1:2]:
            continue
        head = ser.read(5)
        if len(head) != 5:
            continue
        ftype = head[0]
        (length,) = struct.unpack("<I", head[1:5])
        if ftype != TYPE_JPEG or length == 0 or length > 2_000_000:
            print(f"  跳过异常帧: type={ftype} len={length}")
            continue
        payload = b""
        while len(payload) < length:
            more = ser.read(length - len(payload))
            if not more:
                break
            payload += more
        if len(payload) == length:
            return payload
        print(f"  帧不完整: {len(payload)}/{length}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--snap", default="", help="把抓到的图存到这个路径")
    ap.add_argument("--lamp", type=int, default=0, help="点亮补光灯的毫秒数")
    args = ap.parse_args()

    print(f"打开 {args.port} @ {args.baud}")
    ser = serial.Serial(args.port, args.baud, timeout=1)
    time.sleep(1.5)
    ser.reset_input_buffer()

    # 1) 链路测试
    send_cmd(ser, "PING")
    r = wait_reply(ser, "PONG", 3)
    if not r:
        print("✗ 没收到 PONG —— 检查 USB CDC On Boot 是否开启、板子是否在跑")
        return 1
    print("✓ PING → PONG")

    # 2) 状态
    send_cmd(ser, "STAT")
    print("✓", wait_reply(ser, "OK STAT", 3) or "(无响应)")

    # 3) 补光灯
    if args.lamp > 0:
        send_cmd(ser, f"LAMP {args.lamp}")
        print("✓", wait_reply(ser, "OK LAMP", 3) or "(无响应)")

    # 4) 抓一帧
    if args.snap:
        send_cmd(ser, "SNAP")
        t0 = time.monotonic()
        jpg = read_frame(ser, 8)
        if jpg:
            with open(args.snap, "wb") as f:
                f.write(jpg)
            print(f"✓ 收到 {len(jpg)} 字节，已存 {args.snap} "
                  f"（耗时 {time.monotonic() - t0:.2f}s）")
        else:
            print("✗ 没收到图像帧")

    # 5) 连续流测速（3 秒）
    send_cmd(ser, "FPS 10")
    wait_reply(ser, "OK FPS", 2)
    send_cmd(ser, "STREAM 1")
    wait_reply(ser, "OK STREAM", 2)

    n, total, t0 = 0, 0, time.monotonic()
    while time.monotonic() - t0 < 3.0:
        if read_frame(ser, 2):
            n += 1
            total += 1
    dt = time.monotonic() - t0
    send_cmd(ser, "STREAM 0")
    print(f"✓ 3 秒收到 {n} 帧 → {n / dt:.1f} fps")

    ser.close()
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
