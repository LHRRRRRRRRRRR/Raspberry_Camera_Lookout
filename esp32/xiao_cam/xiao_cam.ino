/* ============================================================================
 *  xiao_cam.ino  ——  XIAO ESP32S3 Sense 第二机位 + 红外补光灯
 *
 *  和树莓派的连接：一根 USB-C 线（供电 + 数据，USB CDC）
 *
 *  ─── 两种工作方式 ───────────────────────────────────────────────────────
 *
 *  1) 文本命令（换行结尾，ASCII）—— 人可以直接在串口监视器里敲：
 *
 *       PING              → PONG
 *       STAT              → OK STAT frames=123 psram=8123456 lux=-1
 *       LAMP 6000         → OK LAMP 6000      （点亮补光灯 6 秒，自动关）
 *       FPS 10            → OK FPS 10         （限制推流帧率 1~30）
 *       SNAP              → OK SNAP 12345     （紧接着发一帧二进制，见下）
 *       STREAM 1 / 0      → OK STREAM 1       （开始/停止连续推流）
 *       MODE vga|qvga     → OK MODE vga       （切换分辨率，会重启摄像头）
 *       HELP              → 打印命令表
 *
 *  2) 二进制帧（Pi 端解析）—— 每帧 JPEG 前面加 7 字节头：
 *
 *       0xA5 0x5A | type(1B) | len(4B 小端) | JPEG 数据
 *       type = 0x01 表示图像帧
 *
 *     USB 本身有 CRC 校验，所以不再加校验和；解析时按 len 读满即可。
 *
 *  ─── Arduino IDE 设置（很关键）────────────────────────────────────────
 *
 *     Board          : XIAO_ESP32S3
 *     PSRAM          : OPI PSRAM        ← 不开这个，摄像头缓冲分配会失败
 *     USB CDC On Boot: Enabled          ← 不开这个，串口收不到任何东西
 *     Flash Size     : 8MB
 *     Port           : 插上后出现的那个 COM 口
 *
 * ==========================================================================*/

#include "esp_camera.h"
#include "driver/i2s.h"

// ─────────────────────────────────────────────── 麦克风（PDM）
// 这两个脚在板上是固定的；如果读到的电平一直是 0，用 MIC 命令对调试试：
//   串口敲  MIC 41 42   或   MIC 42 41
int micDataPin = 41;
int micClkPin  = 42;
bool micReady  = false;
int  micThreshold = 0;          // 0 = 关闭声音触发，由 Pi 下发
uint32_t lastSoundMs = 0;
uint32_t soundEvents = 0;
float micPeak = 0;

// ─────────────────────────────────────────────── 引脚定义（XIAO ESP32S3 Sense）
#define PWDN_GPIO_NUM   -1
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM   10
#define SIOD_GPIO_NUM   40
#define SIOC_GPIO_NUM   39
#define Y9_GPIO_NUM     48
#define Y8_GPIO_NUM     11
#define Y7_GPIO_NUM     12
#define Y6_GPIO_NUM     14
#define Y5_GPIO_NUM     16
#define Y4_GPIO_NUM     18
#define Y3_GPIO_NUM     17
#define Y2_GPIO_NUM     15
#define VSYNC_GPIO_NUM  38
#define HREF_GPIO_NUM   47
#define PCLK_GPIO_NUM   13

// 补光灯控制脚：D0 = GPIO1，接 MOS 管栅极（高电平点亮）
#define LAMP_PIN        1

// ─────────────────────────────────────────────── 帧头
#define MAGIC0          0xA5
#define MAGIC1          0x5A
#define TYPE_JPEG       0x01
#define TYPE_SOUND      0x02      // 声音事件：负载是 4 字节 float（RMS 电平）

// ─────────────────────────────────────────────── 状态
bool     streaming   = false;
uint8_t  targetFps   = 10;
uint32_t frameCount  = 0;
uint32_t lastFrameMs = 0;
uint32_t lastEmitMs  = 0;
uint32_t lampOffAt   = 0;       // 0 = 灯灭
framesize_t frameSize = FRAMESIZE_VGA;
int      jpegQuality  = 12;     // 0~63，越小越清晰越大

// ─────────────────────────────────────────────── 摄像头初始化
bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;

  c.pin_pwdn  = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.pin_xclk  = XCLK_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;   // 老版板包叫 pin_sscb_sda
  c.pin_sccb_scl = SIOC_GPIO_NUM;   // 老版板包叫 pin_sscb_scl

  c.pin_d0 = Y2_GPIO_NUM;
  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM;

  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href  = HREF_GPIO_NUM;
  c.pin_pclk  = PCLK_GPIO_NUM;

  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    c.frame_size  = frameSize;
    c.jpeg_quality = jpegQuality;
    c.fb_count    = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode   = CAMERA_GRAB_LATEST;
  } else {
    // 没 PSRAM 就只能小分辨率单缓冲，勉强能跑
    c.frame_size  = FRAMESIZE_QVGA;
    c.jpeg_quality = 15;
    c.fb_count    = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.grab_mode   = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("ERR CAM 0x%x\n", err);
    return false;
  }
  return true;
}

