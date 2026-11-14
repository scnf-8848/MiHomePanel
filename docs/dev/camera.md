# 摄像头画面

> 专题文档，入口见 [DEVELOPMENT.md](../DEVELOPMENT.md)。
> 本文覆盖：go2rtc 取流、首帧耗时拆解、可选补丁构建、独立窗口、声音、云台、已知边界。

## 摄像头画面

米家摄像头没有 `stream_address`，码流走 TUTK/MISS 私有 P2P（UDP 打洞 + 帧加密），
厂商 SDK 有商业授权、不能进 GPL 发行包。2026-11-07 四轮调研的结论：账号里两台摄像头
都没有 `stream_address`，实时画面只能走 P2P；`miss_get_vendor` 已用本项目凭据实测拿到
`vendor=4`（CS2）的全套取流授权。所以这块**不自研协议栈**，把取流交给可选的
外部进程 [go2rtc](https://github.com/AlexxIT/go2rtc)（MIT）：

1. `core/camera.py` 用登录凭据（`userId` + `passToken`）生成一份只监听 `127.0.0.1`
   的配置，流源是 `xiaomi://<uid>:CN@<localip>?did=…&model=…&channel=0&subtype=0`
2. 起 go2rtc 子进程（端口从 1984 / 8554 往后找空位），等 `/api` 就绪
3. 详情页拿到 `rtsp://127.0.0.1:<port>/cam_<did>`，用 `QMediaPlayer` + `QVideoSink`
   解码，自绘控件按 cover 裁切画出来（圆角、加载态、错误态都在这一层）

### 默认不用显卡解码：显存一紧就会崩，而且接不住

Qt 的 ffmpeg 后端在 Windows 上**默认走 D3D11 硬件解码**，交给应用的帧是从显存纹理
转出来的 `NV12`。显存被别的程序（游戏）吃满时，实测两种后果都不是我们能处理的：

- 轻的：`AVHWFramesContext: Could not create the texture (8007000e)` ——
  `0x8007000e` 就是 `E_OUTOFMEMORY`，纹理建不出来，**一帧都收不到**（当时那次
  `verify_camera` 直接 18/27）
- 重的：`QVideoFrame::toImage()` 里 **access violation**。这一步就是把 GPU 帧回读成
  CPU 图像，失败时 Qt 不是返回空图而是踩空指针，Python 层连异常都看不到，
  整个面板进程直接消失（`exit=139`）

所以 `ui/camera_view.py` 在**导入期**就把硬解关掉（`QT_FFMPEG_DECODING_HW_DEVICE_TYPES
=none`，早于任何 `QMediaPlayer` 创建），帧改从内存里的 `YUV420P` 来，整条链路不碰显存。

代价实测很小，同一路 2960×1666 HEVC、同一台机器：

| | 帧格式 | 帧率 | 进程 CPU（14 秒） |
|---|---|---|---|
| 硬解（Qt 默认） | `NV12` | 20.0 fps | 4.0 s |
| 软解（现在的默认） | `YUV420P` | 19.9 fps | 5.7 s |

帧率没差别，多花约单核 12%。想换回硬解（例如 CPU 很弱、或不打游戏）设
`MIHOME_CAMERA_HW_DECODE=1`，或在环境里自己指定 `QT_FFMPEG_DECODING_HW_DEVICE_TYPES`。

定位过程留个记录，省得下次重新查：`python -X faulthandler` 拿到崩溃时的 Python 栈
（指向 `camera_view.py` 的 `toImage()`）→ 崩溃日志里翻到 `8007000e` → 对比开关硬解
前后帧的 `pixelFormat()`（`NV12` vs `YUV420P`）。排除了两个干扰项：占 10GB 内存 +
8 路 CPU 满载各跑一轮都不复现，说明不是单纯的内存/CPU 压力，而是**显存**。

### 打开页面不取流，点播放才连

首帧要 4.8–6.3 秒（官方 go2rtc 构建、8 次实测；装了开播补丁约 2 秒，见下），自动播等于让
用户盯着黑屏等。所以打开详情页**不建立 RTSP 会话**：只在后台把 go2rtc 和本机地址
准备好（实测 0.85 s，不占观看位、不推流），画面区停在一个播放键上。用户点下去才连
RTSP，加载态只写一句「加载中…」加一条流动的细条——**不写秒数、也不做百分比**。
链路上没有一处能给出可靠的剩余量（等 IDR 的相位完全随机），编个数字只会在某一格
上停住，看着比不写更让人以为卡死。

控件状态机在 `ui/camera_view.py`：`idle → loading → playing ⇄ paused`，另有
`released`（暂停闲置放掉连接）和 `error`。中央只在空闲/暂停/释放时放大播放键（主操作，
大一号）；右下角一排小键**共用一条规则**：有画面（播放 / 暂停）且鼠标在画面上才整排
出现，移开整排消失。首帧之前的 `QMediaPlayer` 报错会自动重连一次，不弹重试给用户。

暂停不是断开：画面停在当前帧，再点立刻续上，不会重新打洞。暂停时设备仍在推流
（实测 ~170 KB/s），所以闲置 3 分钟会主动放掉连接，把观看位腾出来。

### 首帧为什么要 5–7 秒：实测拆解

不是 P2P 打洞慢，局域网下打洞只要 0.3 秒。用
`scripts/measure_camera_startup.py` 把整条链路拆开（Xiaomi Smart Camera 3 / 069a01，
2960×1666 HEVC，局域网）：

| 阶段 | 官方构建 | 开播补丁版 | 能不能省 |
|---|---|---|---|
| go2rtc 子进程启动到 `/api` 就绪 | 0.85 s | 0.85 s | 打开详情页就在后台起，点播放时不用等 |
| 云端 `miss_get_vendor` 换票 | 0.65 s | 0.65 s | **go2rtc 每开一路都要调一次**，可以自己预取后并行 |
| MISS 握手 + `probe` 到首个 RTP 包 | 0.55 s | 0.75 s | 补丁多一次 video start，多 0.2 s |
| **等下一个 IDR** | **2.5–3.0 s** | **0** | 补丁把它消掉了，见下 |
| 177–251 KB 的 IDR 传完 + 解码出帧 | ~1.0 s | ~0.7 s | 换子码流能小一截，但画质就没了 |
| **点播放 → 出画面（实测 8 次）** | **4.8–6.3 s** | **1.85–3.15 s** | |

关键在第四行：**摄像头每 3.05 秒才出一个 IDR**（实测 `[5.25, 8.29, 11.29]`，间隔
3.04 / 3.00），而**会话起点那个 IDR 到不了消费者手里**——`pkg/xiaomi/miss/producer.go`
的 `Dial()` 是「先 `StartMedia()` 再 `probe()`」，probe 为了拿 VPS/SPS/PPS 会把开播那一包
读走，而 IDR 切片和参数集恰好在同一个 access unit 里。于是收到的最初 60 帧 POC 从 0 开始
却全是非关键帧，ffmpeg 只能一路报 `Could not find ref with POC 0/1/2…`，直到下一个 IDR
才解得出画面。也就是说**每次打开都要完整等一个 GOP**，而且随相位抖动（4.8–6.3 秒）。

不是「谁先连谁独占」的错觉：让一路先占住生产者，后面接进来的第二、三、四路只要等
**0.94 / 1.17 / 1.46 秒**（随机相位），只有**会话的第一路**固定等满 3 秒。

所以米家 App「感觉秒开」不是因为它连得快，而是它**立刻显示上次的静止画面**，再在背后
慢慢地出实时流。我们不垫旧画面（见下），那 3 秒只能从链路本身消掉——**已经消掉了**：
`scripts/patches/go2rtc-keyframe.patch` 在 probe 之后把流停掉、150 ms 后重起一次
（摄像头每次 video start 都从 IDR 开头），消费者第一包就能解码，补丁只有 12 行。

### 开播补丁（可选，首帧 2 秒）

`scripts/build_go2rtc.py` 自己编一版带补丁的 go2rtc：

```bash
.venv\Scripts\python.exe scripts\build_go2rtc.py
```

这一版同时带**云台接口**（见下一节「云台」）。两个补丁是独立的：官方构建两个都没有，
只打第一个就只有首帧优化。

- 取上游快照 `990f4de`（验证过的那个）→ `git apply` 两个补丁
  （`go2rtc-keyframe.patch` 开播、`go2rtc-ptz.patch` 云台）
  → 用 Go 1.27.3 编译（本机没有 Go 会自动下便携版到 `<data>/go/`，76 MB，只下一次）
  → 装到 `<data>/go2rtc/go2rtc.exe`
- `git` 先找 PATH；找不到再自动用 GitHub Desktop 自带的
  `%LOCALAPPDATA%\GitHubDesktop\app-*\resources\app\git\cmd\git.exe`，
  没装独立 Git 的机器也能构建
- 构建环境固定 `CGO_ENABLED=0`，产物是不依赖本机 C 运行库的静态二进制；
  其余参数仍是 `-trimpath -ldflags=-s -w`
- 装之前把原来的官方构建留成 `go2rtc-official.exe`；**想回退把它改名回 `go2rtc.exe` 即可**，
  两个脚本互不干扰
- 实测 8 次：1.85 / 1.87 / 1.89 / 2.15 / 2.16 / 2.25 / 2.64 / 3.15 秒，均值 2.18；
  远程（relay）路径同样受益，端到端 2.86–3.07 秒（多出来的是 relay 握手那 0.9 秒）
- 2026-11-13 用 Go 1.27.3 + `CGO_ENABLED=0` 重打后复测：直接量 RTSP
  2.20 / 2.56 / 3.47 秒（均值 2.74），详情页端到端 2.09 / 3.51 秒（均值 2.80）；
  仍远低于官方 4.8–6.3 秒，云台 `op=1..4` 画面差异 36.1–56.5%
- 代价：**这是一个自建二进制**。上游改了这段代码补丁会打不上，脚本会明确报错并提示手工改；
  想跟进新版本得重跑一遍（补丁与 `producer.go` 的 `Dial()` 一一对应，改动面很小）。
  上游哪天自己修了「probe 吃掉开播 IDR」，删掉补丁和这个脚本即可
- 试过但**不行**的方案记在这里免得重走：把 probe 读到的包缓存起来在 `Start()` 里补发
  （`Producer.cache`）。裸流里首包确实变成 IDR，但 Qt 端到端只到 4.7 秒且时灵时不灵——
  补发是在 RTSP 还没 PLAY 完时写的，和建流竞争；而且老包的时间戳/POC 落后于追上来的活流，
  ffmpeg 会直接跳过它继续等下一个 IDR

#### 补丁版 Release 与 `install_go2rtc.py --patched`

普通用户不必装 Go / git，直接：

```bash
.venv\Scripts\python.exe scripts\install_go2rtc.py --patched
```

- tag **不写死**，由 [build_go2rtc.py](../../scripts/build_go2rtc.py) 的 `REF` 推导成
  `go2rtc-<ref>`（当前 `go2rtc-990f4de`）；安装脚本请求
  `/repos/scnf-8848/MiHomePanel/releases/tags/<tag>`，**不查 `latest`**，避免程序
  自身的 Release 顶掉组件 Release
- 资产名：`go2rtc_<平台>_<架构>_patched.zip`，例如 Windows x64 是
  `go2rtc_win64_patched.zip`，macOS 是 `go2rtc_mac_arm64_patched.zip`，Linux 是
  `go2rtc_linux_amd64_patched.zip`。zip 内**平铺**三件：
  `go2rtc.exe`（非 Windows 为 `go2rtc`）、`BUILD.txt`、`LICENSE`；可再带
  `NOTICE.txt` 说明补丁
- 安装到 `<data>/go2rtc/`；`BUILD.txt` 的 `ref=` 必须等于当前 `REF`，
  Release 若带 `SHA256SUMS` 还会核对 zip 的 sha256
- 失败路径（Release 404、GitHub API 403/限流、断网、资产缺失、ref 不符、哈希不符）
  都会打印中文原因并回退：本地已有 go2rtc 就原样保留（并说明是官方版还是哪个 ref），
  没有就尝试装官方版；官方也装不上时给出目录和手动命令，不静默失败

**发布组件 Release（手动，仓库目前没有 GitHub Actions）**：

```powershell
# 1. 编译到独立的发布目录（--out 不写 BUILD.txt，别用）
.venv\Scripts\python.exe scripts\build_go2rtc.py --dir dist-go2rtc --keep
dist-go2rtc\go2rtc.exe -v

# 2. 验证首帧 / 云台（需要登录态和一台在线摄像头）
.venv\Scripts\python.exe -X utf8 scripts\measure_camera_coldstart.py --binary dist-go2rtc\go2rtc.exe
.venv\Scripts\python.exe -X utf8 scripts\measure_app_play.py --lane lan --rounds 3
.venv\Scripts\python.exe -X utf8 scripts\probe_ptz.py

# 3. 组包：exe + BUILD.txt + go2rtc 的 MIT 原文
New-Item -ItemType Directory -Force dist-go2rtc\release | Out-Null
Invoke-WebRequest "https://raw.githubusercontent.com/AlexxIT/go2rtc/990f4de/LICENSE" `
    -OutFile dist-go2rtc\LICENSE
Compress-Archive -Path dist-go2rtc\go2rtc.exe,dist-go2rtc\BUILD.txt,dist-go2rtc\LICENSE `
                 -DestinationPath dist-go2rtc\release\go2rtc_win64_patched.zip
Get-FileHash dist-go2rtc\release\go2rtc_win64_patched.zip -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash.ToLower())  go2rtc_win64_patched.zip" } |
    Set-Content dist-go2rtc\release\SHA256SUMS -Encoding ascii

# 4. 发组件 Release（不是程序版本）
gh release create go2rtc-990f4de dist-go2rtc\release\* `
  --title "go2rtc 990f4de + keyframe/PTZ patches" --notes-file dist-go2rtc\release\RELEASE.md
```

已发布（2026-11-13）：<https://github.com/scnf-8848/MiHomePanel/releases/tag/go2rtc-990f4de>。
两个资产为 `go2rtc_win64_patched.zip`（8,460,003 字节，
sha256 `5a14a3af074292021412012c373178a6280ce8e6ac91120378dfd876dd56e232`）与
`SHA256SUMS`（92 字节）；匿名 API、匿名直链下载和哈希都已核对。

> GitHub 匿名 API 是共享的 60 次/小时额度，`--patched` 查询 Release 时可能遇到 403。
> 这是限流不是 Release 缺失：稍后重试即可；脚本会明确提示并回退官方版 / 保留本地副本。

`RELEASE.md` 至少写：上游 `AlexxIT/go2rtc@990f4de`（2026-11-05，晚于 v1.9.15）、
Go 1.27.3、两个补丁各自作用、构建日期、实测首帧区间、安装命令
（`install_go2rtc.py --patched`）、MIT 许可提示，以及「组件 Release，不是 MiHomePanel
程序版本」。`BUILD.txt` 必须和 exe 同层：`build_exe.py` 靠它判断是不是补丁版，
`write_notice()` 也靠它把构建信息写进 `NOTICE.txt`。

**上游升级后重新对齐**：

1. 选新 tag / commit，先 `build_go2rtc.py --ref <sha> --keep` 试编；
2. `git apply` 失败说明上下文变了：在 go2rtc 源码里手工落改动，再用
   `git diff -- <文件>` 重新生成 `scripts/patches/` 下的两个补丁；
3. 确认 `PATCH_MARKS`（`StopMedia()`、`func (c *Client) Motor(`）仍命中；
4. 重跑首帧与云台验证，通过后改 `REF`，再发一个 `go2rtc-<新 ref>` Release；
5. 上游若自己实现了某项，删掉对应补丁和 mark，程序会按能力探测自动退化。

### 为什么不再垫上次的画面

试过把每台摄像头最后一帧缩到 960 宽存成 JPEG（`ui/snapshots.py`、`<data>/snapshots/`）
垫在画面区，让加载"看起来"秒开。但旧画面和实时的门口状态对不上，容易误导；既然改成
用户主动点播放，加载态如实说"在连"就够了（一行字 + 一条流动的细条）。这套缓存已随
`ui/snapshots.py` 一起删掉。

如果确实需要更快，只能从链路本身下手：`MIHOME_CAMERA_SUBTYPE=sd` 换子码流（IDR 从
~177 KB 降到 ~21 KB，画质明显下降，但**GOP 不会变短**），或者装 ffmpeg 让 go2rtc 转码
成短 GOP 的 H.264（依赖多 80 MB、还要吃 CPU）。

「闲置释放」那一档踩过同一个坑的变种：释放后本该是黑底 + 「闲置 N 分钟，连接已释放」。
实际却是一张**静止的旧画面**加一个播放键、一句解释都没有——先是因为详情页拿
`playbacks().release()` 收尾，而那个接口是给卡片墙用的硬重置（`stop()` +
`release_player()` → 回到空态），刚摆好的释放态被抹掉了；接着拆会话期间漏出来的
尾帧又落在空态底下（`_on_frame` 当时只挡 `paused` / `stopped`）。现在释放分支只放
所有权和 go2rtc 会话、控件外观留给自己，`_on_frame` 也改成只在 `loading` / `playing`
收帧，其余状态一律丢弃。

**没装 go2rtc 时不会静默失败**：画面区域直接写「没有找到 go2rtc」，并给出安装命令。
只装一次（7.7MB，MIT）——**官方构建，首帧 4.8–6.3 秒**：

```bash
.venv\Scripts\python.exe scripts\install_go2rtc.py
```

`install_go2rtc.py` 走 GitHub 的 **assets API**，不是
`releases/download`——后者 302 到 `objects.githubusercontent.com`，本机网络下
`github.com` 和几个常见镜像都不通，只有 `api.github.com` 可用。
（`build_go2rtc.py` 取源码用的也是 `api.github.com` 的 zipball；这条不能带
`Accept: application/octet-stream`，会回 415。）

**查找顺序**（`core/camera.py::find_binary`）：显式指定 → `MIHOME_GO2RTC` →
**程序目录**（`go2rtc/`、`tools/go2rtc/`）→ 数据目录 → `PATH`。程序目录排在前面是
为了打包版：把 go2rtc 和 exe 装在一起，它跟着程序版本走，是默认项；数据目录那份
是用户自己下载/编译的，要压过它就用环境变量。打包后 `__file__` 落在 `_internal`
里，`parents[2]` 算回仓库根这条会失效，所以 `program_dirs()` 额外加了
`sys.executable` 所在目录和 `sys._MEIPASS` 两个候选（PyInstaller onedir 把数据放
`_internal`，Nuitka 放在 exe 旁边，两者覆盖）。`verify_camera.py` 第 [13] 项会伪造
一个 frozen 布局守住这条。

**打包成 exe 时**：go2rtc 是外部进程，不会被自动收集，得摆到安装目录的
`go2rtc/go2rtc.exe`（`scripts/build_exe.py` 已经做了）。想让别人也拿到 2 秒，就得
**发补丁后的那一份**（MIT 允许再分发，但要把它的许可证一并带上，脚本会取；致谢表里
也如实登记了这处改动）；只发官方构建的话，别人装完是 4.8–6.3 秒——功能完整，只是慢。
缺件时的提示会区分两种情况：源码运行让你跑 `install_go2rtc.py`，打包版直接告诉你该
放到哪个路径（打包版没有 `scripts/` 目录）。

### 独立窗口

详情页播放中，画面右下角有一个「独立窗口」键：把同一块画面控件弹到一个约 62% 屏宽、
可四边（含右下角）缩放的无边框窗口。切换只做 `setParent` 和尺寸策略切换，
`QMediaPlayer` / `QVideoSink` / RTSP 会话都不动，所以不重连、画面不闪断；关掉独立窗口
画面回到详情页原位。观看位仍按详情页算——独立窗口不算第二路。

### 卡片 ↔ 详情页：画面跟着走，关掉再还回来

同一台摄像头在三个承载面（墙面卡片 / 详情页 / 独立窗口）之间搬，靠
`PlaybackOwnership` 单路仲裁：`claim` 开新会话、`move` 换承载面（只搬
`QMediaPlayer` / `QVideoSink` / `QAudioOutput`，不碰 RTSP）、`release` 真结束。

**去程**：双击卡片 → `wall.hand_off_active_to(dlg.camera_view, did)` →
`playbacks().move(target, "detail")`。源卡片进 `MOVED`，画面上写「画面已在其他窗口中
播放 / 关掉那个窗口即可回到这里」；墙上不再有"活动卡"，滚出视野也不会误停这条会话。

**回程**：详情页关窗时发 `about_to_stop(did)`，`MainWindow` 接到
`wall.adopt_back(did)`——卡片**还看得见**（`_card_on_screen`：可见、没被搜索过滤、
与视口相交）就 `move(card.view, "wall")` 收回去，同一个播放器、帧号连续、声音状态不变；
看不见就不收，交给 `release` 结束，绝不把一条看不见的会话留在墙上占观看位。
收回之后要调 `WallCameraController.take_back()` 重新登记成活动卡，否则"滚出视野就停"
不再管它。

两道保险，缺一条就会重现 2026-11-10 实测的那个 bug（关掉详情页，卡片永久停在提示）：

1. **会话结束要通知**。`PlaybackOwnership.on_finish` 在 `release` / `forget` 之后回调，
   卡片墙据此把还停在 `MOVED` 的卡片 `show_idle()`。MOVED 的卡片已经不是"活动卡"，
   没有任何状态推送会叫醒它，`CameraCard.refresh` 只在 store 推新状态时才跑。
2. **易主要放手**。`closeEvent` 与 `done` 各调一次 `_stop_camera`，第一次已经把会话
   还回去了，第二次看到 `_session_taken_by_other`（所有者是同 did 的另一个承载面）
   就只清本页界面直接 return。少了这一步，兜底的 `camera_service().release(did)`
   会把刚还回墙上的画面掐掉。

一个例外：详情页已经关掉的**独立窗口**再关，等于"用户不想看了"——直接结束会话，
卡片回到空闲等下一次点播放，不再还回去（要还也得重新取流，不符合"关窗就是不想看"）。

验证：`verify_camera_handback.py`（离屏，还回去 / 结束通知 / 易主判定）、
`verify_camera_card_return.py`（真机端到端：卡片播 → 双击进详情 → 关掉 → 卡片继续播）。

### 声音：go2rtc 一直在取音轨，Qt 只是没接输出设备

`xiaomi://` 的 `audio` 参数是**真参数**（`pkg/xiaomi/miss/client.go` 的 `StartMedia`
直接拼进 MISS 的 `enableaudio`），**不写默认 "1"**。所以以前局域网分支没写它——
音轨其实一直在流里，只是 `QMediaPlayer` 没配 `QAudioOutput`，Qt6 的语义就是"无声"。
跨网分支当时写死了 `&audio=0`，纯粹是因为没人听、省一点带宽。

实测（`chuangmi.camera.069a01`，局域网，`scripts/probe_camera_audio.py`）：

| 事实 | 数据 |
|---|---|
| 音轨格式 | `audio, recvonly, OPUS/48000/2`，Qt 解出来是 `48000Hz 2ch Float` |
| 能不能解 | 一路 8 秒收到 **121 个**音频 buffer（`QAudioBufferOutput` 为证） |
| `hasAudio()` 何时为真 | `LoadingMedia=False → LoadedMedia=True`，**必须挂 `mediaStatusChanged`** |
| 开音频拖不拖首帧 | 3.96s vs 3.92s，噪声级差异（`probe()` 是视频一到就返回） |
| 关掉音轨（`audio=0`） | 轨道只剩 `video, recvonly, H265`，`hasAudio()` 全程 False |

同一个 medias 里还会有一条 `audio, sendonly, OPUS/48000/2`：那是 go2rtc 默认挂上的
**对讲（backchannel）声明**，不是这台摄像头在发音频。本程序不碰它（只收），看到它
不必以为是 bug。

于是实现只有三处：

- `_ensure_player`：建 `QAudioOutput`，音量固定 1.0，`player.setAudioOutput()`
- 喇叭键：`_RoundButton` 新增喇叭图标（静音加一道斜线），显隐看 `hasAudio()`
- 禁音：`audio_out.setMuted()`，**不重建流**——关掉音轨要重建 producer、重新等一个
  GOP，点一下喇叭要等 4 秒就太蠢了

几个刻意的选择：

- **每次开播都从静音开始，而且不落盘**。点播放是为了看画面，不该先把家里/店里的
  声音放到当前环境。声音状态只属于**这一次观看**：`play()` 里重置成静音，而暂停/继续
  （`toggle`）与换承载面（`take_playback` / `adopt_playback`）都不经过 `play()`，所以
  它们保持原状。曾经把它写进偏好（`prefs.camera_muted`），用户实测后否掉了——"昨天
  点过一次喇叭"不该让今天打开就出声，重启、换设备、隔天都一样。偏好里的这个键随
  v8 一起删掉，老文件里读到直接忽略
- **喇叭键在卡片墙也有**。2×2 实时画面卡同样会出声，不给关的地方就只能去关系统音量
- **右下角整排一条显隐规则**，不给任何键开例外：`hover and state in (playing, paused)`。
  曾经让静音时的喇叭常驻、独立窗口键在有画面时常驻，结果是同一排里三个键各有各的
  出现条件——用户学到「这里的键悬停才出」，然后发现有一个不遵守，就会在别处找不到暂停；
  常驻的键还一直挡着画面。整排一起进、一起退，位置靠 `_place_buttons` 按「这套键里会
  出现哪些」算好，所以显隐不会让剩下的键横跳。键内再按语义各让一步：暂停键只在
  `playing` 出现（已暂停时中央已经是播放键），喇叭只在真有音轨时出现
- **三个小键同尺寸**（`_SIDE_BTN` / `_SIDE_BTN_COMPACT`，30 与 34）。26/30/34 混排会
  让人觉得它们权重不同，也让这一排像随手摆的；中央播放键是主操作，才故意大一号
- **`take_playback` 把 `audio_out` 一起搬**。它是独立的 QObject，不搬的话声音留在旧控件
  上，新控件按静音键改的却是自己那个没人用的设备
- 没有音轨（设备没麦克风 / `MIHOME_CAMERA_AUDIO=0`）时按钮直接不出现，不做一个
  点了没反应的按钮

**跨网音频未实测**：relay 是纯 UDP 转发（不解析负载），音频与视频走同一条通道，所以
理论上能过来；但只有离开局域网才能验。出问题时先用 `MIHOME_CAMERA_AUDIO=0` 把音轨
整条关掉，再判断是 relay 的问题还是解码的问题。

不做音量滑杆、不做对讲（go2rtc 有 `backchannel.go`，是另一件事：要麦克风权限与回声
消除）。

### 云台：规格里没有 ptz 服务，只能走 MISS 的 `0x112`

这台 `chuangmi.camera.069a01`（小米智能摄像机3 云台版）在米家 App 里能转，但
**MIoT 规格里没有任何 ptz / motor 服务**——`miotspec/spec` 列出来的服务只有摄像头、
存储卡、指示灯这些，也没有能拼出来的 action。云台指令走的是**取流那条 MISS 连接**
上的命令帧：`0x112`（上游常量 `cmdMotorReq`），载荷 `{"operation":N}`，
`N` = 1 左 / 2 右 / 3 上 / 4 下 / 5 校准。

上游 go2rtc 只定义了这个常量，没实现、也没有 API 入口。所以
`scripts/patches/go2rtc-ptz.patch` 补三样东西：

1. `Client.Motor(op)`：写一帧 `0x112`。**不等回包**——回包（`0x113` 角度）在
   读循环里会被当成媒体包读走，等它只会把发指令的时间拉长
2. **命令通道常驻读者** `drainCommands()`：CS2 的命令队列只有 **10 格**缓冲，
   没人读的话设备每回一条就少一格。连发十几条云台指令就会把队列撑满，worker
   直接关掉连接——表现是**取流突然中断**，和"观看位满了"长得一样，极难归因。
   所以登录完就起一个常驻读者把回包丢掉；取流走另一个通道，不受影响
3. `GET /api/xiaomi/ptz?name=<流名>&op=<1-5>`：**不带参数就是能力探测**，回
   `{"ok":true,"ptz":true}`；没打补丁的官方构建没有这条路由，直接 404

`scripts/build_go2rtc.py` 现在一次打两个补丁（开播 + 云台），装到
`<data>/go2rtc/go2rtc.exe`；两个补丁各自核对落点，哪个打不上都会明确报错并停下。

程序侧（`core/camera.py`）：

- `ptz_supported()` —— 只缓存**构建层面**的结论：404 记一次 False 就不再问；
  连不上（go2rtc 刚重启、端口还没起）**不缓存**，下次进页面再问
- `ptz(did, op)` —— 打本机 API，超时 2 秒。被拒（`ok:false`）不是异常：没在
  播放时那条流没有 producer，go2rtc 回的是 `stream has no ptz producer`，属于预期
- **官方构建整块退化**：路由 404 → `ptz_supported()` 为 False → 详情页连方向盘
  都不建。不给一个点了没反应的灰圆

界面（`ui/ptz_pad.py`）：画面下方一个圆，切成四块（中心孔不响应，圆外不响应），
按下哪块就往哪边转。

- **点一下 = 一条指令**。`HOLD_MS = 450` 之内松开只发按下那一条；超过它才开始连转，
  之后每 **220 ms**（`INTERVAL_MS`）补一条。两段必须分开：只有一条 220ms 节拍时，
  一次普通点击（按下到松开常常 300 ms 上下）会顺手多发一条，观感就是"点一下转两格"
- **上一条没回来不补下一条**（`_inflight`）。网络一堵就积压一串指令、松手了还在转，
  是这类控件最容易踩的坑；代价是慢网络下转速会自己降下来，这是想要的
- 松手 / 移出控件 / 关页面都停；`MAX_HOLD_S = 12` 秒兜底（收到松开事件才算停，
  收不到就最多转 12 秒）
- 按住拖到另一个方向直接换向，不用松开重按
- 可点性由承载面的 `_sync_ptz` 决定：LOADING / PLAYING / PAUSED 可点，
  IDLE / ERROR / RELEASED 置灰（没画面时发出去也只会被拒）
- **与画面的间隔 = 卡片下边距（14 px）**：圆盘外面包一层 `_ptz_host`，上边距 10，
  加上布局 spacing 4 正好 14。不能直接往布局里塞 `addSpacing`——官方构建里圆盘不建，
  那段空白会留在卡片底部；容器整体显隐，间隔跟着一起消失

**两种形态**（`PtzPad(..., overlay=)`），同一套交互、两套画法：

| | 详情页 | 独立窗口（`overlay=True`） |
|---|---|---|
| 直径 | 132（`SIZE`） | 104（`OVERLAY_SIZE`） |
| 位置 | 进布局，排在画面下方 | **叠在画面上**，贴左下角（`PTZ_MARGIN = 12`） |
| 底盘 | `bg_soft` + 1px 描边（跟着主题走） | 黑色半透明 `OVERLAY_ALPHA = 150`，悬停 +45 |
| 图形 | 主题文字色 / 强调色高亮 | 白色箭头 + 白色扇区高亮 |

- **四角必须真的透出去**：overlay 形态要 `WA_TranslucentBackground`，否则会在画面
  左下角糊出一块方底。离屏验证直接读像素（角上 alpha = 0）
- 为什么不是"和右下角那排圆钮一样的 110"：96 px + alpha 110 在真机上白色箭头会糊进
  白墙里（`scripts/out/camera_popout_ptz.png` 核对过），104 + 150 是"小而不糊"的下限
- 放**左下角**是因为右下角要留给暂停 / 喇叭 / 收回那排键；`_place_ptz` 用
  `_view.geometry()` 而不是窗口几何（上面还有一条标题栏），`resizeEvent` 后重排
- 显隐由 `CameraPopoutWindow._sync_ptz` 管：不支持（官方构建）或没画面就不露；
  关窗 / 收回详情页要 `stop()`，别让连转的定时器继续往摄像头发指令
- 探测走后台线程（`_probe_ptz`）+ `Signal` 回 UI：`ptz_supported()` 是一条 HTTP，
  没命中缓存时最长 0.8 s，开窗不能为了一个可能根本不存在的控件卡住

真机实测（2026-11-11）：

- **单条指令转多少是固件定的**：画面位移约 **10–14%**（`scripts/probe_ptz_step.py`，
  用列灰度剖面的 SAD 估水平位移），从静止起步那一条最大、之后每条约 7–8%。所以
  "点一下够不够"取决于点了几条，界面这一侧的杠杆只有"别多发"
- **固件不接受步长参数**：`{"operation":1}` 加 `step` / `duration` / `speed` / `time`
  试过四种，位移全部与基准一致（13.9% vs 14.2%）；`operation` 只有 1–5 有效，
  6–10 与 0 都无动作。**不要再去猜参数**，想改只能改交互
- 四条指令逐条下发，前后帧差异 74.6% / 74.2% / 33.5% / 35.3%（`scripts/probe_ptz.py`，
  截图在 `scripts/out/ptz_*.png`）
- 详情页端到端：方向盘排在画面下方（画面底 433 / 盘顶 448，上下两个间隔 15 / 14）、
  按住 0.75 秒只发 3 条（0 / 450 / 670 ms）全部 `ok:true`、松手后不再发
  （`scripts/verify_camera_ptz_ui.py`）
- 上下方向的画面变化比左右小（33%/35% 对 74%）：这台机器俯仰行程本来就短，
  不是指令没生效——判定"有没有转"要看成对发（左+右）之后是否回到原位
- 独立窗口端到端：方向盘在 `QRect(12, 404, 104, 104)`，画面 `QRect(0, 38, 760, 482)`
  （标题栏 38 px 被正确扣掉）；点一下只发一条、方向「上」、go2rtc 受理
  （`scripts/verify_camera_ptz_popout_ui.py`）

不做：预置位、巡航、跟随、变焦、画面拖动定位。校准（`op=5`）常量留在
`camera.PTZ_CALIBRATE` 里但界面没入口——误点会把画面拉回原点，不确定该放在哪。

### 已知边界

- 只做**实时画面 + 声音 + 云台方向**。画质切换、SD 卡回放、云存储录像已明确取消
  （理由见 TODO「摄像头二期」）
- 本程序对一台摄像头只开**一条** P2P 会话（多路观看复用 go2rtc 的 producer）；设备侧
  最多允许 **3 路并发**——实测第 4 路被设备直接掐断（go2rtc 日志 `miss: read media:
  cs2: EOF`），前三路不受影响。所以和米家 App 同看是可以的，占满 3 个观看位时后连的
  那一路才会掉
- **暂停不等于断开，也不等于让位**。实测暂停 4 秒内设备仍以 ~170 KB/s 向本机推流，
  控件只冻结在 1 帧：详情页开着（就绪 / 暂停 / 播放）就一直占着 3 个观看位之一，
  也一直走摄像头的上行带宽。离开详情页才会真正断开
- 所以**闲置 3 分钟会自动释放连接**（就绪和暂停都算闲置，播放不算）：画面变成
  「闲置 3 分钟，连接已释放」，点播放重新取流（局域网一次约 4.8–6.3 秒；远程另加
  2–4 秒握手）。阈值见
  `camera_view.IDLE_RELEASE_SECONDS`，测试可用 `MIHOME_CAMERA_IDLE_SECONDS` 调小
- **崩了不会一直占着位**。实测强杀程序后，设备侧的观看位在停止推流后立刻可用
  （新会话 0 秒内就能连上）；但 Windows 不会跟着结束子进程，会留一个 go2rtc 占住
  1984 / 8554。启动时会按 `<data>/go2rtc/runtime.json` 把它清掉——三样都对上才动手：
  pid 还活着、映像名是 `go2rtc.exe`、命令行里有本程序的配置路径，所以不会误杀用户
  自己跑的 go2rtc
- 流被掐断时能分辨原因：go2rtc 日志里 20 秒内出现 `cs2: EOF` / `read media` 就写
  「画面中断：观看位可能已满（最多 3 路）」，否则还是通用文案
- 点播放后才连 RTSP（打开页面只预启动 go2rtc，不建立会话、不占观看位），局域网首帧
  实测 **4.8–6.3 秒**（8 次实测，2960×1666 HEVC、官方构建）：约 0.3 秒打洞 +
  等一个完整 GOP（摄像头 3 秒才出一个 IDR）+ 解码
- **支持跨网**：同一局域网仍走直连（更快、更省摄像头上行带宽）；局域网不可达时自动
  回落到小米 relay，用户不用手动切。实现是 `core/relay.py` 的旁路代理——把 relay
  会话伪装成 `127.0.0.1` 上的"设备"交给 go2rtc，媒体仍是 MISS 加密负载，程序只搬
  传输层，没有改 go2rtc 源码
  - 直连失败（首帧前连不上 / 等不到 IDR）会自动切远程重试一次，画面区写
    「局域网直连不可用，正在尝试远程中继…」；只自动试一次，避免来回重连
  - 远程要先向云端要授权（`miss_get_vendor`），再做分阶段的无应答 UDP 握手
    （`LIST_REQ1` → `RLY_HELLO` → `RLY_PORT` → `RLY_REQ`），一次通常 2–4 秒，
    网络差时更久；加上等 GOP，**远程首帧通常在 6–10 秒**
  - 远程会话同样占一个观看位。只在真正播放时建立；关闭详情页或闲置释放会主动断开，
    不在后台占位。同一时刻只保留一条远程会话，切摄像头时先收旧再建新
  - 失败原因会分开写：没有局域网地址、relay 没有响应、relay 拒绝注册、
    relay 没分配端口、摄像头没有加入（多半是观看位已满或设备离线）、授权到期
  - **必须真实外网才能验证**：relay 的 `25000/UDP` 要能从当前网络出去，
    `api.mijia.tech` 要能访问。对称 NAT 下走 relay 是设计预期，但不同运营商 / 企业
    防火墙的 UDP 行为可能不同。代码只做了协议函数与离线路径校验，**没有在真实异地
    网络下回归过**
  - 设置 → 外观 → 摄像头 有三档：**「局域网优先」**（默认，失败回落远程）、
    **「远程优先」**（失败再试局域网）、**「仅远程」**（完全不试局域网）。
    异地常驻建议「远程优先」或「仅远程」，省掉一次注定失败的局域网等待
- go2rtc 自身仍然只有"往设备局域网 IP 的 32108/UDP 发 LAN search"这一条路
  （`pkg/xiaomi/miss/cs2/conn.go`），跨网完全靠上面那层旁路
- 画面是 2960×1666 的 HEVC，解码靠 Qt 的 FFmpeg 后端。控制台里那几类行都不是故障：
  - `qt.multimedia.playbackengine.codec: "No HW decoder found"` —— 我们主动关了硬解
    （见上文），Qt 例行提示一次。已在 `camera_view.py` 导入期把**这一个类别**关掉
  - `[av1_mf] / [h264_mf] / [hevc_mf] … could not find any MFT` —— ffmpeg 在枚举
    Windows 的 MediaFoundation 编解码器，4 行，一次性
  - `Could not find ref with POC` / `Skipping invalid undecodable NALU` —— 从 GOP 中间
    开始解码的必然结果，每次打开约 60 行，**出画即停**。它和「首帧等 3 秒」是同一个
    原因的两面：那 60 帧本来就解不出来
- 摄像头自带的时间水印会一起显示（那是画面的一部分，去不掉）
