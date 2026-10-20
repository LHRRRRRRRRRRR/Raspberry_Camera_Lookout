"""拍一张真正合焦的照片：先跑自动对焦、跟踪 FocusFoM 峰值，
再把镜头锁在最佳位置拍满分辨率，最后打印曝光信息。

用法：
    python3 shot.py /tmp/a.jpg [宽 高] [等焦秒数]
"""
import sys
import time

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shot.jpg"
W = int(sys.argv[2]) if len(sys.argv) > 2 else 4608
H = int(sys.argv[3]) if len(sys.argv) > 3 else 2592
settle = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0

from picamera2 import Picamera2

pc = Picamera2()
pc.configure(pc.create_still_configuration(main={"size": (W, H)}))
pc.start()

# AfMode 2 = continuous，一边跑一边盯 FocusFoM
pc.set_controls({"AfMode": 2, "AfSpeed": 1})

best_fom, best_pos = -1.0, None
t_end = time.monotonic() + settle
while time.monotonic() < t_end:
    md = pc.capture_metadata()
    fom = float(md.get("FocusFoM", 0.0))
    pos = md.get("LensPosition")
    print(f"  FoM={fom:7.1f}  lens={pos}  exp={md.get('ExposureTime')}us  gain={md.get('AnalogueGain'):.2f}")
    if fom > best_fom:
        best_fom, best_pos = fom, pos
    time.sleep(0.15)

# 锁在最佳位置，再拍
if best_pos is not None:
    pc.set_controls({"AfMode": 0, "LensPosition": float(best_pos)})
    time.sleep(0.4)

md = pc.capture_metadata()
pc.capture_file(out)
pc.stop()
pc.close()

print("--- 结果 ---")
print(f"最佳 FocusFoM = {best_fom:.1f} @ lens={best_pos}")
for k in ("ExposureTime", "AnalogueGain", "Lux", "ColourGains", "ColourTemperature", "SensorTemperature"):
    if k in md:
        print(f"{k:18s} {md[k]}")
print("saved:", out)