// ─────────────────────────────────────────────── 麦克风
bool micInit(int clk, int data) {
  if (micReady) {
    i2s_driver_uninstall(I2S_NUM_0);
    micReady = false;
  }
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
  cfg.sample_rate = 16000;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 4;
  cfg.dma_buf_len = 256;
  cfg.use_apll = false;

  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL) != ESP_OK) return false;

  i2s_pin_config_t pins = {};
  pins.mck_io_num   = I2S_PIN_NO_CHANGE;
  pins.bck_io_num   = I2S_PIN_NO_CHANGE;
  pins.ws_io_num    = clk;      // PDM 的时钟走 ws
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num  = data;

  if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) return false;
  i2s_start(I2S_NUM_0);
  micReady = true;
  return true;
}

// 读一小段音频，返回 RMS 电平（0~32767 量级）
float micReadLevel() {
  if (!micReady) return 0;
  int16_t buf[512];
  size_t got = 0;
  if (i2s_read(I2S_NUM_0, buf, sizeof(buf), &got, 50 / portTICK_PERIOD_MS) != ESP_OK) return 0;
  int n = got / 2;
  if (n <= 0) return 0;
  double sum = 0;
  for (int i = 0; i < n; i++) sum += (double)buf[i] * (double)buf[i];
  return (float)sqrt(sum / n);
}

// 超过阈值就往串口塞一个声音事件包
void micPoll() {
  if (!micReady) return;
  if (millis() - lastSoundMs < 100) return;   // 每 100ms 采样一次
  lastSoundMs = millis();

  float lvl = micReadLevel();
  if (lvl > micPeak) micPeak = lvl;

  if (micThreshold <= 0) return;              // 未启用
  if (lvl < (float)micThreshold) return;
  if (millis() - lastEmitMs < 500) return;    // 事件间至少隔 0.5s

  lastEmitMs = millis();
  soundEvents++;

  uint8_t head[7] = { MAGIC0, MAGIC1, TYPE_SOUND, 4, 0, 0, 0 };
  Serial.write(head, sizeof(head));
  Serial.write((uint8_t *)&lvl, 4);
  Serial.flush();
}

// ─────────────────────────────────────────────── 补光灯
void lampOn(uint32_t ms) {
  digitalWrite(LAMP_PIN, HIGH);
  lampOffAt = millis() + ms;
}

void lampLoop() {
  if (lampOffAt && (int32_t)(millis() - lampOffAt) >= 0) {
    digitalWrite(LAMP_PIN, LOW);
    lampOffAt = 0;
    Serial.println("EVT LAMP 0");
  }
}

// ─────────────────────────────────────────────── 发一帧（二进制）
bool sendFrame(framesize_t want = FRAMESIZE_INVALID) {
  if (want != FRAMESIZE_INVALID && want != frameSize) {
    frameSize = want;
    sensor_t *s = esp_camera_sensor_get();
    s->set_framesize(s, frameSize);
  }

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("ERR FB");
    return false;
  }

  uint32_t len = fb->len;
  uint8_t head[7] = { MAGIC0, MAGIC1, TYPE_JPEG,
                      (uint8_t)(len & 0xFF), (uint8_t)((len >> 8) & 0xFF),
                      (uint8_t)((len >> 16) & 0xFF), (uint8_t)((len >> 24) & 0xFF) };

  Serial.write(head, sizeof(head));
  Serial.write(fb->buf, len);
  Serial.flush();

  esp_camera_fb_return(fb);
  frameCount++;
  return true;
}

