"""极简 HTTP 服务：网页看板 + MJPEG 直播 + 事件列表。

刻意只用标准库（http.server），不引入 Flask：
Pi 5 只有 2GB，少一个依赖少一份风险，而且不用配 venv。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from util import cget

log = logging.getLogger("vision.web")


CSS = """
*{box-sizing:border-box}body{margin:0;background:#0e1116;color:#c9d1d9;
font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:12px 18px;border-bottom:1px solid #21262d;display:flex;
align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.5px;font-weight:600}
h1 span{color:#58a6ff}
.badge{padding:2px 8px;border-radius:10px;font-size:12px;background:#21262d}
.badge.on{background:#1f6f3f;color:#7ee2a8}.badge.rec{background:#7d1d1d;color:#ff9c9c}
.wrap{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:14px;padding:14px}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#8b949e;margin:0 0 10px}
img{width:100%;border-radius:6px;display:block;background:#000}
button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;
padding:7px 12px;cursor:pointer;font-size:13px;margin-right:6px}
button:hover{background:#30363d}button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
button.primary:hover{background:#388bfd}
.kv{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px dashed #21262d}
.kv b{font-weight:500;color:#8b949e}
table{width:100%;border-collapse:collapse;font-size:12px}
td{padding:5px 4px;border-bottom:1px solid #21262d;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}a{color:#58a6ff;text-decoration:none}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
"""

JS = """
async function tick(){
  try{
    const s = await (await fetch('/status')).json();
    document.getElementById('armed').textContent = s.armed ? '已布防':'仅监视';
    document.getElementById('armed').className = 'badge ' + (s.armed?'on':'');
    const r = document.getElementById('rec');
    r.textContent = s.recording ? '录制中':'待机';
    r.className = 'badge ' + (s.recording?'rec':'');
    document.getElementById('fps').textContent = s.fps.toFixed(1);
    document.getElementById('score').textContent = s.motion_score.toFixed(2);
    document.getElementById('area').textContent = (s.motion_area*100).toFixed(2)+'%';
    document.getElementById('pre').textContent = s.preroll_frames;
    document.getElementById('frames').textContent = s.frames;
    document.getElementById('csi').textContent = s.sources.csi ? '正常':'异常';
    document.getElementById('sec').textContent = s.sources.secondary ? '在线':'离线';
    const pr = document.getElementById('prof');
    pr.textContent = s.profile === 'day' ? '白天·彩色':'夜间·黑白';
    pr.className = 'badge ' + (s.profile === 'day' ? 'on':'rec');
    document.getElementById('lux').textContent = s.lux;
    if(!sliderSynced){
      sliderSynced = true;
      document.getElementById('fpsr').value = s.stream_fps;
      document.getElementById('fpsv').textContent = s.stream_fps+' fps';
    }
    document.getElementById('fpsreal').textContent = s.fps.toFixed(1)+' fps';
    var bw = s.stream_fps * (s.frame_bytes || 0) * 8 / 1e6;
    document.getElementById('bw').textContent = s.stream_fps > 0
      ? '≈' + bw.toFixed(1) + ' Mbps'
      : '已暂停（0 流量）';
  }catch(e){}
}
async function events(){
  try{
    const min = document.getElementById('onlyImp').checked ? 15 : 0;
    const list = await (await fetch('/events?limit=15&min='+min)).json();
    const t = document.getElementById('ev');
    t.innerHTML = list.map(function(e){
      const clock = e.id.replace(/^(\d{8})-(\d{2})(\d{2})(\d{2})_.*/, '$2:$3:$4');
      const thumb = e.has_thumb
        ? '<a href="/events/'+e.id+'/clip.mp4"><img src="/events/'+e.id+
          '/thumb.jpg" style="width:96px;height:54px;object-fit:cover;border-radius:4px;display:block"></a>'
        : '<div style="width:96px;height:54px;background:#21262d;border-radius:4px"></div>';
      const pre = e.has_pre ? ' · <a href="/events/'+e.id+'/preroll.mp4">事前</a>' : '';
      const imp = e.importance >= 40 ? '<span style="color:#f0883e">重要 '+e.importance+'</span>'
                : e.importance >= 15 ? '<span style="color:#8b949e">一般 '+e.importance+'</span>'
                : '<span style="color:#484f58">轻 '+e.importance+'</span>';
      return '<tr><td style="width:104px;padding:4px 4px 4px 0">'+thumb+'</td>'+
             '<td><a href="/events/'+e.id+'/clip.mp4">'+clock+'</a> '+imp+'<br>'+
             '<span style="color:#8b949e">'+e.size+' · '+
             (e.reason==='sound'?'声音':'移动')+pre+'</span></td></tr>';
    }).join('') || '<tr><td colspan=2 style="color:#8b949e">没有符合条件的事件</td></tr>';
  }catch(e){}
}
setInterval(tick,1000);setInterval(events,5000);tick();events();

var slideTimer=null, sliderSynced=false;
function onSlide(v){
  document.getElementById('fpsv').textContent = v+' fps';
  clearTimeout(slideTimer);
  slideTimer = setTimeout(function(){
    fetch('/fps?value='+v).catch(function(){});
  }, 120);
}
function setRes(p){
  if(!confirm('切换画质档位会重启服务，约 4 秒。继续吗？')) return;
  fetch('/res?preset='+p).then(function(){
    setTimeout(function(){ location.reload(); }, 6000);
  }).catch(function(){});
}
"""


def _page() -> bytes:
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vision</title><style>{CSS}</style></head><body>
<header>
  <h1>vi<span>sion</span> · 事件守夜</h1>
  <span class="badge" id="armed">…</span>
  <span class="badge" id="rec">…</span>
  <span class="badge">CSI <b id="csi">…</b></span>
  <span class="badge">副摄 <b id="sec">…</b></span>
  <span class="badge">FPS <b id="fps">0</b></span>
  <span class="badge">运动 <b id="score">0</b> / 面积 <b id="area">0%</b></span>
  <span class="badge">预录 <b id="pre">0</b> 帧 / 累计 <b id="frames">0</b></span>
  <span class="badge" id="prof">…</span>
  <span class="badge">照度 <b id="lux">0</b> lux</span>
</header>
<div class="wrap">
  <div class="card">
    <h2>实时画面</h2>
    <div style="display:flex;gap:8px;align-items:flex-start">
      <div style="flex:2;min-width:0">
        <img src="/stream" alt="live">
        <div style="color:#8b949e;font-size:12px;margin-top:4px">主摄 · Camera Module 3 NoIR</div>
      </div>
      <div style="flex:1;min-width:0">
        <img src="/stream2" alt="secondary">
        <div style="color:#8b949e;font-size:12px;margin-top:4px">副摄 · ESP32 广角</div>
      </div>
    </div>
    <div style="margin-top:10px">
      <button class="primary" onclick="fetch('/trigger').then(()=>0)">手动触发</button>
      <button onclick="fetch('/arm?on=1').then(()=>0)">布防</button>
      <button onclick="fetch('/arm?on=0').then(()=>0)">撤防</button>
      <button onclick="location.reload()">刷新</button>
    </div>
    <div style="margin-top:8px">
      <span style="color:#8b949e;font-size:12px;margin-right:6px">滤镜配置</span>
      <button onclick="fetch('/profile?name=day').then(()=>0)">白天（装滤镜）</button>
      <button onclick="fetch('/profile?name=night').then(()=>0)">夜间（拆滤镜）</button>
      <button onclick="fetch('/profile?name=auto').then(()=>0)">自动</button>
    </div>
    <div style="margin-top:12px;padding-top:10px;border-top:1px solid #21262d">
      <div style="color:#8b949e;font-size:12px;margin-bottom:8px">
        帧率 —— 拖动实时生效（MJPEG 带宽 = 单帧字节 × 帧率）
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <input type="range" id="fpsr" min="0" max="120" step="1" value="5"
               style="flex:1" oninput="onSlide(this.value)">
        <span class="mono" id="fpsv" style="width:74px;text-align:right">5 fps</span>
      </div>
      <div style="margin-top:6px;color:#8b949e;font-size:12px">
        实际 <b id="fpsreal" style="color:#c9d1d9">—</b>
        <span class="mono" id="bw" style="margin-left:10px">—</span>
      </div>
      <div style="margin-top:10px;padding-top:8px;border-top:1px dashed #21262d">
        <span style="color:#8b949e;font-size:12px;margin-right:6px">画质档位</span>
        <button onclick="setRes('hd')">高清 1080p（最高 56fps）</button>
        <button onclick="setRes('fast')">高帧率 720p（可到 120fps）</button>
      </div>
      <div style="margin-top:6px;color:#8b949e;font-size:12px">切档位会重启服务，约 4 秒</div>
    </div>
  </div>
  <div class="card">
    <h2>最近事件</h2>
    <label style="display:flex;align-items:center;gap:6px;margin-bottom:8px;
                  color:#8b949e;font-size:12px;cursor:pointer">
      <input type="checkbox" id="onlyImp" onchange="events()"> 只看值得看的（重要度 ≥ 15）
    </label>
    <table><tbody id="ev"><tr><td colspan=2 style="color:#8b949e">加载中…</td></tr></tbody></table>
  </div>
</div>
<script>{JS}</script></body></html>"""
    return html.encode("utf-8")


class App:
    """把各组件打包给 handler 用。"""

    def __init__(self, cfg: dict, pipeline, events, csi=None):
        self.cfg = cfg
        self.pipeline = pipeline
        self.events = events
        self.csi = csi
        self.token = str(cget(cfg, "server.token", "") or "")
        self.root = Path(__file__).resolve().parent
        # 网页上可实时调整：MJPEG 带宽 = 单帧字节 × 这个帧率
        self.stream_fps = float(cget(cfg, "server.stream_fps", 5.0))
        self.started = time.time()
        self.page = _page()


def _make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ 工具
        def log_message(self, fmt, *a):  # 安静点
            log.debug("%s - %s", self.address_string(), fmt % a)

        def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
                  extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _authed(self, q: dict) -> bool:
            if not app.token:
                return True
            return q.get("token", [""])[0] == app.token

        # ------------------------------------------------------------ 路由
        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            path = u.path
            q = parse_qs(u.query)
            p = app.pipeline

            if path == "/":
                getattr(self, "_send")(200, app.page, "text/html; charset=utf-8")
            elif path == "/status":
                self._json(self._status())
            elif path == "/events":
                limit = int(q.get("limit", ["30"])[0])
                min_imp = int(q.get("min", ["0"])[0])
                self._json(app.events.list_events(limit, min_imp))
            elif path == "/snapshot.jpg":
                self._snapshot()
            elif path == "/stream":
                self._stream(app.pipeline.ring)
            elif path == "/stream2":
                self._stream(app.pipeline.sec_ring)
            elif path == "/trigger":
                if not self._authed(q):
                    self._json({"ok": False, "error": "bad token"}, 403)
                else:
                    ok = p.trigger("manual", 0.0)
                    self._json({"ok": ok, "busy": p.events.busy})
            elif path == "/profile":
                name = q.get("name", ["auto"])[0].lower()
                if name not in ("day", "night", "auto"):
                    self._json({"ok": False, "error": "name 只能是 day / night / auto"}, 400)
                elif not self._authed(q):
                    self._json({"ok": False, "error": "bad token"}, 403)
                else:
                    p.request_profile(name)
                    self._json({"ok": True, "requested": name})
            elif path == "/wb":
                if not self._authed(q):
                    self._json({"ok": False, "error": "bad token"}, 403)
                elif app.csi is None:
                    self._json({"ok": False, "error": "no camera"}, 503)
                else:
                    def num(k):
                        v = q.get(k, [None])[0]
                        return float(v) if v not in (None, "") else None
                    st = app.csi.set_live(num("r"), num("b"), num("ev"))
                    self._json({"ok": True, "live": st})
            elif path == "/wb_reset":
                if app.csi is None:
                    self._json({"ok": False, "error": "no camera"}, 503)
                else:
                    self._json({"ok": True, "live": app.csi.reset_live()})
            elif path == "/fps":
                v = q.get("value", [None])[0]
                try:
                    fps = max(0.0, min(120.0, float(v)))
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "value 必须是数字"}, 400)
                    return
                app.stream_fps = fps
                # 0 = 只暂停推流；大于 0 时连采集帧率一起改
                if fps > 0 and app.csi is not None:
                    app.csi.set_framerate(fps)
                log.info("帧率改为 %.0f fps", fps)
                self._json({"ok": True, "fps": fps})
            elif path == "/res":
                preset = q.get("preset", ["hd"])[0].lower()
                table = {
                    # 高清：1920x1080，传感器该模式上限 56fps
                    "hd": {"camera": {"width": 1920, "height": 1080, "fps": 15},
                           "preroll": {"size": [1280, 720], "fps": 15},
                           "record": {"fps": 15}},
                    # 高帧率：降到 720p，传感器 1536x864@120 模式，实测可跑满 120
                    "fast": {"camera": {"width": 1280, "height": 720, "fps": 120},
                             "preroll": {"size": [640, 360], "fps": 120},
                             "record": {"fps": 30}},
                }
                if preset not in table:
                    self._json({"ok": False, "error": "preset 只能是 hd / fast"}, 400)
                    return
                try:
                    (app.root / "runtime.json").write_text(
                        json.dumps(table[preset], indent=2), encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 500)
                    return
                log.info("切换画质档位 → %s，即将重启", preset)
                self._json({"ok": True, "preset": preset, "restart": True})
                # 借 systemd 的 Restart=always 来重启自己，不需要 sudo
                threading.Timer(0.6, lambda: os._exit(0)).start()
            elif path == "/stream_fps":
                v = q.get("value", [None])[0]
                try:
                    fps = max(0.0, min(30.0, float(v)))
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "value 必须是数字"}, 400)
                    return
                app.stream_fps = fps
                log.info("推流帧率改为 %.1f fps", fps)
                self._json({"ok": True, "stream_fps": fps})
            elif path == "/arm":
                on = q.get("on", ["1"])[0] not in ("0", "false", "no")
                if not self._authed(q):
                    self._json({"ok": False, "error": "bad token"}, 403)
                else:
                    p.armed = on
                    self._json({"ok": True, "armed": on})
            elif path.startswith("/events/"):
                self._event_file(path[len("/events/"):])
            elif path == "/favicon.ico":
                self._send(204, b"")
            else:
                self._send(404, b"not found")

        do_POST = do_GET

        # ------------------------------------------------------------ 具体
        def _status(self) -> dict:
            p = app.pipeline
            p.health()
            s = p.status()
            cur = app.events.current
            s.update({
                "uptime": int(time.time() - app.started),
                "events_total": app.events.total,
                "current_event": cur.id if cur else None,
                "last_error": app.events.last_error,
                "stream_fps": app.stream_fps,
                "frame_bytes": (len(p.ring.latest()[1]) if p.ring.latest() else 0),
            })
            return s

        def _snapshot(self) -> None:
            frame = app.pipeline.ring.latest()
            if not frame:
                self._send(503, b"no frame yet")
                return
            self._send(200, frame[1], "image/jpeg")

        def _stream(self, ring) -> None:
            """MJPEG 直播。

            注意：MJPEG 每帧都是一张完整 JPEG，所以带宽 = 单帧字节 × 帧率。
            想省流量就把 stream_fps 调低（1~2 就够看有没有人）。
            ring 由调用方传进来：主流是 pipeline.ring，副摄是 pipeline.sec_ring。
            """
            boundary = b"frame"
            min_interval = 0.0
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=" + boundary.decode())
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_ts = -1.0
            last_sent = 0.0
            idle = 0.0
            try:
                while True:
                    fps = float(app.stream_fps)      # 每轮重读，改帧率立即生效
                    if fps <= 0:                     # 暂停：保持连接但不发数据
                        time.sleep(0.1)
                        idle = 0.0
                        continue
                    frame = ring.latest()
                    now = time.monotonic()
                    if (frame and frame[0] != last_ts
                            and now - last_sent >= 1.0 / fps):
                        last_ts = frame[0]
                        last_sent = now
                        head = (b"--" + boundary + b"\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Content-Length: " + str(len(frame[1])).encode() + b"\r\n\r\n")
                        self.wfile.write(head + frame[1] + b"\r\n")
                        idle = 0.0
                    else:
                        # 轮询间隔决定推流上限：5ms ≈ 200fps 天花板
                        time.sleep(0.005)
                        idle += 0.005
                        if idle > 30:
                            break
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _event_file(self, rel: str) -> None:
            root = Path(cget(app.cfg, "events.dir", "events")).resolve()
            target = (root / rel).resolve()
            if not str(target).startswith(str(root)) or not target.is_file():
                self._send(404, b"not found")
                return
            ctype = "video/mp4" if target.suffix == ".mp4" else (
                "image/jpeg" if target.suffix in (".jpg", ".jpeg") else "application/octet-stream")
            data = target.read_bytes()
            self._send(200, data, ctype)

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), _make_handler(app))
    srv.daemon_threads = True
    log.info("HTTP 服务已启动: http://%s:%d", host, port)
    return srv
