# Lookout

**一台会"等"的本地视觉哨兵。**

不 7×24 录像。平时只观察，只在对的那一刻，把**事前 4 秒 + 事后 10 秒**、两台相机的画面一起存下来。
全程本地，不上云、不联网、不发通知给任何第三方。

```
                     ┌──────────────────────────────────────────┐
   Camera Module 3   │  Raspberry Pi 5 (2GB)                    │
   NoIR  ── CSI-2 ──▶│                                          │
                     │  libcamera/ISP ─┬─▶ 预录环（4s JPEG）     │
                     │                 ├─▶ ffmpeg → clip.mp4     │
                     │                 └─▶ MJPEG → 浏览器        │
   XIAO ESP32S3      │                                          │
   Sense  ── USB ───▶│  串口帧解析 ──────▶ 副摄环 + secondary.mp4 │
   (广角 / 麦克风)    │                                          │
                     │  systemd 常驻 · HTTP 看板 · 自动清理       │
                     └──────────────────────────────────────────┘
```

## 它特别在哪

大多数监控是"不停地录，事后翻"。这套反过来：**内存里一直滚着最近 4 秒**，
检测到动（或听到响）才落盘——所以它拍得到**人踏进门之前的那一秒**。

## 硬件

| 部件 | 型号 | 作用 |
|---|---|---|
| 主机 | Raspberry Pi 5 / 2GB | 大脑 |
| 主摄 | Raspberry Pi Camera Module 3 **NoIR** | 主画面，能收红外 |
| 副摄 | Seeed XIAO ESP32S3 **Sense** | 广角第二机位 + PDM 麦克风 + 补光灯控制 |
| 补光 | 850nm 红外灯板 + MOS 管（可选） | 夜间可视（**没有它就等于没有夜视**） |
| 滤镜 | 可拆 IR-cut 滤片（可选） | 白天颜色正常 |

## 快速开始

### 1. Pi 端

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-numpy python3-pil python3-serial ffmpeg

git clone <this-repo> ~/lookout
cd ~/lookout/pi
cp ../config.example.toml config.toml   # 然后按需修改
python3 main.py                          # 先前台跑，看清楚日志
```

跑通后装成服务：

```bash
sudo cp vision.service /etc/systemd/system/
sudo systemctl enable --now vision
```

打开 `http://<pi>.local:8080`。

### 2. ESP32 固件

用 Arduino IDE 打开 `esp32/xiao_cam/xiao_cam.ino`，设置：

| 选项 | 值 |
|---|---|
| Board | `XIAO_ESP32S3` |
| PSRAM | **OPI PSRAM**（不开摄像头必挂） |
| Flash Size | 8MB |
| USB CDC On Boot | **Enabled**（不开串口收不到任何东西） |

烧完插到 Pi 的 USB 口，`ls /dev/ttyACM0` 应该能看到。

自检：

```bash
python3 tools/esp32_test.py --port /dev/ttyACM0 --snap /tmp/a.jpg --lamp 2000
```

## 配置

`config.toml` 每个段落都有中文注释，重点几个：

| 键 | 说明 |
|---|---|
| `camera.profile` | `auto` 按光照切日/夜；`day`/`night` 强制 |
| `motion.area_ratio` | 变化像素占比阈值，**调大压误报** |
| `preroll.seconds` | 事前留几秒（这套东西的灵魂） |
| `record.seconds` | 事后录几秒 |
| `events.max_age_hours` | 过期自动删 |
| `server.stream_fps` | MJPEG 推流帧率，直接决定带宽 |
| `sound.threshold` | 声音触发阈值，`0` 关闭 |

## HTTP 接口

| 端点 | 作用 |
|---|---|
| `/` | 网页看板（双画面 + 事件列表 + 帧率滑块） |
| `/status` | JSON 状态 |
| `/stream` `/stream2` | 主摄 / 副摄 MJPEG |
| `/snapshot.jpg` | 主摄单帧 |
| `/trigger` `/arm?on=1` | 手动触发 / 布防撤防 |
| `/profile?name=day\|night\|auto` | 日/夜配置 |
| `/fps?value=N` | 采集 + 推流帧率（0 = 暂停推流） |
| `/res?preset=hd\|fast` | 1080p/15fps 或 720p/120fps（会重启） |
| `/events?limit=&min=` | 事件列表（`min` = 重要度过滤） |

## 事件目录

```
events/20261020-232532_motion/
├── clip.mp4         主摄，事后 N 秒，全分辨率
├── preroll.mp4      主摄，事前 N 秒，低清
├── preroll/         事前逐帧 JPEG（用于算重要度）
├── secondary/       副摄逐帧
├── secondary.mp4    副摄片段
├── thumb.jpg        缩略图（预录中间帧）
└── meta.json        元数据 + 重要度
```

**重要度**是拿预录帧做帧差算出来的 0~100 分：抬手挠头 2~5，人走进画面 20~40。
不做识别，但能把八成噪音挡在列表外。

## 踩过的坑

见 [docs/PITFALLS.md](docs/PITFALLS.md)。里面每一个都是真金白银调出来的：

- 不写 `format` 时 `main` 流默认是 4 字节/像素，喂给 `yuv420p` 的 ffmpeg 会整帧错位
- `RGB888` 输出路径和 `YUV420` 的色彩不一致
- ESP32 重连时要先发 `STREAM 0`，否则握手读到的是旧视频流
- 信号处理函数里调 `srv.shutdown()` 会死锁

## 许可

MIT