// ─────────────────────────────────────────────── 命令处理
void handleCommand(String line) {
  line.trim();
  if (line.length() == 0) return;

  int sp = line.indexOf(' ');
  String cmd = (sp < 0) ? line : line.substring(0, sp);
  String arg = (sp < 0) ? ""   : line.substring(sp + 1);
  cmd.toUpperCase();

  if (cmd == "PING") {
    Serial.println("PONG");
  } else if (cmd == "STAT") {
    Serial.printf("OK STAT frames=%lu psram=%u heap=%u fps=%u stream=%d size=%d\n",
                  (unsigned long)frameCount, (unsigned)ESP.getPsramSize(),
                  (unsigned)ESP.getFreeHeap(), targetFps, streaming ? 1 : 0,
                  (int)frameSize);
  } else if (cmd == "LAMP") {
    uint32_t ms = (arg.length() ? arg.toInt() : 6000);
    if (ms == 0) { digitalWrite(LAMP_PIN, LOW); lampOffAt = 0; Serial.println("OK LAMP 0"); }
    else { lampOn(ms); Serial.printf("OK LAMP %lu\n", (unsigned long)ms); }
  } else if (cmd == "FPS") {
    int v = arg.toInt();
    if (v < 1) v = 1;
    if (v > 30) v = 30;
    targetFps = (uint8_t)v;
    Serial.printf("OK FPS %d\n", targetFps);
  } else if (cmd == "SNAP") {
    bool was = streaming;
    streaming = false;
    bool ok = sendFrame();
    streaming = was;
    Serial.printf(ok ? "OK SNAP %lu\n" : "ERR SNAP\n", (unsigned long)frameCount);
  } else if (cmd == "STREAM") {
    streaming = (arg.toInt() != 0);
    Serial.printf("OK STREAM %d\n", streaming ? 1 : 0);
  } else if (cmd == "MODE") {
    framesize_t want = frameSize;
    if (arg.equalsIgnoreCase("vga"))  want = FRAMESIZE_VGA;
    else if (arg.equalsIgnoreCase("qvga")) want = FRAMESIZE_QVGA;
    else if (arg.equalsIgnoreCase("svga")) want = FRAMESIZE_SVGA;
    else { Serial.println("ERR MODE"); return; }
    frameSize = want;
    sensor_t *s = esp_camera_sensor_get();
    if (s) s->set_framesize(s, frameSize);
    Serial.printf("OK MODE %d\n", (int)frameSize);
  } else if (cmd == "Q") {
    int v = arg.toInt();
    if (v < 4) v = 4;
    if (v > 40) v = 40;
    jpegQuality = v;
    sensor_t *s = esp_camera_sensor_get();
    if (s) s->set_quality(s, jpegQuality);
    Serial.printf("OK Q %d\n", jpegQuality);
  } else if (cmd == "SMIC") {
    micThreshold = arg.toInt();
    Serial.printf("OK SMIC %d\n", micThreshold);
  } else if (cmd == "SND") {
    float lvl = micReadLevel();
    Serial.printf("OK SND level=%.1f peak=%.1f events=%lu thr=%d mic=%d\n",
                  lvl, micPeak, (unsigned long)soundEvents, micThreshold, micReady ? 1 : 0);
  } else if (cmd == "MIC") {
    int a = 0, b = 0;
    if (sscanf(arg.c_str(), "%d %d", &a, &b) == 2) {
      micDataPin = a;
      micClkPin  = b;
      bool ok = micInit(micClkPin, micDataPin);
      Serial.printf("OK MIC %d %d %s\n", micDataPin, micClkPin, ok ? "ok" : "fail");
    } else {
      Serial.println("ERR MIC 用法: MIC <data> <clk>");
    }
  } else if (cmd == "HELP") {
  Serial.println("cmds: PING STAT LAMP <ms> FPS <n> SNAP STREAM <0|1> MODE <vga|qvga|svga> Q <4-40> SMIC <thr> SND MIC <data> <clk> HELP");
  } else {
    Serial.printf("ERR CMD %s\n", cmd.c_str());
  }
}

// ─────────────────────────────────────────────── setup / loop
void setup() {
  pinMode(LAMP_PIN, OUTPUT);
  digitalWrite(LAMP_PIN, LOW);

  Serial.begin(115200);
  delay(300);

  Serial.println();
  Serial.println("=== xiao_cam ===");

  if (!initCamera()) {
    Serial.println("摄像头初始化失败：检查 PSRAM 是否设为 OPI PSRAM");
  } else {
    Serial.printf("camera ok, psram=%u\n", (unsigned)ESP.getPsramSize());
  }

  // 麦克风（PDM）。失败不影响摄像头，只是没有声音触发
  if (micInit(micClkPin, micDataPin)) {
    float lvl = micReadLevel();
    Serial.printf("mic ok, data=%d clk=%d level=%.1f\n", micDataPin, micClkPin, lvl);
  } else {
    Serial.println("mic 初始化失败（可用 MIC 命令换脚位重试）");
  }

  Serial.println("READY");
}

void loop() {
  lampLoop();
  micPoll();

  // 1) 处理命令（流式传输时也保持响应）
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    handleCommand(line);
  }

  // 2) 按目标帧率推流
  if (streaming) {
    uint32_t interval = 1000UL / (targetFps ? targetFps : 1);
    if (millis() - lastFrameMs >= interval) {
      lastFrameMs = millis();
      sendFrame();
    }
  }
}
