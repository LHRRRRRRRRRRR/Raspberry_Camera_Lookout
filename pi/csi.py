"""主摄：CSI Camera Module 3 NoIR，双流采集 + 日/夜配置切换。

两路输出的分工：
  main  1920x1080 RGB —— 只在录制时取，喂给 ffmpeg
  lores  960x540 RGB —— 常开，用于运动检测 + 预录环缓冲

关键点：用 capture_request() 一次拿到两路流，保证它们来自同一帧。

关于 IR-cut 滤镜
----------------
NoIR 拆掉了红外截止片，白天会偏洋红。装上可拆的 IR-cut 滤镜后：
  · 装上（白天）→ 颜色正常，但看不见红外 → 用 [day] 配置
  · 拆下（夜里）→ 红外夜视，彩色全是假的 → 用 [night] 配置，强制黑白
本文件负责按光照自动在这两套配置间切换，也支持 HTTP 手动指定。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from util import cget

log = logging.getLogger("vision.csi")

_FLIP = {
    "none": (0, 0),
    "hflip": (1, 0),
    "vflip": (0, 1),
    "rot180": (1, 1),
}

# 滤镜拆下后，NoIR 的彩色是假的 → 强制灰度
_DEFAULT_DAY = {"awb_gains": [], "ev": 0.0, "mono": False}
_DEFAULT_NIGHT = {"awb_gains": [], "ev": -0.3, "mono": True}


class CsiSource(threading.Thread):
    def __init__(self, cfg: dict, pipeline):
        super().__init__(name="csi", daemon=True)
        self.cfg = cfg
        self.pipeline = pipeline
        self.size = (int(cget(cfg, "camera.width", 1920)), int(cget(cfg, "camera.height", 1080)))
        self.lores_size = tuple(int(v) for v in cget(cfg, "preroll.size", [960, 540]))
        self.fps = float(cget(cfg, "camera.fps", 15))
        self.flip = str(cget(cfg, "camera.flip", "none")).lower()

        # 日/夜两套配置
        self.profiles = {
            "day": self._profile(cfg, "day", _DEFAULT_DAY),
            "night": self._profile(cfg, "night", _DEFAULT_NIGHT),
        }
        self.mode = str(cget(cfg, "camera.profile", "auto")).lower()
        lo, hi = cget(cfg, "camera.switch_lux", [18.0, 40.0])
        self.switch_lo = float(lo)     # 低于它 → night
        self.switch_hi = float(hi)     # 高于它 → day（留出滞回，避免来回翻）
        # 最短驻留时间：切一次之后这么久内不再切。
        # 必须要有——Lux 读数会受当前配置影响（切 night 改了曝光/增益，
        # Lux 跟着变），只靠滞回会在临界点疯狂打摆子。
        self.switch_dwell = float(cget(cfg, "camera.switch_dwell", 30.0))
        self._last_switch = 0.0
        self.active = "day"
        self.lux = 0.0
        self.focus = 0.0

        self._stop = threading.Event()
        self._last_check = 0.0
        self.picam2 = None
        # 帧率（网页滑块可实时改）
        self._want_fps = self.fps
        self._fps_dirty = False
        # 实时调色（网页滑块），优先于 [day]/[night] 配置
        self._live = {"r": None, "b": None, "ev": None}
        self._live_on = False
        self._live_dirty = False

    # ------------------------------------------------------------ 实时调色
    def set_framerate(self, fps: float) -> dict:
        self._want_fps = max(1.0, min(120.0, float(fps)))
        self._fps_dirty = True
        return {"target": self._want_fps}
    def set_live(self, r=None, b=None, ev=None) -> dict:
        if r is not None:
            self._live["r"] = float(r)
        if b is not None:
            self._live["b"] = float(b)
        if ev is not None:
            self._live["ev"] = float(ev)
        self._live_on = True
        self._live_dirty = True
        return self.live_state()

    def reset_live(self) -> dict:
        self._live_on = False
        self._live_dirty = True
        return self.live_state()

    def live_state(self) -> dict:
        return {"on": self._live_on, **{k: v for k, v in self._live.items()}}

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _profile(cfg: dict, name: str, default: dict) -> dict:
        gains = cget(cfg, f"{name}.awb_gains", default["awb_gains"]) or []
        return {
            "awb_gains": [float(g) for g in gains][:2],
            "ev": float(cget(cfg, f"{name}.ev", default["ev"])),
            "mono": bool(cget(cfg, f"{name}.mono", default["mono"])),
        }

    def _controls_for(self, name: str) -> dict:
        p = self.profiles[name]
        c = {
            "Saturation": 0.0 if p["mono"] else 1.0,
            "ExposureValue": p["ev"],
        }
        if len(p["awb_gains"]) == 2:
            c["AwbEnable"] = False
            c["ColourGains"] = (p["awb_gains"][0], p["awb_gains"][1])
        else:
            c["AwbEnable"] = True
        return c

    def _apply(self, pc, name: str) -> None:
        if name == self.active and getattr(self, "_applied_once", False):
            return
        c = self._controls_for(name)
        try:
            pc.set_controls(c)
            self.active = name
            self._applied_once = True
            self.pipeline.profile = name
            p = self.profiles[name]
            log.info("切换配置 → %s  (黑白=%s, EV=%+.1f, 增益=%s)",
                     name, p["mono"], p["ev"], p["awb_gains"] or "自动")
        except Exception:  # noqa: BLE001
            log.exception("应用 %s 配置失败", name)

    # ------------------------------------------------------------------ 运行
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            log.exception("CSI 采集线程异常退出")
            self.pipeline.source_error["csi"] = f"{type(exc).__name__}: {exc}"
            self.pipeline.source_ok["csi"] = False

    def _run(self) -> None:
        from libcamera import Transform
        from picamera2 import Picamera2

        pc = Picamera2()
        self.picam2 = pc
        hflip, vflip = _FLIP.get(self.flip, (0, 0))
        transform = Transform(hflip=hflip, vflip=vflip)

        # 必须显式写 format="YUV420"：
        # 不写时 main 默认是 4 字节/像素的 RGBX（8.29MB/帧），
        # 而 ffmpeg 按 yuv420p 只吃 3.11MB/帧，会整帧错位、编码出垃圾。
        # 另外 RGB888 输出路径的偏色问题也一并绕开了。
        vcfg = pc.create_video_configuration(
            main={"size": self.size, "format": "YUV420"},
            lores={"size": self.lores_size, "format": "YUV420"},
            controls={"FrameRate": self.fps},
            transform=transform,
        )
        pc.configure(vcfg)
        pc.start()
        log.info(
            "CSI 启动: main=%sx%s lores=%sx%s fps=%.0f flip=%s profile=%s",
            self.size[0], self.size[1], self.lores_size[0], self.lores_size[1],
            self.fps, self.flip, self.mode,
        )

        self._apply(pc, self.active)

        self.pipeline.source_ok["csi"] = True

        # 显式打开连续自动对焦：默认虽然是 Continuous，但明确设置一次更可靠。
        # 之前预览发糊，很大原因是没人管对焦。
        try:
            pc.set_controls({"AfMode": 2, "AfSpeed": 1})   # 2=Continuous, 1=Fast
            log.info("自动对焦已开启（连续模式）")
        except Exception:  # noqa: BLE001
            try:
                pc.set_controls({"AfMode": 2})
                log.info("自动对焦已开启（连续模式，无 AfSpeed）")
            except Exception:  # noqa: BLE001
                log.warning("设置自动对焦失败", exc_info=True)

        n = 0
        while not self._stop.is_set():
            req = pc.capture_request()
            try:
                # make_array 返回的是映射缓冲的视图，release 后内容可能被覆盖，
                # 所以必须真正拷一份出去。
                lores = np.array(req.make_array("lores"), copy=True)
                # I420 平面数据，(h*3//2, w)；不做 RGB 转换
                md = req.get_metadata()
                self.focus = float(md.get("FocusFoM", 0.0) or 0.0)
                main_bytes = None
                # 只在需要且到了录制间隔时，才复制主图（否则 120fps 下会拖垮循环）
                if self.pipeline.wants_main_now(time.monotonic()):
                    main = np.asarray(req.make_array("main"))
                    main_bytes = np.ascontiguousarray(main).tobytes()
            finally:
                req.release()

            # 每 0.5 秒看一次照明与外部指令
            now = time.monotonic()
            if now - self._last_check > 0.5:
                self._last_check = now
                self.lux = float(md.get("Lux", 0.0) or 0.0)
                self._decide(pc)

            self.pipeline.on_csi(lores, now, main_bytes)
            n += 1
            if n % 300 == 0:
                log.debug("CSI 已处理 %d 帧", n)

        pc.stop()
        pc.close()
        log.info("CSI 已停止")

    # ------------------------------------------------------------------ 决策
    def _decide(self, pc) -> None:
        self.pipeline.lux = self.lux
        self.pipeline.focus = self.focus

        # 帧率改动（网页滑块）
        if self._fps_dirty:
            self._fps_dirty = False
            try:
                pc.set_controls({"FrameRate": float(self._want_fps)})
                self.pipeline.set_pre_fps(self._want_fps)
                self.pipeline.capture_fps = self._want_fps
                log.info("采集帧率改为 %.0f fps", self._want_fps)
            except Exception:  # noqa: BLE001
                log.exception("设置帧率失败")

        # 0) 网页滑块的实时调色，优先级最高
        if self._live_dirty:
            self._live_dirty = False
            if self._live_on:
                c = {}
                r, b = self._live["r"], self._live["b"]
                if r is not None and b is not None:
                    c["AwbEnable"] = False
                    c["ColourGains"] = (r, b)
                    c["Saturation"] = 1.0
                if self._live["ev"] is not None:
                    c["ExposureValue"] = self._live["ev"]
                if c:
                    try:
                        pc.set_controls(c)
                        log.info("实时调色: 红=%.2f 蓝=%.2f EV=%+.1f", r or 0, b or 0,
                                 self._live["ev"] or 0)
                    except Exception:  # noqa: BLE001
                        log.exception("实时调色失败")
                return
            # 关掉实时调色 → 回到配置
            self._applied_once = False
            self._apply(pc, self.active)
            return

        if not self._live_on:
            # 1) 手动指令
            req = self.pipeline.take_profile_request()
            if req:
                if req in ("day", "night"):
                    self.mode = req
                elif req == "auto":
                    self.mode = "auto"
                log.info("收到手动配置切换: %s", req)

            if self.mode in ("day", "night"):
                self._apply(pc, self.mode)
                return

            # 2) 自动：带滞回 + 最短驻留，避免在临界点反复横跳
            if self.lux <= 0:
                return
            if time.monotonic() - self._last_switch < self.switch_dwell:
                return
            if self.active == "day" and self.lux < self.switch_lo:
                self._last_switch = time.monotonic()
                self._apply(pc, "night")
            elif self.active == "night" and self.lux > self.switch_hi:
                self._last_switch = time.monotonic()
                self._apply(pc, "day")
