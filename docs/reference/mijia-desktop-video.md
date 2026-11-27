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

- 2026-11-28 凌晨助手**已真机掐表对比首帧**（对方约 0.8–1.1 s、我方约 2.2–3.0 s，
  见 §6.1.2）；但仍**没实测多路 CPU / HEVC 兼容性**，没在 Windows 上验证它的
  WebView2 shared buffer 路径。对方 exe 是下载版、源码无 Rust 工具链，故对方侧只有
  外部屏幕掐表，没有内部计时探针。
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
- **低风险动作（当时的候选）**：
  1. ~~详情页改成"进页面自动起流"~~ → **2026-11-27 深夜已落地，见下**。
  2. 保持不自动播，但把预热完成反馈到按钮文案（"后台连接中 / 可播放"）——未做（已被第 1 条取代）。
  3. 不换栈，只在自建 go2rtc 上压缩拨号 / RTSP 段（收益上限约 0.5–1.5 s）——未做。

**2026-11-27 晚先落地的开关版（已撤回）**：曾加 `prefs.camera_preheat`（默认关），
设置 → 设备 → 摄像头；源码核对发现对方 `camera.rs:611-615` 在云端返回 relay 服务器
列表时**本身就是远程优先**（没有列表才局域网优先），所以预热跟随「取流方式」。
用户实测开预热仍显著慢（见 §6.1.1），开关**同日按用户决定删除**。对方也不是"程序
一启动就占"，而是进摄像头页 / 监控页自动起流时才占；若启动恢复了那一页，看起来才像
一开程序就占。

**2026-11-27 深夜最终落地：按对方方式做**。用户拍板"取消预热开关和取流方式开关，
按他的方式实现看看效果"，于是：

- `DetailDialog._auto_start_camera()` 构造末尾 `singleShot(0, ...)` 触发，打开详情页
  就走 `_start_camera()` 建会话 + 接画面（**会话级**，不是旧版那种只起进程的假预热）；
  `_cam_adopted`（从卡片墙接续）跳过，不重复 open。代价：打开页面就占一个观看位 +
  上行带宽——这正是对方付的那份。
- 设置页「取流方式」下拉与「摄像头预热」开关删除，设备页只留一张说明卡；
  `camera_preheat` 字段从 prefs v22 起彻底移除；取流通道固定**远程优先、失败退回
  局域网**（`CAMERA_NETWORK_DEFAULT = remote_first`，对方口径），底层字段保留给
  `measure_app_play.py` 覆盖做通道对比。
- 验证 `scripts/verify_camera_autoplay.py`（15 项路由 + 偏好断言）。**效果由用户真机
  对比，助手未掐表**；本段只记实现，不预判提速幅度。

#### 6.1.1 用户实测「开了预热仍显著慢」——预热的深度不够（2026-11-27 深夜再核）

用户开启上面的开关后实测：本程序**仍显著慢于对方**，即便远程档牺牲了一个观看位也追不上。
复核 `logs/camera_timing.log` 的逐段时间戳（**注意：该文件最后写入停在 11-09 16:05，用户
这次实测没开 `MIHOME_CAMERA_TIMING=1`，没有新记录**；但旧记录的链路拆分足以定位），
结论是**我方预热只做了进程级准备，没做会话级起流**：

| 阶段 | 记录 #1「后台预热」(0–947 ms) | 记录 #2「点播放 → 首帧」(0–3969 ms) |
| --- | --- | --- |
| 探测 + 起 go2rtc 进程 | 有（`/api` 就绪 874 ms） | — |
| 下发 RTSP 地址 | 有（874 ms） | — |
| consumer 连 RTSP（DESCRIBE） | **无** | 809 ms |
| `xiaomi: dial` 摄像头拨号 | **无** | 1593 ms（DESCRIBE 后 +784 ms） |
| producer 起来 / MISS 协商 | **无** | 2374 ms（+781 ms） |
| 媒体元数据就绪 | **无** | 3298 ms（+870 ms） |
| 首个关键帧解码上屏 | **无** | 3969 ms（+564 ms，等 IDR） |

- **预热只省掉 go2rtc 子进程启动那约 0.85 s**。go2rtc 的 xiaomi producer 是**惰性的**：
  没有 consumer 连上 RTSP 就不拨号、不建 producer、不占观看位（正是 `core/camera.py`
  `prepare()` 注释里"零成本预置"的由来）。所以预热记录里从头到尾没有 `dial` 一行。
- **真正耗时的约 3 s 全在点播放之后**：consumer 连 RTSP → dial 拨摄像头 → MISS 协商 →
  等 IDR 关键帧 → QMediaPlayer 缓冲出画。这段预热一概没覆盖，因此"开了预热还是慢"。
