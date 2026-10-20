"""白平衡校准（迭代版）。

为什么要迭代：
  传感器的响应是线性的，但 ISP 会做 gamma / 色调映射，
  两者叠加后"线性域算出的增益"在输出上会过冲。
  所以这里在**渲染后的图像**上反复测量、反复修正，3 轮基本收敛。

用法：
    python3 awb_cal.py
    python3 awb_cal.py --box 0.3,0.2,0.7,0.8 --ev -0.7
"""

from __future__ import annotations

import argparse
import time

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="/tmp/cal_final.jpg")
ap.add_argument("--ev", type=float, default=0.0)
ap.add_argument("--rounds", type=int, default=3)
ap.add_argument("--box", default="0.30,0.20,0.70,0.80")
ap.add_argument("--flip", default="rot180")
args = ap.parse_args()

from libcamera import Transform
from picamera2 import Picamera2

hflip, vflip = (1, 1) if args.flip == "rot180" else (0, 0)

pc = Picamera2()
pc.configure(pc.create_still_configuration(
    main={"size": (1280, 720)}, transform=Transform(hflip=hflip, vflip=vflip)))
pc.start()
pc.set_controls({"AwbEnable": False, "ColourGains": (1.0, 1.0),
                 "ExposureValue": args.ev})

x0, y0, x1, y1 = (float(v) for v in args.box.split(","))


def measure() -> tuple[float, float, float]:
    time.sleep(1.0)                       # 等 AE / AWB 稳定
    img = pc.capture_array("main").astype(np.float32)[..., :3]
    h, w = img.shape[:2]
    roi = img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    px = roi.reshape(-1, 3)
    m = (px.max(axis=1) < 248) & (px.min(axis=1) > 8)
    if m.sum() > 1000:
        px = px[m]
    mr, mg, mb = (float(v) for v in px.mean(axis=0))
    return mr, mg, mb


gr, gb = 1.0, 1.0
for i in range(args.rounds):
    pc.set_controls({"ColourGains": (gr, gb)})
    mr, mg, mb = measure()
    print(f"第 {i+1} 轮  增益=({gr:.3f},{gb:.3f})  "
          f"输出均值 R={mr:6.1f} G={mg:6.1f} B={mb:6.1f}  "
          f"偏差 R/G={mr/mg:.3f} B/G={mb/mg:.3f}")
    # 就在输出域上修正：哪个通道偏亮就把它的增益压下去
    gr *= mg / max(mr, 1e-3)
    gb *= mg / max(mb, 1e-3)

pc.set_controls({"ColourGains": (gr, gb)})
mr, mg, mb = measure()
pc.capture_file(args.out)
md = pc.capture_metadata()
pc.stop()
pc.close()

print(f"\n最终 ColourGains = ({gr:.3f}, {gb:.3f})   EV={args.ev}")
print(f"最终输出均值     R={mr:6.1f} G={mg:6.1f} B={mb:6.1f}")
print(f"Lux={md.get('Lux'):.1f}  曝光={md.get('ExposureTime')}us  "
      f"增益={md.get('AnalogueGain'):.2f}  FocusFoM={md.get('FocusFoM')}")
print("saved:", args.out)
