# mijia-desktop 的摄像头取流 / 播放：与本项目对比，以及能不能用

> 2026-11-27 调研对象：<https://github.com/ALittlePandaMan/mijia-desktop>（Tauri 2 + Rust + TypeScript，原创代码 MIT）。
> 结论：**整套方案不采用；只把"关键帧驱动恢复 / 解码资源限流与背压 / 会话代次"这三处思路当参考。**
> 四档状态：**明确排除（整套替换）+ 仅参考（局部机制）**。落点：`docs/reference/README.md` 索引、本文件、`TODO.md`「已作废 / 已取消」。
> **2026-11-27 晚补充**：用户实际试跑对方程序后反馈"开播感觉更快"，并给出它的 microSD 回放时间轴截图。
> 第 6 节据此复核开播体感的来源、重新评估 SD 回放的技术前提；**整套替换的结论不变**。

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
3. **已有能力会被推倒**：我们的开播补丁、MISS `0x112` 云台（补丁版才有）、按 did 多路 relay、卡片 ↔ 详情 ↔ 独立窗口 / 监控页换布局"搬播放器不重连"，都是踩过坑之后的既有成果。对方在这些点上做的是同类选择（关键帧恢复、切解码器不重连），没有不可替代的增量。**"首帧约 2 s"只在预热命中时成立**，本机日志复核见 §6.1。
4. **收益不明确**：对方 `MAX_PAGE_CAMERAS=16` 看着比我们 6 格大，但它靠的是自研栈 + 帧率/分辨率上限 + 后台降画质 + 解码进程池，不是"协议更快"； ours 6 格本来就是 CPU 推算而非真机结论，换栈并不会凭空把软解 CPU 变没。
5. **对方功能范围超出本项目**：SD 回放 / 云录像 / 对讲 / 双镜头是对方的主线，而本项目在 `TODO.md` 里已明确取消回放类功能。为这些功能改写取流栈，属于产品范围变更，不是技术优化。**2026-11-27 用户重新提出 SD 回放**，技术前提有变化，但"整套替换"仍不成立——见 §6.2。

### 4.3 值得抄的三点（不抄码，只抄机制）

- **关键帧驱动恢复**：把"队列满 / 画质变 / 解码器切换 / 会话重建"统一成"丢弃旧帧 → 等下一个 IDR → 从新关键帧开始"，并给这一轮恢复打一个递增的 revision。我们已有 `CameraService.epoch` 与地址缓存，可以补一层"播放器恢复代次"，避免恢复后把旧帧画上去。
- **背压与资源上限**：对方用有界帧队列 + 前端 ack + `pressure` 降分辨率 + 解码进程池上限。我们目前只有"软解默认 + 6 格上限"，没有帧级背压；真机 6 路时若主线程封面刷新积压，这套思路是可直接借鉴的防线。
- **会话不随渲染面重建**：对方"切解码器不重开设备会话"，我们"换承载面/换布局搬 `QMediaPlayer`"，是同一条原则的不同实现——这条已经在我们这边落地，不需要再改。

## 5. 没验证到的部分

- 助手只做了源码与文档静态核对，**没有替用户跑对方的客户端、没有真机对比首帧时间 / 多路 CPU / HEVC 兼容性**，也没在 Windows 上实测它的 WebView2 shared buffer 路径。用户 2026-11-27 自行试跑后反馈"开播感觉更快"，并给出回放界面截图；这部分是**用户观察 + 源码解释**，不是助手掐表对比，见 §6.1。
- 对方文档自述的 15/8 fps、4K 范围、16 路监控墙、离线探针结果均按其原文记录，不是本项目实测。
- 如果将来真要评估"自研协议栈"，前置条件是产品先决定重新开放 SD 回放 / 对讲 / 双镜头，并且愿意接受 Rust + WebView2 的工具链和发行包变化；在此之前按本文档结论**不要重开**。**SD 回放本身可单独评估，不必连协议栈一起换**，见 §6.2。

## 6. 2026-11-27 晚补充：开播体感与 microSD 回放

### 6.1 "感觉对方开播更快"——差在哪里

- **对方进页面就起流**：`src/camera-page.ts:344` 首次拿到设备数据时直接 `queueMicrotask(() => this.start())`，`:470` 恢复状态也直接 `start()`，`:525` 才调 `startCamera()` 走授权 + MISS 协商。用户还没点按钮，连接已在后台建立。
- **我们必须点播放**：`mihome/ui/camera_view.py:10-12` 明写"进页面不取流"，因为一台摄像头只有 3 个观看位、推流占上行。详情页 `ui/detail.py:596-615` 只做**后台预热**（探测 + 拉起 go2rtc + 登记源），不自动播。
- **本机 `logs/camera_timing.log` 复核（2026-11-27，局域网 2960×1666 HEVC）**：

  | 情形 | 点下播放 → 首帧上屏 | 当日样本 |
  | --- | --- | --- |
  | 冷启（go2rtc / producer 未就绪） | **3.68–4.58 s** | 7 次：3.68 / 3.73 / 3.97 / 3.97 / 4.06 / 4.41 / 4.58 |
  | 预热命中后再点播放 | **1.72–2.18 s** | 3 次：1.72 / 1.98 / 2.18 |
  | 预热本身（后台） | 0.08–1.04 s | 9 次，只探测 + 起 go2rtc，不占观看位 |

  冷启那段里 RTSP `OPTIONS/DESCRIBE` 约 0.9 s 才发出、go2rtc `xiaomi: dial` 又要 0.7–2.4 s，之后才是等 IDR；预热命中后这一整轮被省掉。此前文档/脚本里"首帧约 2 s"指的是**预热命中**，不是冷启。
