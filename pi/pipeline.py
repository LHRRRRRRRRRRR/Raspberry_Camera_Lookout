"""帧管线：环形缓冲（预录）+ 运动检测 + 触发判定。

设计要点
--------
* 预录（preroll）是本项目最重要的部分：只有滚存"事发前几秒"，
  才能拍到人/动物真正从画面外走进来的那一刻。
* 预录帧以 JPEG 存在内存里，960x540 @8fps x 4s ≈ 1MB，
  2GB 内存的 Pi 5 完全吃得消；绝不要存原始 RGB 帧。
* 运动检测跑在 1/8 降采样的灰度图上（120x68），CPU 占用可以忽略。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

import numpy as np

from util import cget, yuv420_to_jpeg

Frame = Tuple[float, bytes]  # (单调时钟时间戳, JPEG 字节)


class Ring:
    """定长环形缓冲，线程安全。"""

    def __init__(self, seconds: float, fps: float):
        self.fps = max(1.0, float(fps))
        self.maxlen = max(2, int(round(seconds * self.fps)))
        self._buf: Deque[Frame] = deque(maxlen=self.maxlen)
        self._lock = threading.Lock()
        self.total = 0

    def push(self, ts: float, jpeg: bytes) -> None:
        with self._lock:
            self._buf.append((ts, jpeg))
            self.total += 1

    def set_maxlen(self, n: int) -> None:
        """改变缓冲长度（帧率变了要跟着调整），保留最新的内容。"""
        n = max(2, int(n))
        with self._lock:
            if n == self.maxlen:
                return
            self.maxlen = n
            keep = list(self._buf)[-n:]
            self._buf = deque(keep, maxlen=n)

    def snapshot(self) -> List[Frame]:
        with self._lock:
            return list(self._buf)

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


class MotionDetector:
    """灰度帧差 + 慢速背景更新。返回 (平均强度差, 变化像素占比)。"""

    def __init__(self, cfg: dict):
        self.threshold = float(cget(cfg, "motion.threshold", 4.0))
        self.pixel_delta = float(cget(cfg, "motion.pixel_delta", 14))
        self.area_ratio = float(cget(cfg, "motion.area_ratio", 0.004))
        self.persist = int(cget(cfg, "motion.persist", 2))
        self.cooldown = float(cget(cfg, "motion.cooldown", 5.0))
        self.warmup = float(cget(cfg, "motion.warmup", 4.0))
        self.alpha = float(cget(cfg, "motion.bg_alpha", 0.03))
        self.scale = int(cget(cfg, "motion.downsample", 8))

        self.bg: Optional[np.ndarray] = None
        self.t0: Optional[float] = None
        self.streak = 0
        self.score = 0.0
        self.area = 0.0

    # -- 内部 ---------------------------------------------------------------
    def _gray(self, y_plane: np.ndarray) -> np.ndarray:
        s = self.scale
        return y_plane[::s, ::s].astype(np.float32)

    # -- 对外 ---------------------------------------------------------------
    def update(self, y_plane: np.ndarray, ts: float) -> Tuple[float, float]:
        """y_plane 是 YUV420 的 Y 平面——它本来就是灰度，直接降采样即可。"""
        g = self._gray(y_plane)
        if self.bg is None or self.bg.shape != g.shape:
            self.bg = g
            self.t0 = ts
            self.score = self.area = 0.0
            return self.score, self.area

        diff = np.abs(g - self.bg)
        self.score = float(diff.mean())
        self.area = float((diff > self.pixel_delta).mean())
        # 慢速自适应：光照缓慢变化（云飘过）不会一直报警，
        # 但站稳不动的真人会慢慢"融进"背景——这是帧差法的固有局限，
        # 对"短暂经过"场景反而合适。
        self.bg += (g - self.bg) * self.alpha
        return self.score, self.area

    @property
    def warm(self) -> bool:
        return self.t0 is not None and (
            self.score > 0 or time.monotonic() - self.t0 >= 0
        )

    def ready(self, ts: float) -> bool:
        return self.t0 is not None and (ts - self.t0) >= self.warmup

    def hit(self) -> bool:
        return self.score >= self.threshold and self.area >= self.area_ratio


class Pipeline:
    """把各相机源的帧汇成一条事件流水线。"""

    def __init__(self, cfg: dict, events):
        self.cfg = cfg
        self.events = events

        self.pre_w = int(cget(cfg, "preroll.size", [960, 540])[0])
        self.pre_h = int(cget(cfg, "preroll.size", [960, 540])[1])
        self.pre_fps = float(cget(cfg, "preroll.fps", 8))
        self.pre_quality = int(cget(cfg, "camera.quality", 78))
        self.pre_seconds = float(cget(cfg, "preroll.seconds", 4.0))
        # 录制帧率：主摄可能跑 120fps，但录像按这个帧率抽帧，
        # 否则 120 帧喂给 30fps 的封装器会变成 4 倍慢动作。
        self.record_fps = float(cget(cfg, "record.fps", 15))
        self._last_main = 0.0

        self.ring = Ring(cget(cfg, "preroll.seconds", 4.0), self.pre_fps)
        self.sec_ring = Ring(
            cget(cfg, "secondary.ring_seconds", 4.0),
            cget(cfg, "secondary.fps", 5),
        )

        self.motion = MotionDetector(cfg)
        self.armed = bool(cget(cfg, "motion.armed", True))
        self.trigger_enabled = bool(cget(cfg, "motion.enabled", True))

        self.score = 0.0
        self.area = 0.0
        self.frames = 0
        self.recording = False
        self.source_ok = {"csi": False, "secondary": False}
        self.source_error: dict[str, str] = {}

        self._lock = threading.Lock()
        self._last_pre = 0.0
        self._last_trigger = 0.0
        # 录完之后再静默一段，否则会"录完立刻又录"，实质变成连续录像
        self.cooldown_until = 0.0
        self._fps_window: Deque[float] = deque(maxlen=30)
        self._last_csi_ts = 0.0

        # 日/夜配置：由 csi 线程写入，网页可请求切换
        self.profile = "day"
        self.lux = 0.0
        self.focus = 0.0
        self.capture_fps = float(cget(cfg, "camera.fps", 15))
        self._profile_req: Optional[str] = None

        # 声音触发（由 ESP32 的 PDM 麦克风上报）
        self.sound_enabled = bool(cget(cfg, "sound.enabled", False))
        self.sound_cooldown = float(cget(cfg, "sound.cooldown", 15.0))
        self.sound_level = 0.0
        self.sound_events = 0
        self._sound_until = 0.0

    # -- 帧率（网页滑块可实时改） -------------------------------------------
    def set_pre_fps(self, fps: float) -> None:
        fps = max(1.0, min(120.0, float(fps)))
        if abs(fps - self.pre_fps) < 0.01:
            return
        self.pre_fps = fps
        # 缓冲按"秒数 × 帧率"算，但封顶 300 帧，免得 120fps 时吃爆内存
        self.ring.set_maxlen(min(300, int(self.pre_seconds * fps)))

    # -- 日/夜配置 ----------------------------------------------------------
    def request_profile(self, name: str) -> None:
        self._profile_req = name

    def take_profile_request(self) -> Optional[str]:
        req, self._profile_req = self._profile_req, None
        return req

    # -- 状态 ---------------------------------------------------------------
    @property
    def wants_main(self) -> bool:
        """主摄是否应该输出全分辨率帧（只有录制时才需要，省 CPU）。"""
        return self.events.busy

    def wants_main_now(self, ts: float) -> bool:
        """要不要"这一帧"的主图。

        必须按 record.fps 节流：120fps 下每帧都复制一份 1280x720 的原始数据
        就是 166MB/s 的 memcpy，会把采集循环拖到 10fps 以下。
        """
        if not self.events.busy:
            return False
        if ts - self._last_main < (1.0 / max(1.0, self.record_fps)) - 1e-4:
            return False
        self._last_main = ts
        return True

    def fps(self) -> float:
        with self._lock:
            if len(self._fps_window) < 2:
                return 0.0
            dt = self._fps_window[-1] - self._fps_window[0]
            return (len(self._fps_window) - 1) / dt if dt > 0 else 0.0

    # -- 主摄帧 -------------------------------------------------------------
    def on_csi(self, yuv: np.ndarray, ts: float, main: Optional[bytes] = None) -> None:
        """yuv 是 lores 的 I420 平面数据 (h*3//2, w)。

        不做 RGB 转换：运动检测直接用 Y 平面，预录 JPEG 直接封 YCbCr。
        这样色彩与 libcamera 编码器一致，也省掉一次全图转换。
        """
        self.source_ok["csi"] = True
        self._last_csi_ts = ts
        self.frames += 1
        with self._lock:
            self._fps_window.append(ts)

        y_plane = yuv[:self.pre_h]
        # 1) 预录采样（按 preroll.fps 抽帧，不跟满帧率）
        if ts - self._last_pre >= (1.0 / self.pre_fps) - 1e-4:
            self._last_pre = ts
            try:
                self.ring.push(ts, yuv420_to_jpeg(yuv, self.pre_w, self.pre_h,
                                                 self.pre_quality))
            except Exception:  # noqa: BLE001
                pass

        # 2) 录制时把全分辨率帧交给 EventManager（取图已在 wants_main_now 里节流）
        if main is not None:
            self.events.feed_main(main)

        # 3) 运动判定（Y 平面本身就是灰度）
        self.score, self.area = self.motion.update(y_plane, ts)
        if not self.trigger_enabled or not self.armed:
            self.motion.streak = 0
            return
        if self.events.busy or not self.motion.ready(ts):
            self.motion.streak = 0
            return
        if ts < self.cooldown_until or ts - self._last_trigger < self.motion.cooldown:
            return
        if self.motion.hit():
            self.motion.streak += 1
            if self.motion.streak >= self.motion.persist:
                self.trigger("motion", self.score)
        else:
            self.motion.streak = 0

    # -- 副摄帧（ESP32） ----------------------------------------------------
    def on_secondary(self, jpeg: bytes, ts: float) -> None:
        self.source_ok["secondary"] = True
        try:
            self.sec_ring.push(ts, jpeg)
        except Exception:  # noqa: BLE001
            pass

    # -- 声音 ---------------------------------------------------------------
    def on_sound(self, level: float, ts: float) -> None:
        """ESP32 报来一声"响动"——这就是"耳朵"。

        比运动检测更省：麦克风只需极低采样率就能抓到玻璃碎、门响这类瞬态，
        而视觉必须一直算帧差。
        """
        self.sound_level = float(level)
        self.sound_events += 1
        if not self.sound_enabled or not self.armed:
            return
        if self.events.busy or ts < self._sound_until:
            return
        self._sound_until = ts + self.sound_cooldown
        log.info("听到响动 %.0f，触发录制", level)
        self.trigger("sound", float(level))

    # -- 触发 ---------------------------------------------------------------
    def trigger(self, reason: str, score: float = 0.0) -> bool:
        if self.events.busy:
            return False
        self._last_trigger = time.monotonic()
        ok = self.events.start(
            reason=reason,
            score=score,
            csi_frames=self.ring.snapshot(),
            sec_frames=self.sec_ring.snapshot(),
        )
        if ok:
            self.motion.streak = 0
        return ok

    # -- 健康检查 -----------------------------------------------------------
    def note_event_finished(self) -> None:
        """一次事件录完，从此刻起再静默 cooldown 秒。"""
        self.cooldown_until = time.monotonic() + self.motion.cooldown
        self.motion.streak = 0

    def health(self, stale_after: float = 5.0) -> None:
        if self._last_csi_ts and time.monotonic() - self._last_csi_ts > stale_after:
            self.source_ok["csi"] = False
        self.recording = self.events.busy

    def status(self) -> dict:
        return {
            "armed": self.armed,
            "motion_enabled": self.trigger_enabled,
            "recording": self.events.busy,
            "frames": self.frames,
            "fps": round(self.fps(), 1),
            "profile": self.profile,
            "lux": round(self.lux, 1),
            "focus": round(self.focus, 1),
            "capture_fps": round(self.capture_fps, 1),
            "sound_level": round(self.sound_level, 1),
            "sound_events": self.sound_events,
            "motion_score": round(self.score, 2),
            "motion_area": round(self.area, 4),
            "preroll_frames": len(self.ring),
            "secondary_frames": len(self.sec_ring),
            "sources": dict(self.source_ok),
            "errors": dict(self.source_error),
        }
