# mijia-desktop 的摄像头取流 / 播放：与本项目对比，以及能不能用

> 2026-11-27 调研对象：<https://github.com/ALittlePandaMan/mijia-desktop>（Tauri 2 + Rust + TypeScript，原创代码 MIT）。
> 结论：**整套方案不采用；只把"关键帧驱动恢复 / 解码资源限流与背压 / 会话代次"这三处思路当参考。**
> 四档状态：**明确排除（整套替换）+ 仅参考（局部机制）**。落点：`docs/reference/README.md` 索引、本文件、`TODO.md`「已作废 / 已取消」。

## 1. 一句话区别

两边**连摄像头的底层协议是同一套**（CS2/MISS，`miss_get_vendor` 拿到 `vendor=4`），
分歧在"谁实现协议"和"怎么把码流送进播放器"：

| | 本项目（现状） | mijia-desktop |
| --- | --- | --- |
| 协议栈 | **外部进程 go2rtc**（MIT）里现成的 Go 实现 | **Rust 自研**：`src-tauri/src/camera.rs` 注释明写"CS2/MISS framing derived from Xiaomi Camera Viewer/go2rtc (MIT)"，参考协议、不搬代码、不分发 Go 运行时 |
| 授权协商 | go2rtc 内部做 | Rust `negotiate_with_host()`：X25519 临时密钥 → `/v2/device/miss_get_vendor`（`support_vendors: TUTK_CS2_MTP`）→ hsalsa 派生 32 字节 key → `0x100` JSON 鉴权，媒体包用 ChaCha20 解 |
| 交付播放器 | go2rtc 开**本机 RTSP**（`rtsp://127.0.0.1:<port>/cam_<did>`），Qt `QMediaPlayer` 拉流 | Rust 把解密后的 H.264/HEVC 用 `ts::Mux` 打成 **MPEG-TS 片段**，base64 塞进 `Event.data` 推给前端 |
| 播放 / 渲染 | Qt FFmpeg 后端 → `QVideoSink` → `CameraView.paintEvent` 自绘（cover/contain） | 前端 `native-video.ts` 用 **mpegts.js + 自定义 IpcLoader（`type='xiaomi-native-session'`）** 喂 MSE / `<video>`；软件兜底时换 `software-video.ts` 的 canvas |
| 解码兜底 | 只有软/硬解开关（默认软解） | 随包 `mijia-media-decoder.exe`（FFmpeg 9.0.2 动态库，LGPL-2.1-or-later）：stdin 收 TS + 12 字节视口 / 8 字节压力控制记录，stdout 出 40 字节 `MJFR` 帧头 + RGBA 像素，经 WebView2 `CreateSharedBuffer` / `PostSharedBufferToScript` 送前端，前端 `ack` 才发下一帧 |
| 进程隔离 | go2rtc 一个外部进程；解码在 UI 进程内 | Windows **Job Object** 包住解码进程：单进程 512 MB / 总 1 GB / 最多 16 个 / CPU 硬上限（系统不支持 CPU 配额时仍保留前三项） |
| 多路 | 上限 **6 格**（推算，未真机铺满），切走全停，进页面不自动播 | `MAX_PAGE_CAMERAS = 16`；进监控只连当前页，隐藏时**保持连接并降到最低画质**，回前台从新关键帧恢复；正在录像 / 对讲 / 回看时阻止切走 |
| 功能范围 | 实时画面 + 声音 + 云台；SD 回放 / 云端录像 / 对讲明确取消 | 实时 + 双镜头 + 画质切换 + SD 回放 + 对讲 + 云录像 / 看家助手；资料里明确说"浏览器 Mock 通过不代表真机播放验收" |

## 2. 对方取流链路（源码证据）

`src-tauri/src/camera.rs`：

