# 摄像头首帧：钱花在哪、这轮省了哪一段（2026-11-28）

> 结论：**冷启那 0.7–1.0 s 的云端换票已经挪出关键路径**（新补丁
> `go2rtc-vendor-cache.patch` + 程序启动即拉起 go2rtc），实测首帧 3.45 s → 2.61 s。
> 剩下的 ~1 s 在 **Qt 的 FFmpeg 播放器**里，再往下压就得换播放栈；本轮**不动**。
> 四档状态：**已落地**（换票缓存 + 启动预热）；**已否决**（probe 前缀补发）；
> **待拍板**（换播放器 / 常驻保活）。

## 1. 一句话现状

| 情形 | 首帧（点开 → 画面） | 说明 |
| --- | --- | --- |
| 程序起来 3 s 后再开摄像头（**本轮之后**） | **2.61 s** | 换票已由后台预热做好 |
| 同上（本轮之前） | **3.45 s** | 换票落在用户等待里 |
| 同一台设备再开一次（go2rtc 已热） | 2.2–2.4 s | 拨号、摄像头重启都还是冷的 |
| mijia-desktop（对方 exe，外部掐表） | **0.8–1.1 s** | 进程内 Rust 直连 + MSE，没有 go2rtc 进程 / 本地 RTSP / QMediaPlayer 缓冲 |

## 2. 逐段实测（局域网 2960×1666 HEVC，同一台 069a01）

给 go2rtc 打了临时调试输出（记录在 scratch 构建里，未入库）后的单轮数据：

| 阶段 | 冷启 | 热（5 s 内重开） | 能不能省 |
| --- | --- | --- | --- |
| `miss_get_vendor` 换票（含 TLS + token 登录） | **700–1000 ms** | 90–110 ms | **能**，票据几十秒内有效 → 缓存 + 预热 |
| CS2 打洞 | 14–20 ms | 15–20 ms | 不能，本来就快 |
| `0x100` 登录握手 | 125–155 ms | 98–157 ms | 不能 |
| `StartMedia` + probe 拿到首包 | 50–240 ms | 200–240 ms | 不能（要等摄像头回包） |
| 开播补丁的 stop / 150 ms / start | ~150–300 ms | 同 | 能，但见 §3 的否决结论 |
| 首帧进 go2rtc 的 receiver | 拨号后 +0.2–0.4 s | 同 | — |
| **RTSP → QMediaPlayer → 首帧上屏** | **0.6–1.0 s** | 0.5–0.7 s | 只能换播放栈 |

关键一点：**首帧进 receiver 的时刻，比"画面出来"早 0.6–1.0 s**。也就是说后段的大头
在播放器侧（Qt FFmpeg 的探测 / 缓冲 / 首帧解码），不是取流侧。这条与
11-09 那次应用内日志（`媒体元数据就绪 1998 ms → 首帧 2093 ms`，当时播放器只花 95 ms）
不一致，差异来自"那次是暖流 + 局域网 + 播放器已预热"；本轮统一用
`scripts/measure_camera_coldstart.py` 的口径（每次接流后等 2.5 s，冷生产者）。

## 3. 已否决：probe 前缀补发（2026-11-28 深夜，实测数据在案）

**想法**：开播补丁现在是"probe 读走开头几包 → StopMedia → 150 ms → StartMedia → 等新
IDR"。如果把 probe 读过的包缓存下来、`Start()` 按原顺序补发，就不用重起，省掉那
0.15 s + 一次 IDR 编码。

**实测（同一台设备、同一网络、每档 3 轮）**：

| 方案 | 冷启 | 热（2.5 s 内重开） |
| --- | --- | --- |
| 现有补丁（stop / 150 ms / start） | 3.0–3.6 s | 2.4–2.6 s |
| 补发 probe 前缀（本轮实验） | 3.4–5.7 s | **4.6–5.8 s** |

