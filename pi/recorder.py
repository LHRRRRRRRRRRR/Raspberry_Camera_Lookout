"""录制与事件管理。

一次"事件"的产物：
    events/<时间>_<原因>/
        meta.json        元数据（时间、分数、帧数、错误）
        clip.mp4         事后 N 秒，主摄全分辨率
        preroll/         事前几秒的低清 JPEG（逐帧）+ preroll.mp4
        secondary/       第二机位同期帧（ESP32 广角，可选）
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from util import cget, human, stamp

log = logging.getLogger("vision.rec")

Frame = Tuple[float, bytes]  # (时间戳, JPEG)


# --------------------------------------------------------------------------- 录制
class Recorder:
    """把主摄的原始 RGB 帧喂给 ffmpeg 编成 H.264。

    * 独立线程写管道：编码慢了只会丢帧，不会卡住采集
    * 没有 ffmpeg 时自动退化为 JPEG 序列（还能用，只是体积大）
    """

    def __init__(self, out: Path, size: Tuple[int, int], fps: float,
                 crf: int = 23, preset: str = "ultrafast", queue_size: int = 4):
        self.out = out
        self.size = size
        self.fps = float(fps)
        self.frames_dir = out.with_name(out.stem + "_frames")
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = shutil.which("ffmpeg")
        self.mode = "h264" if self.ffmpeg else "jpeg"
        self.q: queue.Queue = queue.Queue(maxsize=queue_size)
        self.dropped = 0
        self.written = 0
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        # 关掉之后不能再往队列里塞，否则 close() 会被活活"喂"死
        self.closed = False
        # ffmpeg 的 stderr 必须收集，否则出错时完全看不见原因
        self._ff_err: List[str] = []
        self._thread = threading.Thread(target=self._run, name="rec", daemon=True)
        self._thread.start()

    # -- 外部调用 -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            self.q.put_nowait(data)
        except queue.Full:
            self.dropped += 1

    def close(self, timeout: float = 30.0) -> None:
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self.q.put_nowait(None)  # type: ignore[arg-type]
                break
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
        self._thread.join(timeout)

    def size_bytes(self) -> int:
        if self.out.exists():
            return self.out.stat().st_size
        total = 0
        for p in self.frames_dir.glob("*.jpg"):
            total += p.stat().st_size
        return total

    # -- 线程体 -------------------------------------------------------------
    def _run(self) -> None:
        try:
            if self.mode == "h264":
                self._run_h264()
            else:
                self._run_jpeg()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("录制线程异常")

    def _cmd(self) -> List[str]:
        w, h = self.size
        return [
            self.ffmpeg or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", f"{w}x{h}", "-framerate", f"{self.fps:g}",
            "-i", "pipe:0",
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "23", "-pix_fmt", "yuv420p",
            str(self.out),
        ]

    def _run_h264(self) -> None:
        frame_size = int(self.size[0] * self.size[1] * 1.5)
        log.info("开始录制 %s（%sx%s，每帧应为 %d 字节 yuv420p）",
                 self.out.name, self.size[0], self.size[1], frame_size)
        self._proc = subprocess.Popen(
            self._cmd(), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        assert self._proc.stdin is not None

        # 把 ffmpeg 的 stderr 读走，否则管道写满会卡死，而且看不见报错
        def _drain():
            assert self._proc is not None and self._proc.stderr is not None
            for raw in self._proc.stderr:
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:  # noqa: BLE001
                    continue
                if line:
                    self._ff_err.append(line)
                    if len(self._ff_err) > 30:
                        self._ff_err.pop(0)

        threading.Thread(target=_drain, name="fferr", daemon=True).start()

        bad_size = 0
        while True:
            item = self.q.get()
            if item is None:
                break
            if len(item) != frame_size:
                bad_size += 1
                if bad_size <= 3:
                    log.warning("帧长度异常：%d 字节，应为 %d —— 丢弃这一帧",
                                len(item), frame_size)
                continue   # 宁可丢帧，也不要把错位数据喂给编码器
            try:
                self._proc.stdin.write(item)
                self.written += 1
            except (BrokenPipeError, ValueError):
                self.error = "ffmpeg 管道关闭"
                break
        try:
            self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        code = self._proc.wait(timeout=30)
        if code != 0 and self.error is None:
            self.error = f"ffmpeg 返回 {code}"
        if self._ff_err:
            log.warning("ffmpeg 输出（%d 行，取最后 5 行）：%s",
                        len(self._ff_err), " | ".join(self._ff_err[-5:]))
        if bad_size:
            log.warning("共 %d 帧长度不对", bad_size)

    def _run_jpeg(self) -> None:
        """没有 ffmpeg 时的退化路径：把 Y 平面存成灰度 JPEG（颜色没了但能看）。"""
        from PIL import Image
        import numpy as np

        w, h = self.size
        while True:
            item = self.q.get()
            if item is None:
                break
            try:
                y = np.frombuffer(item[:w * h], dtype=np.uint8).reshape(h, w)
                Image.fromarray(y).save(self.frames_dir / f"frame_{self.written:05d}.jpg",
                                        quality=88)
                self.written += 1
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                break


# --------------------------------------------------------------------------- 事件
@dataclass
class EventInfo:
    id: str
    dir: Path
    reason: str
    score: float
    started: float          # wall clock
    duration: float
    ended: float = 0.0
    csi_preroll: int = 0
    sec_preroll: int = 0
    frames: int = 0
    dropped: int = 0
    mode: str = ""
    importance: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "reason": self.reason,
            "score": round(self.score, 2),
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "duration": round(self.duration, 1),
            "preroll_frames": self.csi_preroll,
            "secondary_frames": self.sec_preroll,
            "frames": self.frames,
            "dropped": self.dropped,
            "mode": self.mode,
            "importance": self.importance,
            "error": self.error,
        }


class EventManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cget(cfg, "events.dir", "events"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.duration = float(cget(cfg, "record.seconds", 10.0))
        self.fps = float(cget(cfg, "record.fps", 15.0))
        self.pre_fps = float(cget(cfg, "preroll.fps", 8.0))
        self.sec_fps = float(cget(cfg, "secondary.fps", 5.0))
        self.size = (int(cget(cfg, "camera.width", 1920)), int(cget(cfg, "camera.height", 1080)))
        self.max_count = int(cget(cfg, "events.max_count", 400))
        self.max_bytes = float(cget(cfg, "events.max_gb", 40.0)) * 1024 ** 3
        # 不留档：超过这个小时数的录像自动删除
        self.max_age = float(cget(cfg, "events.max_age_hours", 24.0)) * 3600.0

        self._lock = threading.Lock()
        self._cur: Optional[EventInfo] = None
        self._rec: Optional[Recorder] = None
        self.on_start = None    # 回调：例如让 ESP32 开补光灯
        self.on_finish = None
        self.total = 0
        self.last_error: Optional[str] = None

    # -- 状态 ---------------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._cur is not None

    @property
    def current(self) -> Optional[EventInfo]:
        with self._lock:
            return self._cur

    def feed_main(self, data: bytes) -> None:
        rec = self._rec
        if rec is not None:
            rec.feed(data)

    # -- 开始一次事件 -------------------------------------------------------
    def start(self, reason: str, score: float,
              csi_frames: List[Frame], sec_frames: Optional[List[Frame]] = None) -> bool:
        with self._lock:
            if self._cur is not None:
                return False
            now = time.time()
            base = f"{stamp(now)}_{reason}"
            d = self.root / base
            n = 1
            while d.exists():
                base = f"{stamp(now)}_{reason}_{n}"
                d = self.root / base
                n += 1
            d.mkdir(parents=True)

            info = EventInfo(id=base, dir=d, reason=reason, score=score,
                             started=now, duration=self.duration)
            try:
                info.csi_preroll = self._write_frames(d / "preroll", csi_frames)
                if sec_frames:
                    info.sec_preroll = self._write_frames(d / "secondary", sec_frames)
                info.mode = "h264" if shutil.which("ffmpeg") else "jpeg"
                self._rec = Recorder(d / "clip.mp4", self.size, self.fps)
                self._cur = info
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("创建事件失败")
                self._cur = None
                self._rec = None
                return False

        self.total += 1
        log.info("▶ 事件开始 %s（原因=%s 分数=%.2f 预录=%d 帧）",
                 info.id, reason, score, info.csi_preroll)
        if self.on_start:
            try:
                self.on_start()
            except Exception:  # noqa: BLE001
                log.exception("on_start 回调失败")
        threading.Thread(target=self._finish, args=(info,), name="evt", daemon=True).start()
        return True

    # -- 收尾 ---------------------------------------------------------------
    def _finish(self, info: EventInfo) -> None:
        try:
            t_end = time.monotonic() + self.duration
            while time.monotonic() < t_end:
                time.sleep(0.1)

            rec = self._rec
            if rec is not None:
                rec.close()
                info.frames = rec.written
                info.dropped = rec.dropped
                info.error = rec.error if rec.error else None
            if info.error:
                self.last_error = info.error

            self._encode_dir(info.dir / "preroll", info.dir / "preroll.mp4", self.pre_fps)
            self._encode_dir(info.dir / "secondary", info.dir / "secondary.mp4", self.sec_fps)

            # 打重要度分（帧差法，几十毫秒）
            try:
                info.importance, _ = self.importance_of(info.dir)
            except Exception:  # noqa: BLE001
                log.debug("计算重要度失败", exc_info=True)

            info.ended = time.time()
            self._write_meta(info)
        except Exception:  # noqa: BLE001
            log.exception("事件收尾失败")
        finally:
            with self._lock:
                self._cur = None
                self._rec = None
            log.info("✓ 事件完成 %s（%d 帧，丢 %d，%s）",
                     info.id, info.frames, info.dropped, self._event_size(info.dir))
            if self.on_finish:
                try:
                    self.on_finish(info)
                except Exception:  # noqa: BLE001
                    log.exception("on_finish 回调失败")
            self.prune()

    def _write_frames(self, d: Path, frames: Optional[List[Frame]]) -> int:
        if not frames:
            return 0
        d.mkdir(parents=True, exist_ok=True)
        stamps = []
        for i, (ts, jpg) in enumerate(frames):
            (d / f"frame_{i:04d}.jpg").write_bytes(jpg)
            stamps.append(round(ts, 3))
        (d / "timestamps.json").write_text(json.dumps(stamps), encoding="utf-8")
        return len(frames)

    def _encode_dir(self, src: Path, out: Path, fps: float) -> bool:
        ff = shutil.which("ffmpeg")
        if not ff or not (src / "frame_0000.jpg").exists():
            return False
        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", f"{fps:g}", "-i", str(src / "frame_%04d.jpg"),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-pix_fmt", "yuv420p", str(out),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("预录转码失败 %s: %s", out.name, exc)
            return False

    def _write_meta(self, info: EventInfo) -> None:
        meta = info.to_dict()
        meta["clip"] = (info.dir / "clip.mp4").name
        meta["cluster"] = {"width": self.size[0], "height": self.size[1], "fps": self.fps}
        (info.dir / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        self._make_thumb(info.dir)

    @staticmethod
    def importance_of(d: Path) -> tuple[int, dict]:
        """给事件打个"重要度"分（0~100）。

        不去跑神经网络：直接拿预录帧做帧差，看**最大那一帧的变化面积**。
        抬手挠头大概 2~5%，人走进画面 20~40%，搬东西更高。
        这一步的成本是几十毫秒，能把八成噪音挡在列表外面。
        """
        pre = d / "preroll"
        if not pre.is_dir():
            return 0, {}
        frames = sorted(pre.glob("frame_*.jpg"))
        if len(frames) < 3:
            return 0, {}

        import numpy as np
        from PIL import Image

        prev = None
        ratios: list[float] = []
        for p in frames:
            try:
                im = Image.open(p).convert("L").resize((64, 36))
            except Exception:  # noqa: BLE001
                continue
            a = np.asarray(im, dtype=np.float32)
            if prev is not None:
                ratios.append(float((np.abs(a - prev) > 18).mean()))
            prev = a

        if not ratios:
            return 0, {}
        peak = max(ratios)
        mean = sum(ratios) / len(ratios)
        score = int(round(min(100.0, peak * 100.0)))
        return score, {"peak": round(peak, 3), "mean": round(mean, 3),
                       "samples": len(ratios)}

    @staticmethod
    def _make_thumb(d: Path) -> None:
        """挑一张预录帧做缩略图，网页列表直接显示。

        选中间那张——事件刚开始时画面还没动静，中间那张更容易看清是谁。
        """
        pre = d / "preroll"
        if not pre.is_dir():
            return
        frames = sorted(pre.glob("frame_*.jpg"))
        if not frames:
            return
        pick = frames[len(frames) // 2]
        try:
            (d / "thumb.jpg").write_bytes(pick.read_bytes())
        except OSError:
            pass

    # -- 列表与清理 ---------------------------------------------------------
    def list_events(self, limit: int = 50, min_importance: int = 0) -> List[dict]:
        items = []
        for d in sorted(self.root.iterdir(), reverse=True) if self.root.exists() else []:
            if not d.is_dir():
                continue
            clip = d / "clip.mp4"
            importance = 0
            meta_path = d / "meta.json"
            if meta_path.exists():
                try:
                    importance = int(json.loads(meta_path.read_text(encoding="utf-8"))
                                     .get("importance", 0))
                except Exception:  # noqa: BLE001
                    importance = 0
            if importance < min_importance:
                continue
            items.append({
                "id": d.name,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d.stat().st_mtime)),
                "size": human(self._event_size(d)),
                "has_clip": clip.exists(),
                "has_pre": (d / "preroll.mp4").exists(),
                "has_thumb": (d / "thumb.jpg").exists(),
                "importance": importance,
                "reason": "sound" if d.name.endswith("_sound") else "motion",
            })
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _event_size(d: Path) -> int:
        total = 0
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def prune(self) -> None:
        dirs = [d for d in self.root.iterdir() if d.is_dir()] if self.root.exists() else []
        dirs.sort(key=lambda p: p.stat().st_mtime)
        cur = self.current
        total = sum(self._event_size(d) for d in dirs)
        cutoff = time.time() - self.max_age
        removed = 0
        while dirs and (len(dirs) > self.max_count or total > self.max_bytes
                        or dirs[0].stat().st_mtime < cutoff):
            victim = dirs[0]
            if cur is not None and victim == cur.dir:
                break
            total -= self._event_size(victim)
            shutil.rmtree(victim, ignore_errors=True)
            dirs.pop(0)
            removed += 1
        if removed:
            log.info("清理了 %d 个旧事件，剩余 %d 个", removed, len(dirs))