- **所以用户的体感是合理的**：对方自动起流 + Rust 进程内直连（没有 go2rtc 进程、没有本地 RTSP 层），我们点播放才开始协商且多两层本机转发；如果点击时预热还没跑完，差距更明显。**这不是"对方协议更快"**——双方都要等 IDR，对方 `LensStream.needs_keyframe=true` 同样丢掉非关键帧。
- **可做的低风险动作（都还没做，待拍板）**：
  1. 详情页改成"进页面自动起流"（或默认关、设置里开），把预热升级成真起流。代价是打开详情就占一个观看位、走上行带宽，和现在"不自动取流"的产品决定冲突。
  2. 保持不自动播，但把预热完成反馈到按钮文案（"后台连接中 / 可播放"），别让用户在预热没完成时以为点了播放才开始。
  3. 不换栈，只在自建 go2rtc 上压缩拨号 / RTSP 段（收益上限约 0.5–1.5 s）。

### 6.2 microSD 回放：能拿到，但不是"换个播放器"

- 截图是 **microSD 卡**的时间轴（今天 / 黄色录像段 / 全部回看视频），不是云录像。云录像需要 VIP，账号 `vipStatusEnum: NONE_VIP`、7/30/180 天全 0 条的历史结论不变，**仍不排期**。
- **旧结论的技术前提被部分推翻**：2026-11-08 记的是"SD 回放要另起 P2P `replay` 会话（另一套 `init_string` + 录像索引查询），等于把跨网握手再做一遍"。对方源码证明 **SD 回放可以在同一条 MISS 会话里完成**：

  | 环节 | 对方源码 | 做法 |
  | --- | --- | --- |
  | 录像索引 | `camera/media_control.rs:683-700` + `camera/rdt.rs:40-70,102-125` | `rdt::index_request(channel=0, bytes=24, declared=0)` 写控制通道 1，分帧后按 8 字节/条解析 `start/duration/event/channel` |
  | 开始回放 | `media_control.rs:717-756` | 加密命令 `0x10d`，JSON `sessionid/starttime/endtime/offset/speed/channel/avchannelmerge/autoswitchtolive`，等 `0x10e` 回包 |
  | 倍速 / 回直播 | `media_control.rs:758-789` | `0x10f` 仅支持 1× / 4×；回直播也是 `0x10d`（`autoswitchtolive=1`） |
  | 媒体流 | `camera.rs` 同一 producer | 回放包仍走原来的视频通道与 TS 封装，不需要第二套 P2P |

- **型号支持**：`src/camera-profiles.json` 的 `sd` 标准档包含 `chuangmi.camera.069a01`（`indexBytes=24`、`indexDeclared=0`、单 channel 0）——就是家里这台。这是"路径可行"的证据，**不是我们已真机验过索引和回放**。
- **我们要补的三块，缺一不可**：
  1. **go2rtc 侧**：自建版本来就有 keyframe / PTZ 两个补丁，再加 `0x10d`/Live 命令封装、RDT 索引请求与解析（约百来行）、录像列表 API，以及"用回放命令起流"的 producer。上游 go2rtc 只定义了 `cmdPlaybackReq=0x10D` / `cmdPlaybackRes=0x10E` 两个常量，**既没有实现方法，也没有 recordings 接口**，别指望升级版本白拿。
  2. **Python 侧**：`core/camera.py` 增加回放会话状态与地址，和实时会话互斥（一台设备 3 个观看位，不能两边各开一条）。
  3. **UI 侧**：详情页时间轴（按天切、录像段着色、拖动定位、1×/4×、返回直播）。现在的 `PlaybackOwnership` 是"播放器换承载面"的所有权，不是 SD 回放，别混用。
- **结论**：技术上**能拿到**，代价是在自建 go2rtc 上补一段协议 + 一个时间轴 UI，属于中等工程；**不等于采用对方整套 Rust/WebView2 方案**，§4.2 的整套替换结论不变。旧排除理由中"价值低于成本"属产品判断，现在用户重新提出，前提变了 → 已从"明确排除"改挂 `TODO.md` B 段待拍板。
- **动手前的前置验证**：确认 SD 卡已插、设备在线，先只探索引列表（只读、不改设备设置），拿到真实录像段后再决定是否做播放器。按项目纪律，真正起回放会话前还要在对话里申请。
