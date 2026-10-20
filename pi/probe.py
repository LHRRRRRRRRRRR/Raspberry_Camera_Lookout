"""最小采集探针：绕开 rpicam-apps，直接用 picamera2 拍一张并打印全部元数据。"""
import json
import sys
import time

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/py_test.jpg"

import libcamera
print("libcamera:", libcamera.__file__)

from picamera2 import Picamera2

pc = Picamera2()
props = pc.camera_properties
print("Model:", props.get("Model"))
print("Location:", props.get("Location"))
print("PixelArraySize:", props.get("PixelArraySize"))
print("UnitCellSize:", props.get("UnitCellSize"))
print("ColourFilter:", props.get("ColourFilter"))
print("available modes:", len(pc.sensor_modes))

cfg = pc.create_still_configuration(main={"size": (2304, 1296)})
pc.configure(cfg)
pc.start()
time.sleep(1.5)

md = pc.capture_metadata()
ok = False
try:
    pc.capture_file(out)
    ok = True
except Exception as exc:  # noqa: BLE001
    print("capture_file 失败:", exc)
    from PIL import Image
    import numpy as np

    arr = pc.capture_array("main")
    Image.fromarray(np.ascontiguousarray(arr)).save(out, quality=92)
    ok = True
finally:
    pc.stop()
    pc.close()

keys = [
    "ExposureTime", "AnalogueGain", "DigitalGain", "Lux", "ColourTemperature",
    "ColourGains", "FocusFoM", "LensPosition", "SensorTemperature", "FrameDuration",
]
print("--- metadata ---")
for k in keys:
    if k in md:
        v = md[k]
        print(f"{k:18s} {v}")

print("saved:", out, "ok=", ok)