- **远程预热更亏**：`open(remote=True)` 建的 relay 会话，`relay.py:_join()` 只是让**本机**
  加入 relay 服务器（`MSG_RLY_RDY`），不等于摄像头加入 MISS 视频会话；producer 仍要等
  RTSP DESCRIBE 之后才 dial（这次经本地 relay 代理）。于是"占观看位"的代价付了，
  "提前拨号 + 提前收流"的收益却没拿到。
- **用户没记错：详情页本来就在做预热**。`ui/detail.py:590 _prefetch_camera()` 打开详情页就跑，
  但同样只探测 + 起 go2rtc + 登记源，是**进程级预热**。上一轮新增的主窗口开关只是把这一步
  提前到开主窗口，深度没变，所以追不上是必然的。
- **对方快的真正原因**：正式版 `camera-page.ts:344` 无条件 `start()`（那个 `allowCameraAutoplay`
  guard 只在 `import.meta.env.DEV` 下存在，正式版恒为 `undefined`），`:525` → `startCamera()`
  → `state_camera.rs:582 negotiate()` 云端换票 → worker 线程立刻拨号 / 认证 / 发 `0x102`
  start video → **开始收流并把 TS 推给前端 MSE**。它把拨号、协商、等 IDR、缓冲全挪到"进页面"
  那一刻，用户盯着看时流已经热了；且 Rust 进程内直连，没有 go2rtc 子进程、没有本地 RTSP 层、
  没有 QMediaPlayer 缓冲。**不是协议更快**——对方同样 `needs_keyframe=true` 要等 IDR。
- **要真追平只有一条路**：把预热从"进程级"升级成"会话级"——预热阶段就拨号并保持一个
  consumer（让 producer 一直跑、关键帧一直缓冲），点播放时播放器直接接已热的 RTSP 流。
  代价正是对方付的那个：占观看位 + 上行带宽，与本项目"不自动取流"的产品决定冲突。
  **用户 2026-11-27 深夜拍板接受该代价，已按此实现**：详情页打开即自动起流（见 §6.1
  末段），两个开关删除。真机提速幅度待用户实测。

#### 6.1.2 真机掐表：对方约 0.8–1.1 s，我方约 2.2–3.0 s（2026-11-28 凌晨，助手实测）

用户反馈"改成自动起流后仍有明显差距"，这次**助手真机实测两台程序**（同一台
`chuangmi.camera.069a01`、同一网络、都走远程；对方是桌面 exe
`mijia-desktop-v0.1.0-...portable`，源码没有 Rust 工具链**无法重编译加探针**，改用
**外部屏幕掐表**：ctypes 点击 + PIL 高频截画面 ROI）。

- **判据的坑（务必记下，别再踩）**：第一版用 ROI 灰度 `stddev>35` 判"首帧"，得出
  对方 339 ms / 我方 224 ms——**全是误判**。弹窗自身的深色背景、设备卡片里的静态
  缩略图都会让 stddev 跳变；颜色数判据同样被卡片图标污染（base 就 mean=54/colors=1990）。
  **可信做法**：分时刻截图人工核对 + 用"弹窗黑色加载态（mean≈18）→ 视频画面
  （mean≈60、std≈80）"的跳变，并确认 base 是"弹窗已关"的干净态。
- **对方**（点击摄像头卡片 → 详情页画面，3 轮 + 等 16 s 让它会话过期再测一轮）：

  | 时刻 | 画面状态 |
  | --- | --- |
  | 460 / 739 ms | 黑屏 + `0 KB/S` + 加载点（**还没出画**） |
  | 1125 ms | 实时画面已出（**真首帧**） |

  等 16 s 后重测：207 ms 仍黑屏、319 ms 起有画面、439 ms 稳定 → 会话过期与否
  差别不大，**对方真实首帧约 0.8–1.1 s**。不是会话保活（源码 `check_viewer_poll`
  10 s 无活动才停，但 16 s 后仍快）。
- **我方**（关弹窗 → 点设备卡片标题 → `_auto_start_camera()`，同一 ROI）：

  | 时刻 | 画面区域 mean / std |
  | --- | --- |
  | base（弹窗已关） | 64.8 / 44.1（设备卡片预览，正常） |
  | 100–1000 ms | 18.3 / 19.9（弹窗黑色加载态） |
  | 1500 / 2200 ms | 19.0 / 14.9（**仍黑屏**） |
  | 3000 ms | 62.9 / 79.6（**首帧上屏**） |

  内部 `MIHOME_CAMERA_TIMING=1` 日志同轮给出**精确 2667 ms**
  （记录 #1 通道=远程中继：relay 握手 131 ms → `start producer` / RTSP 建流
  1125 ms → `xiaomi: dial` 1306 ms → 媒体元数据 2592 ms → 首帧解码 2656 ms →
  上屏 2667 ms），与外部掐表 2.2–3.0 s 吻合。**我方真实首帧约 2.2–3.0 s**。