**为什么更慢**（调试输出实据）：热态下 `StartMedia` 之后先到的是**上一轮会话残留的
包**——probe 读到的"首个视频包"时间戳比同时刻的音频包早 0.6–0.8 s，且它后面跟的是
`nal=1`（TRAIL_R / 非关键帧）。把这些补发出去，FFmpeg 拿到的第一帧不可解码
（`Error constructing the frame RPS` / `Skipping invalid undecodable NALU: 1`），
只能等下一个 IDR——正好又是一个 GOP（4 s 上下）。冷启那一轮首包里确实有 IDR，
所以冷启只是"没变快"，热态直接退化。

也就是说：**stop→start 那个 150 ms 不是浪费，它是在强制摄像头从 IDR 重新开一路**，
这正是补发方案丢掉的东西。此路封死，别再试。

## 4. 这轮落地的两处

### 4.1 `go2rtc-vendor-cache.patch`（自建 go2rtc 的第三个补丁）

- 换票结果按 `did|host` 缓存 **10 分钟**（键里带 host：远程中继与局域网直连是两张
  不同的票，混用拨不通）；
- **落盘**到 go2rtc 工作目录 `vendor-cache.json`（0600，与 `go2rtc.yaml` 同级）——
  因为本程序会因"换取流源"重启 go2rtc，纯内存缓存在重启后清零，而重启后那次开播
  最需要这张票；
- 拨号失败且用的是缓存票时**丢掉该条**，让 go2rtc 的重连走一次真实换票，不会拿着
  坏票反复失败；
- 进程启动 0.5 s 后**后台把配置里每条 `xiaomi://` 源先换一遍**（只发 HTTPS，不拨号、
  不占观看位）。

### 4.2 程序启动就把 go2rtc 拉起来

`MainWindow._warm_camera_service()`：设备表首次加载完，后台线程跑一次
`camera_service().prepare()`（监控页用的同一个入口，只登记源）。go2rtc 的 producer
是惰性的——没有消费者连 RTSP 就不拨号、不建会话，所以这一步**零观看位、零上行**，
和 11-19 那次实测一致。

两处合起来，把"进程启动 0.85 s + 换票 0.7 s"从点击后的关键路径上挪走。

## 5. 还剩什么（要真追平 1 s，只能动这些）

1. **播放器侧 0.6–1.0 s**（最大一块）：Qt `QMediaPlayer` 的 FFmpeg 后端有自己的探测
   与缓冲策略，我们改不了它的 `probesize` / `analyzeduration`。可选做法：go2rtc 直接
   吐 MPEG-TS over HTTP，Python 侧用 PyAV 在进程内解码（`fflags=nobuffer`、
   `flags=low_delay`、`probesize=32`、`analyzeduration=0`），把 `QImage` 交给现在的
   `CameraView.paintEvent` 自绘。**这是换播放栈**，工作量与风险都比前两处大，且要
   重新验证多路、声音、云台、换承载面不重连。
2. **摄像头重启那段 0.2–0.4 s**：已在 §3 证明不能用"补发"消掉；只能在"常驻保活一条
   会话"上省——代价是长期占一个观看位 + 上行带宽（用户 11-27 已接受"打开页面就起流"，
   但"后台常驻"是另一档代价，要单独拍板）。
3. **换子码流**（`MIHOME_CAMERA_SUBTYPE=sd`）：IDR 从 ~177 KB 降到 ~21 KB，首帧能再快
   一点，代价是清晰度明显下降。做成了启动参数，不是设置项。

## 6. 复现方式

```bash
# 冷启动首帧（每次接流后等 2.5 s，保证是冷生产者）
.venv\Scripts\python.exe -X utf8 scripts\measure_camera_coldstart.py ^
    --binary "%LOCALAPPDATA%\MiHomePanel\go2rtc\go2rtc.exe" --rounds 3

# 对照官方构建（首帧 4.8–6.3 s）
... --binary "%LOCALAPPDATA%\MiHomePanel\go2rtc\go2rtc-official.exe"
```

要复现"程序启动 3 s 后再开"这一档，先起 go2rtc、等 3 s 再 `player.play()`（本轮用
scratch 驱动脚本做的，未入库）。