1. `negotiate(cloud, home, device)` → 从家庭设备列表取 `localip`，电池门铃先走一次唤醒；
2. `negotiate_with_host()` 生成 X25519 密钥，`miss_get_vendor` 换 `vendor=4` 与 `p2p_id`/`init_string`/`sign`；
3. `authenticate()` 发 `0x100` JSON，之后控制命令用 `encoded(cmd, body, key)`（`0x1001` + 8 字节 nonce + ChaCha20）；
4. 工作线程读通道 2 的媒体包：`codec`（4=h264 / 5=hevc）、`timestamp`、`flags`（含 lens id），去掉 40 字节头后解密；
5. `mp4::has_keyframe(codec, &payload)` 判 IDR；`LensStream::new()` 的 `needs_keyframe = true`，**没拿到关键帧之前直接丢弃**；画质变化 / 格式变化 / 队列满都会把它重新置 true 并重置 TS muxer 与 `stream_revision`；
6. `ts::Mux::video()` 输出 TS 片段 → `STANDARD.encode(bytes)` → `Event { data, keyframe, codec, stream_revision, media_generation, ... }` → 有界队列 `queue::channel()` → Tauri 事件推前端。

要点：**它并没有比我们更"秒开"的魔法**。会话起点同样要等 IDR（我们靠 `go2rtc-keyframe.patch` 把这段从 4.8–6.3 s 压到约 2 s；对方源码里是等下一个关键帧）。它的优势在关键帧把"恢复路径"统一了：切画质、切解码器、队列积压后都从 IDR 重新开始，不会有半截不可解的帧。

`src-tauri/src/camera/media_control.rs`：`Operation::{Quality, Listen, Talk, Ptz, Record, Playback, Live, Microphone}` 都在同一条会话里做；`0x109/0x10a` 画质确认、`0x108` 对讲、`0x10f` 回看倍速、`LIVE_FRAME=0xffffffff`。双镜头靠 `flags` 里的 lens id 分流。

## 3. 对方播放 / 解码链路（源码证据）

- `src/native-video.ts`：默认走 mpegts.js 的 MSE 播放（`systemVideoPlayer` + `IpcLoader`），**不是**直接给 `<video>` 一个 URL；失败（编解码不支持 / MSE error / 12 秒没出帧）才 `fallback()` 到软件路径。切换时**只换播放器，不重开设备会话**（源码原话："A decoder switch never opens or replays a device session"），缓存的 TS 片段喂给新播放器，并要求从关键帧开始。
- `src/camera-lens-player.ts`：`decodeCameraPacket(event.data)`（base64 → Uint8Array）后 `player.feed(bytes.buffer, event.keyframe)`；`BUFFER_LIMIT=4MB`、`FRAME_TIMEOUT=15s`；截图走 `canvas.drawImage(mediaSource(video))`，软件路径时 `mediaSource` 返回 canvas。
- `src-tauri/src/camera_decode.rs`：`camera_decode_open()` 检查 WebView2 shared buffer 能力 → 起 `media-decoder/mijia-media-decoder.exe` → stdin 管道写 TS + 控制记录；`preview-output` 线程读 `MJFR` 帧头（kind 1=视频 RGBA / 2=48kHz 音频 / 4=结束，`bytes = w*h*4`）→ `post_frame()` 用 `CreateSharedBuffer` 零拷贝送前端 → `camera_decode_ack(sequence)` 收到确认才继续。积压时 `pressure` 升档，视口从原尺寸逐级降到 1280×720 / 854×480；文档还给了 1080p 及以下 15 fps、更高 8 fps 的上限。
- `docs/VIDEO_PLAYBACK.md`：这些数字和策略大多来自文档自述 + 离线探针，文档自己承认"浏览器 Mock 与离线编码样本通过，不代表故障电脑的原生播放已经验收"。**别把它当真机实测结论。**

## 4. 我们能用吗？

### 4.1 法律上：能借鉴，但不是"拿来即用"