- **结论：自动起流已落地且生效，但差距仍在（约 2 倍）**。根因是**架构层**，不是
  起流时机：对方 Rust 进程内直连 MISS、TS 经 mpegts.js MSE 直喂
  （`native-video.ts:34` `enableStashBuffer:false`、`lazyLoad:false`），拨号在
  `camera.rs:573` 的 worker 线程异步跑、`start_reserved()` 立刻返回 session_id；
  我方必经 go2rtc 子进程 → `xiaomi://` producer 拨号 → 本地 RTSP → QMediaPlayer
  缓冲四层，日志里 relay + RTSP + dial + 元数据就占了 2.5 s。
  `CAMERA_NETWORK_DEFAULT=remote_first` 实测确实走远程（日志"通道=远程中继"），
  排除了"其实走了局域网导致慢"的可能。
- **要再追平只有改链路层**：把 go2rtc 的 xiaomi producer 前置拨号（登录态时就建好
  producer、保持关键帧缓冲），或去掉本地 RTSP 一层直接把 TS 喂播放器。两者都是
  协议/播放栈改造，**不是参数能补的**，本轮不做，留待用户拍板。

#### 6.1.3 两条"不改播放栈"的提速路：实测都否掉（2026-11-28 凌晨）

用户拍板"尝试改动"后，按不引入 Rust/WebView2 的前提试了两条，都**有实测数据**、都否掉：

1. **go2rtc 原生 `/api/preload` 前置拨号**（隐藏消费者让 producer 提前起来）。
   源码证据：`internal/streams/preload.go` + `AddConsumer` 会触发 `prod.Dial()`，
   且 preload 消费者和真实播放器共享同一条 producer，**不额外占观看位**。
   A/B 实测（同一台 069a01、同样冷流、各 3 轮）：
   冷流 **2.44 s**（2.20–2.81）vs preload 预热 **2.35 s**（2.35–2.36），只快 0.09 s。
   根因：`pkg/core/track.go` 的 `Receiver`/`Sender` **不缓存关键帧**给新加入的
   消费者——producer 热着也没用，播放器连上仍要等下一个 GOP。实验脚本已随
   失败方案挪走，结论留此。
2. **改 keyframe 补丁：probe 读走 IDR 时缓存下来，`Producer.Start()` 先回放**。
   下载 Go 1.27.3 工具链、改 `pkg/xiaomi/miss/producer.go`（probe 多返一个首包、
   `writePacket` 抽出共用、Start 先回放）、编出测试版跑同样冷启动 A/B：
   新补丁 **5.20 s**（4.50–6.42）vs 现有正式版 **1.70 s**（1.00–2.88）——**更慢**。
   原因：回放的 IDR 带 probe 时的旧 RTP 时间戳，与后续实时包时序不连续，
   QMediaPlayer/FFmpeg 反而要多等一次重新同步。原补丁"StopMedia → 150 ms →
   StartMedia 让摄像头重发一个时间戳正确的新 IDR"是对的做法，别改成回放。
   测试版二进制与改动源码已删除，补丁文件与 `build_go2rtc.py` 均**还原原样**
   （`git status` 干净）。

**结论**：在不换播放栈（Qt `QMediaPlayer` + 本地 RTSP）的前提下，首帧 2 s 这段
**没有低成本解法**——已排除改起流时机、preload 预热、回放旧 IDR 三条。要再压只能
上"TS 直喂播放器（MSE/自定义解码）"，那是 §4.2 已经判定的整套替换级工程，
没有产品层面的新理由（SD 回放 / 对讲 / 双镜头）就不启动。

#### 6.1.4 2026-11-28 深夜续：拿掉云端换票那 0.7–1.0 s

按 §6.1.3 的结论（不换播放栈的前提下没有低成本解法）又往前推了一步：给自建 go2rtc 加
`go2rtc-vendor-cache.patch`（换票缓存 + 落盘 + 启动后后台预热），并让程序启动就把
go2rtc 拉起来。**实测首帧 3.45 s → 2.61 s**；`probe` 前缀补发那条路再次实测并彻底封死
（热态退化到 4.6–5.8 s，原因是补发的是上一轮残留的 P 帧）。
**剩下的 0.6–1.0 s 在 Qt 的 FFmpeg 播放器里**，与本文 §4.2"整套替换"的结论一致。
逐段数据见 `docs/reference/camera-first-frame.md`。

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