- 对方原创代码 MIT，与本项目 GPL-3.0-or-later 兼容；要搬代码必须保留版权与许可声明。
- 它的 FFmpeg 组件是 **LGPL-2.1-or-later**（未启用 GPL / nonfree / 网络协议），与 GPLv3 兼容，但要随组件提供源码获取方式、构建命令与哈希——对方自己也是这么做的。
- 它参考的 go2rtc、Xiaomi Camera Viewer 都是 MIT（我们已经在用 go2rtc）。
- **不能拿**的是它随包的米家 APK 静态图形、MiSans 字体等第三方素材：MIT 不覆盖它们，对方文档也单独声明了权利归属。
- 我们没复制任何对方代码，只在本文档记录结论与思路，状态登记为 `仅参考` / `明确排除`。

### 4.2 工程上：不建议整套替换

1. **两层都要换**：取流从 go2rtc 子进程换成 Rust 自研协议栈，播放从 Qt `QMediaPlayer` 换成 WebView2 + mpegts.js + FFmpeg 子进程。这不是"加个开关"，是把 `core/camera.py`、`ui/camera_view.py`、`ui/monitor_page.py` 和打包链重写一遍。
2. **引入新运行时依赖**：Rust 工具链、WebView2 Runtime（对方 Windows-only，Job Object / shared buffer 都是 Win32 API）、随包 FFmpeg 组件。本项目目前跨平台，go2rtc 是可选外部进程，发行包只多 7.7 MB；换过去会失去这条轻量性。
3. **已有能力会被推倒**：我们的开播补丁（首帧约 2 s）、MISS `0x112` 云台（补丁版才有）、按 did 多路 relay、卡片 ↔ 详情 ↔ 独立窗口 / 监控页换布局"搬播放器不重连"，都是踩过坑之后的既有成果。对方在这些点上做的是同类选择（关键帧恢复、切解码器不重连），没有不可替代的增量。
4. **收益不明确**：对方 `MAX_PAGE_CAMERAS=16` 看着比我们 6 格大，但它靠的是自研栈 + 帧率/分辨率上限 + 后台降画质 + 解码进程池，不是"协议更快"； ours 6 格本来就是 CPU 推算而非真机结论，换栈并不会凭空把软解 CPU 变没。
5. **对方功能范围超出本项目**：SD 回放 / 云录像 / 对讲 / 双镜头是对方的主线，而本项目在 `TODO.md` 里已明确取消回放类功能。为这些功能改写取流栈，属于产品范围变更，不是技术优化。

### 4.3 值得抄的三点（不抄码，只抄机制）

- **关键帧驱动恢复**：把"队列满 / 画质变 / 解码器切换 / 会话重建"统一成"丢弃旧帧 → 等下一个 IDR → 从新关键帧开始"，并给这一轮恢复打一个递增的 revision。我们已有 `CameraService.epoch` 与地址缓存，可以补一层"播放器恢复代次"，避免恢复后把旧帧画上去。
- **背压与资源上限**：对方用有界帧队列 + 前端 ack + `pressure` 降分辨率 + 解码进程池上限。我们目前只有"软解默认 + 6 格上限"，没有帧级背压；真机 6 路时若主线程封面刷新积压，这套思路是可直接借鉴的防线。
- **会话不随渲染面重建**：对方"切解码器不重开设备会话"，我们"换承载面/换布局搬 `QMediaPlayer`"，是同一条原则的不同实现——这条已经在我们这边落地，不需要再改。

## 5. 没验证到的部分

- 只做了源码与文档静态核对，**没有跑对方的客户端、没有真机对比首帧时间 / 多路 CPU / HEVC 兼容性**，也没在 Windows 上实测它的 WebView2 shared buffer 路径。
- 对方文档自述的 15/8 fps、4K 范围、16 路监控墙、离线探针结果均按其原文记录，不是本项目实测。
- 如果将来真要评估"自研协议栈"，前置条件是产品先决定重新开放 SD 回放 / 对讲 / 双镜头，并且愿意接受 Rust + WebView2 的工具链和发行包变化；在此之前按本文档结论**不要重开**。
