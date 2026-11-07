# 米家控制台（MiHomePanel）

一个 Windows 桌面端的米家设备控制台。目标是**尽量贴近原版米家 App 的观感**，同时针对桌面
使用场景做取舍：横向空间用来放房间导航和更宽的卡片墙，而不是把手机界面等比放大。

> 非官方项目。通过社区逆向的米家 App 私有接口工作，账号凭据只保存在本机。

## 它长什么样

- 首页按**房间分组**的卡片墙，每张卡片是"名称 + 实时状态 + 产品实拍图 + 开关"
- 设备名是**中文**：请求语言钉死 `zh_CN`。不钉的话，中文 Windows 上
  `locale.getlocale()` 返回的是一个显示名（`Chinese (Simplified)_China`），服务端
  认不出就回退英文，卡片上全变成 "Xiaomi Smart Wall Switch (3 Gang)" 这类厂商默认名
- 多键面板**聚合成一张卡**，按键照实体开关的样子排：1 路是一整块，2–3 路并排成
  竖条（标签一个字一行竖排），4 路以上才退回胶囊网格——四根竖条只剩 28px 宽，
  竖排文字会被压成一条缝
- 顶部一排**场景快捷**，点一下执行
- **自绘标题栏**：没有 Windows 原生白条，最小化 / 最大化 / 关闭和拖动、双击、贴边缩放
  都是自己实现的，跟着主题走
- **家庭横向排列**在顶部（不是下拉菜单）。排列方式和激活样式都能在「设置 → 外观」里改：
  位置可选"保持设定顺序"或"激活的自动排最左"，激活态可选"字号变大 / 仅加粗变色 /
  胶囊底色"，设置页里带一个真控件的实时预览
- 左侧**房间导航**只列当前家庭的房间，房间多时自己滚动，最下面常驻「设置」
- **排序全可改**：家庭、房间、设备、场景都能在「设置 → 排序」里拖动，松手即生效
- **隐藏设备**：不常控制的网关 / 传感器 / 路由子设备可以收进每组末尾的「已隐藏设备」，
  卡片右键即可隐藏或恢复；「设置 → 隐藏设备」里是一列米家风格的开关（开 = 显示），
  隐藏不影响轮询和控制。这组**可以整块收起**（点标题行，只留一行「已隐藏设备 ▼ 21 台」），
  收起状态记在偏好里；其它房间的标题行不可折叠。组内再按房间分一层
  （「客厅 · 3 台」），同名设备不会混在一起认不出
- 顶栏**搜索**：按设备名 / 房间 / 品类 / 按键名过滤，`Ctrl+F` 聚焦、`Esc` 清空
- 房间标题右侧**不放批量按钮**：一个房间品类太杂（摄像头、路由器、净化器和灯混在一起），
  一刀切没有意义；要一键控制走顶部的场景栏，场景的语义是用户自己定的
- 点卡片进**详情页**：主控件在上、只读读数居中、其余可写属性收进"更多功能"；
  离线时控件整体禁用，底部常驻"数据更新于 N 秒前"，左下角可一键隐藏
- 「设置 → 账号」看登录状态、打码后的账号 ID、凭据文件位置，也能在这里**退出登录**
- **窗口圆角**：主窗口和三个子窗口都是 12px 圆角，最大化时自动切成直角
- 子窗口（详情 / 设置 / 登录）比主窗口**深/浅一档的底色 + 一圈描边 + 左侧主色窄边**，
  弹出时主窗口整体压暗——无边框窗口没有系统阴影，靠这几件事区分前后层
- 浅色 / 深色两套主题，可随时切换，窗口大小、主题、当前家庭都会记住
- 摄像头的详情页顶部是**实时画面**：经本机 `go2rtc` 把米家 P2P 码流转成 RTSP，
  Qt 自己解码渲染。进页面先**预热**（连上但画面冻住），点播放键才开始放，可随时暂停/继续

快捷键：`Ctrl+F` 搜索、`Esc` 退出搜索、`F5` / `Ctrl+R` 刷新设备列表、`Ctrl+,` 打开设置。

## 视觉参考

`docs/reference/米家App-UI风格参考.md` 是从真机（小米平板 + 米家 11.8.707）采集并测量出的
UI 规范：版式尺寸（dp）、6 列栅格、配色表、圆角与字号体系、各类组件规格，深色与浅色两套，
附原始截图与 UI 层级数据。后续界面调整以这份文档为准。

重新采集：`python scripts/grab_mihome.py grab <名字>`（需 USB 调试授权）。

## 和参考项目的关系

`MiHome-Windows-0.5.0/` 是起点参考项目，能力层写得扎实，本项目**沿用了它的三个核心判断**：

1. 所有网络调用进**单线程串行队列**（`requests.Session` 不是线程安全的，且上游自带节流）
2. 子线程**绝不直接回调 UI**，只把结果塞队列，主线程定时取走执行
3. 写入用**乐观 UI + 版本号**，避免"点一下绿了又弹回去"

界面层完全重写。参考项目是"一个规格属性 = 一个控件"堆在同一张面板上，所以每台设备
长得都一样；本项目建了一层**品类 → 卡片形态**映射，并按米家 UED 的分层原则
（高频在卡片、低频进详情、其余折叠）重排了信息。

## 数据从哪来

| 环节 | 来源 | 说明 |
|---|---|---|
| 登录 | `account.xiaomi.com` 扫码 | 凭据写到 `~/.config/mijia-api/auth.json`，与 mijiaAPI CLI 共用 |
| 家庭 / 房间 | `/v2/homeroom/gethome_merged` | 设备列表本身不带房间，只能靠 `roomlist[].dids` 反查 |
| 设备列表 | `/home/home_device_list` + 共享设备 | 云端下发的 `spec_type` 是一切能力的钥匙 |
| 设备能力 | `miot-spec.org` 原始 JSON | 自己解析，保留 `unit` / `format` / `access` / `comment` / `events` |
| 实时状态 | `/miotspec/prop/get` | 批量读，单请求实测可带 300 条 |
| 控制 | `/miotspec/prop/set` / `/miotspec/action` | 写属性与执行方法 |
| 场景 | `GetSimpleSceneList` / `NewRunScene` | |
| 产品图 | `/v2/productconfig/get_icon` | 302 跳转到 CDN，168×168 透明底 |
| 摄像头画面 | 本机 `go2rtc` + `xiaomi://` 源 | P2P 打洞由 go2rtc 做，程序只消费 `rtsp://127.0.0.1:…`，见下节 |

**没有推送，只有轮询。** 上游唯一接近事件的接口是账号消息，不是设备状态。所以实时性靠
两件事撑：只轮询卡片上真的会显示的属性（约 130 条，一个请求 0.4 秒），以及 3 秒一轮的
间隔。

## 目录结构

```
mihome/
  core/
    api.py      云端访问：批量读写、错误码分类、能力学习、静默期、子设备路由
    camera.py   摄像头取流：找 go2rtc、生成配置、启停子进程（可选外部组件）
    catalog.py  品类 → 卡片形态映射（21 个品类 + 槽位候选表）
    spec.py     MIoT-Spec 原始规格解析（单位、事件、中文 comment、access）
    store.py    运行时状态机：卡片状态、乐观写入、轮询集合、房间分组
    jobs.py     单线程串行执行器（三级优先级 + 主线程回调）
    tree.py     64 条设备条目 → 56 台真机（多键面板的虚拟子设备折叠）
    prefs.py    用户偏好：排序、隐藏设备（含分组折叠）、家庭标签样式、卡片样式（prefs.json）
    icons.py    产品图缓存
    paths.py    数据目录约定
  ui/
    main_window.py  主窗口与三个定时器
    chrome.py       自绘标题栏 + 无边框窗口（圆角 / 拖动 / 双击最大化 / 边缘缩放 / 压暗蒙版）
    confirm.py      自绘确认框（退出登录这类危险操作用）
    home_tabs.py    家庭切换栏（排列方式 × 激活样式，含位移动画）
    wall.py         房间墙
    device_card.py  设备卡片（外壳自绘；宽度与 body 都由卡片样式决定）
    card_templates.py 卡片样式注册表 + CardBody：尺寸表（通用）+ 品类模板（标准 / 亮度+色温 / 亮度 / 状态 / 温度控制），品类可声明自己默认用哪个
    card_controls.py 卡片内联控件：滑条 / 步进器 / 胶囊组 / 开关行
    detail.py       设备详情页
    camera_view.py  详情页实时画面：QMediaPlayer + 自绘圆角，含加载 / 错误态
    scene_bar.py    场景快捷条
    side_nav.py     房间导航
    settings.py     设置窗口：外观 / 排序 / 隐藏设备 / 账号
    settings_rows.py 设置页的骨架：分区标题 + 圆角卡 + 卡内行
    combo.py        全局下拉框：自绘箭头 + 真透明圆角浮层 + 不遮挡控件 + 禁滚轮 + 当前值打勾
    login.py        扫码登录
    theme.py        配色、品类色、QPalette 映射
    widgets.py      流式布局、图标缓存、文本绘制、切换开关
scripts/
  probe_cloud.py     只读探测云端结构
  verify_api.py      验证读取链路与错误码分类
  verify_store.py    验证状态机（--write 会真的开关一次灯）
  verify_batch.py    房间批量开关的离线验证（假 Client，不碰真设备）
  verify_prefs.py    排序 / 隐藏设备 / 家庭标签样式的离线验证（临时数据目录）
  verify_tabs.py     家庭标签两种排列 × 三种激活样式 + 胶囊盒模型像素验证（离屏，不连网）
  audit_catalog.py   品类映射覆盖率体检
  verify_bindings.py 回读验证 siid/piid 是否真有效
  verify_combo.py    下拉框验证：箭头/圆角/滚轮/接线/主题/当前值打勾；--native 查 WS_EX_LAYERED / alpha
  verify_card_style.py 卡片样式：偏好往返与 v3 迁移、尺寸/内容维度分离、注册表自检、品类默认样式、只读读数 body、空调温度控制 body（步进器 + 胶囊）与接线、几何、轨道像素、菜单结构、局部刷新、浅色卡片底色可读性（161 项）
  verify_hidden_fold.py 「已隐藏设备」折叠与按房间分层：只有该分组可折、三角紧贴标题、台数常驻、收起后只剩标题行高、落盘与重建、搜索临时展开、坏数据当展开（59 项）
  render_preview.py  用真实数据渲染卡片墙 HTML 预览
  shot.py            主窗口截图（--dark / --filter 关键词）
  shot_ui.py         多视图截图：主窗口 / 设置页 / 详情页 / 下拉框 / 胶囊标签 / 卡片样式 / 存在传感器状态卡 / 空调温度控制卡 / 已隐藏设备折叠（34 张）
  shot_states.py     交互态截图：房间标题悬停、详情页底部、离线禁用
  install_go2rtc.py  下载可选的 go2rtc（MIT，7.7MB，走 GitHub assets API）
  verify_camera.py   摄像头验证：预加载 / 播放 / 暂停 / 恢复 / 停止 / 离线 / 闲置释放 / 断流归因 / 孤儿清理 / 占位图（30 项，离屏）
  shot_camera.py     摄像头详情页截图：就绪态、播放态、闲置释放、错误态、占位图（5 张）
  measure_camera_startup.py 首帧计时：把 5–7 秒拆成进程启动 / 云端换票 / 握手 / 等 IDR / 解码
```

## 运行

**Windows 直接双击 `run.bat`。** 它会检查虚拟环境和 PySide6，缺什么装什么，然后启动。

```bash
# 等价的手动步骤
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m mihome
```

> `run.bat` 必须保存为 **CRLF 换行 + GBK 编码**。cmd.exe 解析批处理的括号块时要求
> CRLF，用 LF 会把 `(...)` 里的每一行拆成独立命令，报 "'...' 不是内部或外部命令"
> 和 "'errorlevel' 不是内部或外部命令"。改脚本时别让编辑器把它转成 LF / UTF-8。

首次运行会弹二维码，用米家 App 扫码。凭据写在 `~/.config/mijia-api/auth.json`，
和 mijiaAPI 的命令行工具共用，不用重复登录。

「设置 → 账号」里能看到当前登录状态、打码后的账号 ID 和凭据文件位置，也能**退出登录**：
会删掉那个 `auth.json`，然后直接拉起扫码窗口。米家 App 和云端的登录状态不受影响。
目前**同一时间只支持一个账号**，换号就是"退出登录 → 重新扫码"；多账号同时在线的方案
记在 [TODO.md](TODO.md)（上游 `mijiaAPI(auth_data_path=...)` 支持自定义路径，可行）。

## 排序与隐藏设备

界面顺序不是云端顺序，而是"用户排过的在前，没排过的按云端顺序追加"——这样新增的
设备 / 房间不会被埋到看不见的地方。

- 家庭、房间、设备、场景四类顺序都在「设置 → 排序」里拖，松手立即生效
- 家庭顺序同时决定顶部切换栏的排列；"上次看的是哪个家"单独记住
- **排列方式**（「设置 → 外观」）二选一：
  - `保持设定顺序`（默认）—— 位置永不变，切家庭时整排是静止的
  - `激活的排到最左` —— 当前家庭永远在第一个，切走时其余标签带 170ms 位移动画平移
  - 注意：这只影响**绘制顺序**，`prefs.json` 里存的仍是用户拖出来的那一份，
    否则切一次家庭就把排序改一次
- 隐藏设备是独立偏好：收进每个家庭末尾的「已隐藏设备」分组，卡片右键即可切换；
  隐藏只影响首页怎么排，轮询和控制都还在（点进详情页一样能用）
- **「已隐藏设备」可以整块收起**：点标题行（disclosure 三角 **紧跟在标题后面**，
  展开 ▼ / 收起 ▶，然后是常驻的「N 台」），卡片流整体隐掉，只剩 38px 的标题行——
  实测一组 21 台设备从 746px 压到 38px。
  - 三角放在标题旁边而不是分区最右边：隔着大半行空白的箭头没人会去点，
    「能点」这件事得贴着标题才看得出来。整行都可点，不要求精确命中三角
  - 台数**展开时也在**：一眼知道这一组有多少东西；不再提示「可在设置里恢复」，
    因为这组里右键就能取消隐藏，那句话只会把人往设置页支
  - 只有 `RoomView.kind == "hidden"` 的分区可折叠。普通房间的标题行保持
    `WA_TransparentForMouseEvents`，点它不会误收掉一整个房间
  - 收起态进 `prefs.hidden_collapsed`，切主题 / 切家庭 / 刷新重建墙之后仍是收起的
  - **搜索会临时展开**：命中项可能就在收起的那组里，否则"搜到了却看不见"。
    这个展开不写偏好，清空搜索后回到用户自己选的状态
- **隐藏分组内部再按房间分一层**（如「客厅 · 3 台」）：智能开关 / 音箱这类设备
  在不同房间里名字一模一样，平铺在一张流里根本认不出哪个是哪个。分组顺序沿用
  这一组自己的排序（首现顺序），只有一个房间时不分组——多一行标题纯属噪音。
  搜索时没有命中项的房间整块消失，不留孤零零的小标题。

落盘在 `<data>/prefs.json`（Windows 是 `%LOCALAPPDATA%\MiHomePanel\prefs.json`）。
删掉它等于全部恢复默认，不影响登录和缓存。

## 摄像头画面

米家摄像头没有 `stream_address`，码流走 TUTK/MISS 私有 P2P（UDP 打洞 + 帧加密），
厂商 SDK 有商业授权、不能进 GPL 发行包。所以这块**不自研协议栈**，把取流交给可选的
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

### 进去先预热，点播放才出画面

首帧要 3–6 秒（P2P 打洞 + 等 I 帧），直接自动播等于让用户盯着黑屏等。所以打开详情页
只做**预热**：照常建立 RTSP 会话，收到第一帧立刻 `pause()` 并把它留作封面，中间压一个
播放键；用户点下去才真正播放。

实测同一路 RTSP 从暂停恢复只要 **0.01–0.08 秒**（连接一直保持着，go2rtc 也没停推流），
所以「点播放」是瞬开的。暂停同理：画面停在当前帧，再点立刻续上，不会重新打洞。

控件状态机在 `ui/camera_view.py`：`preloading → ready → playing ⇄ paused`，另有
`released`（闲置放掉连接）和 `error`。播放中右下角常驻一个半透明暂停键（鼠标在画面上时
出现），中央只在就绪/暂停/释放时放大播放键。首帧之前的 `QMediaPlayer` 报错会自动重连
一次，不弹重试给用户。

### 首帧为什么要 5–7 秒：实测拆解

不是 P2P 打洞慢，局域网下打洞只要 0.3 秒。用
`scripts/measure_camera_startup.py` 把整条链路拆开（Xiaomi Smart Camera 3 / 069a01，
2960×1666 HEVC，局域网）：

| 阶段 | 耗时 | 能不能省 |
|---|---|---|
| go2rtc 子进程启动到 `/api` 就绪 | 0.85 s | 只在本次运行第一次取流时付；常驻可省 |
| 云端 `miss_get_vendor` 换票 | 0.65 s | **go2rtc 每开一路都要调一次**，可以自己预取后并行 |
| MISS 握手 + `probe` 到首个 RTP 包 | 0.55 s | 省不掉 |
| **等下一个 IDR** | **2.5–3.0 s** | 省不掉，见下 |
| 177–251 KB 的 IDR 传完 + 解码出帧 | ~1.0 s | 换子码流能小一截，但画质就没了 |
| 合计 | **≈6 s** | |

关键在第三行：**摄像头每 3.05 秒才出一个 IDR**（实测 `[5.25, 8.29, 11.29]`，间隔
3.04 / 3.00），而**会话起点那个 IDR 到不了消费者手里**——收到的最初 60 帧 POC 从 0 开始
却全是非关键帧，ffmpeg 只能一路报 `Could not find ref with POC 0/1/2…`，直到下一个 IDR
才解得出画面。也就是说每次打开都要完整等一个 GOP。

不是「谁先连谁独占」的错觉：让一路先占住生产者，后面接进来的第二、三、四路只要等
**0.94 / 1.17 / 1.46 秒**（随机相位），只有**会话的第一路**固定等满 3 秒。把
`probe()` 吃掉的那几包算进来就说得通了——go2rtc 为了拿 VPS/SPS 会先读掉开头若干包再交给
消费者，而摄像头恰好把 IDR 放在最前面。

所以米家 App「感觉秒开」不是因为它连得快，而是它**立刻显示上次的静止画面**，再在背后
慢慢地出实时流。这个我们也能做，见下。3 秒那一截在客户端无法消除，真要消掉只有两条路：
自己缓存关键帧（相当于自建一层 RTSP 代理），或者给 go2rtc 打补丁——都不划算。

### 首帧那几秒不黑屏：垫上次的画面

`ui/snapshots.py` 把每台摄像头最后看到的一帧缩到 960 宽存成 JPEG（实测 ~120 KB），
落在 `<data>/snapshots/<did>.jpg`，只在本地、不上传、不进仓库。下次打开详情页时先把
这张垫在画面区并压暗、标「显示的是上次的画面」，实时流一到就替换。闲置释放和报错态也
用它当背景，比纯黑好认。

存盘时机：暂停、停止、闲置释放，以及播放中每 300 帧（约 15 秒）刷一次，所以进程被强杀
也留得下近照。缩略处理在 UI 线程做，只在上面这几个时刻发生，不影响播放。


**没装 go2rtc 时不会静默失败**：画面区域直接写「没有找到 go2rtc」，并给出安装命令。

只装一次（7.7MB，MIT）：

```bash
.venv\Scripts\python.exe scripts\install_go2rtc.py
```

装到 `%LOCALAPPDATA%\MiHomePanel\go2rtc\go2rtc.exe`，也可以用环境变量
`MIHOME_GO2RTC` 指向别的副本。脚本走 GitHub 的 **assets API**，不是
`releases/download`——后者 302 到 `objects.githubusercontent.com`，本机网络下
`github.com` 和几个常见镜像都不通，只有 `api.github.com` 可用。

已知边界：

- 只做**实时画面**。云台、画质切换、SD 卡回放、云存储录像都没做
- 本程序对一台摄像头只开**一条** P2P 会话（多路观看复用 go2rtc 的 producer）；设备侧
  最多允许 **3 路并发**——实测第 4 路被设备直接掐断（go2rtc 日志 `miss: read media:
  cs2: EOF`），前三路不受影响。所以和米家 App 同看是可以的，占满 3 个观看位时后连的
  那一路才会掉
- **暂停不等于断开，也不等于让位**。实测暂停 4 秒内设备仍以 ~170 KB/s 向本机推流，
  控件只冻结在 1 帧：详情页开着（就绪 / 暂停 / 播放）就一直占着 3 个观看位之一，
  也一直走摄像头的上行带宽。离开详情页才会真正断开
- 所以**闲置 3 分钟会自动释放连接**（就绪和暂停都算闲置，播放不算）：画面变成
  「闲置 3 分钟，连接已释放」，点播放重新取流（一次 3–6 秒预热）。阈值见
  `camera_view.IDLE_RELEASE_SECONDS`，测试可用 `MIHOME_CAMERA_IDLE_SECONDS` 调小
- **崩了不会一直占着位**。实测强杀程序后，设备侧的观看位在停止推流后立刻可用
  （新会话 0 秒内就能连上）；但 Windows 不会跟着结束子进程，会留一个 go2rtc 占住
  1984 / 8554。启动时会按 `<data>/go2rtc/runtime.json` 把它清掉——三样都对上才动手：
  pid 还活着、映像名是 `go2rtc.exe`、命令行里有本程序的配置路径，所以不会误杀用户
  自己跑的 go2rtc
- 流被掐断时能分辨原因：go2rtc 日志里 20 秒内出现 `cs2: EOF` / `read media` 就写
  「画面中断：观看位可能已满（最多 3 路）」，否则还是通用文案
- 预热 3–6 秒（P2P 打洞 + 等 I 帧），期间写「正在连接摄像头…」；预热完成后即使用户不点
  播放，RTSP 会话也一直挂着（这是「点播放瞬开」的前提）
- **只能同一局域网直连**，原因不在米家而在这条实现路径：
  - go2rtc 的 CS2 传输（`pkg/xiaomi/miss/cs2/conn.go`）只做一件事——往**设备局域网 IP
    的 32108/UDP** 发 `LAN search`（`msgLanSearch 0x30`）然后打洞；它压根没有跨网中继
    的实现，`host` 必须是 `localip`
  - 云端 `miss_get_vendor` 返回的 `init_string` 里确实带 P2P 服务器信息，但 go2rtc 拿到
    之后只用来做认证（`sign` / `public_key` / `device_public`），中继地址被丢掉了
  - 想跨网就得自己实现小米的 relay（mihome-mac 的 `core/src/relay.rs`：`LIST_REQ1` +
    `25000/UDP` 的 `RLY_*` 状态机），或者换用带中继的后端。本项目没做，所以手机在外面
    看得到、这里看不到
  - 而这个 relay **不是「补一个函数」就能加上**：它只负责打通一条 UDP 管道，管道里跑的
    仍是 MISS 加密负载，所以得把 `relay.rs` 连同 `pppp.rs`（帧格式 / 序号重组 /
    `init_string`）、`miss.rs`（0x100 认证、0x102 取流）、`remote_crypto.rs`（X25519 →
    HSalsa20 → ChaCha20）整套搬过来，约 1500 行 Rust。落在哪儿也是问题：塞进 go2rtc
    意味着维护一个 Go 分支和自建二进制，另起 Python 旁路则要自己扛时序与抖动。
    取舍写在 `TODO.md` 的「摄像头二期」
- 画面是 2960×1666 的 HEVC，解码靠 Qt 的 FFmpeg 后端。控制台里那几类行都不是故障：
  - `qt.multimedia.playbackengine.codec: "No HW decoder found"` —— 我们主动关了硬解
    （见上文），Qt 例行提示一次。已在 `camera_view.py` 导入期把**这一个类别**关掉
  - `[av1_mf] / [h264_mf] / [hevc_mf] … could not find any MFT` —— ffmpeg 在枚举
    Windows 的 MediaFoundation 编解码器，4 行，一次性
  - `Could not find ref with POC` / `Skipping invalid undecodable NALU` —— 从 GOP 中间
    开始解码的必然结果，每次打开约 60 行，**出画即停**。它和「首帧等 3 秒」是同一个
    原因的两面：那 60 帧本来就解不出来
- 摄像头自带的时间水印会一起显示（那是画面的一部分，去不掉）

## 几个踩过的坑

这些是实测确认、写进代码注释的：

- **子设备 did 不可用**。多键面板派生的 `1087931746.s3` 对它发请求要拖到 6 秒才返回
  `-704083036`。云端只认父 did + 该路 siid。`.sN` 的 N 就是 siid。
- **"规格里有"不等于"设备认"**。红外空调的 `ir-mode`、部分门锁的 `lock-action` 回读
  一律 `-704002000`。所以有一层能力学习账本，把确认不支持的记下来，之后跳过而不是每
  3 秒重试一次。**但离线/超时绝不能记为不支持**，否则设备掉线一次就永久失去控制。
- **错误码要看后三位**。格式是 `-70 + HTTP 状态码 + 出错位置 + 错误码`，
  `013` = 属性不可读、`043` = 属性值错误、`011` = 设备离线。硬编码整个码值会漏。
- **`updateTime` 不随读取推进**。从没变过状态的插座会显示"3 天前"，所以它只能表示
  "多久没变过"，不能拿来判断数据新鲜度。
- **人体传感器大多没有 `occupancy` 属性**，只有 `motion-detected` 事件，属性轮询拿不到。
- **QSS 里的 8 位十六进制是 `#AARRGGBB`**，不是 `#RRGGBBAA`。写 `#9CA3AF66` 想要
  "灰色 40% 透明"，实际得到的是一块橄榄绿。要透明度就用 `rgba()`。
- **系统深色模式会漏进界面**。QSS 只画写了规则的控件，普通容器背景仍来自 QPalette；
  只在 QSS 里写 `QDialog{background:...}` 时，设置页正文会是一块系统深灰。所以
  `theme.qpalette()` 会显式把调色板也设一遍。
- **"取色板里那一档"是个陷阱**。同一支 `card`（深色 #1B1D22）压在设置窗口底
  `surface_bg`（#191A1D）上只差 2–5 个明度档，肉眼看就是一块；浅色主题下反而差 23 档
  白得干净。所以凡是要"浮起来"的容器，都得相对**它所在的那一层背景**推一档——
  `nav_bg` / `rail_bg` / `panel_bg` 三支分开写，谁也别想着复用谁的数值。
  反向也成立：面板比 `card` 亮之后，行悬停 / 选中（`panel_row_hover` /
  `panel_row_selected`）必须重新基于面板底算，否则深色下会变成"挖洞"。
- **QSS 里写 `qproperty-alignment: AlignRight | AlignVCenter;` 会炸掉整张样式表**。
  Qt 的解析器读到这种带 `|` 的枚举值就停，**后面所有规则一起失效**——表现是卡片
  底色、圆角统统没生效，看着像 objectName 写错了。对齐这类属性老老实实在 Python
  里 `setAlignment()`。
- **`addWidget()` 一旦带 alignment，布局就按 sizeHint 摆放而不铺满单元格**。
  `ElidedLabel` 的 sizeHint 被 `Ignored` 策略压成十几像素，于是"凭据文件"那行的值
  整块消失。要铺满就别传 alignment，把右对齐交给控件自己的对齐属性。
- **无边框窗口的"前后层"要自己造**。系统窗口自带阴影和边框，`FramelessWindowHint` 之后
  就只剩两块同色方块。现在靠三件事区分：子窗口底色离开主窗口一档（浅色更深、深色更浅）、
  外圈 1px 描边、弹出时主窗口整体压暗。`QGraphicsDropShadowEffect` 对顶层窗口无效，
  真要做投影得开 `WA_TranslucentBackground` 自己画。
- **QSS 的 `border` 不会自动让出内容区**。给 `QDialog` 写完 `border:1px` 之后，布局
  margins 还是 0，子控件会正好盖住那一圈，看起来像没生效。要 `setContentsMargins(1,1,1,1)`。
- **圆角只能靠遮罩**。无边框窗口没有系统帮忙裁圆角，`QSS border-radius` 也不会裁子控件。
  现在用 `setMask(圆角区域)`（逻辑像素，Qt 自己按 DPI 换算），代价是边缘没有抗锯齿
  （1 物理像素台阶，1.75 倍缩放下基本看不出）。两个必须记住的点：**最大化要 `clearMask()`**，
  否则四角会露出桌面；**每次 resize 都要重算遮罩**，不然窗口拉大后角又变回直角。
- **浮层（菜单 / 提示 / 下拉列表）的圆角是另一套，不是遮罩**。它们都是独立的顶层
  窗口，平台先铺一层不透明的方底，QSS 的 `border-radius` 只画在方底上，四角外
  仍然是方的。三处统一用 `FramelessWindowHint + NoDropShadowWindowHint +
  WA_TranslucentBackground` 让窗口自己透明（见 `menu.py` / `tooltip.py` /
  `combo.py`）。列表行改自绘 delegate，免得 QStyle 再画当前项的方框和
  `palette.highlight()` 方底。
- **`WA_TranslucentBackground` 必须在窗口创建前设，事后补是假透明**。`QMenu` 在
  构造函数里设，Windows 平台就给它 `WS_EX_LAYERED` + 8bit alpha；下拉框的
  `QComboBoxPrivateContainer` 却是 `showPopup()` 内部懒创建并**立刻显示**的，
  等拿到它再补属性，原生窗口已经按不透明建好（`alphaBufferSize() == -1`），
  四角的"透明像素"在屏幕上合成成纯黑方块。离屏 `QWidget.grab()` 只抓控件自己的
  backing store，四角 `alpha=0` 一片绿——**这类问题必须查原生窗口属性**。
  `Combo._make_layered()` 第一次展开时 hide → 设属性 → 把 `QWindow.format()`
  改成 8bit alpha → `destroy()` 旧 HWND → `setWindowFlags` 重建 → show；
  `verify_combo.py --native` 就是拿 `WS_EX_LAYERED` / alpha 做断言，离屏那套
  和真机这套**都要跑**。
- **浮层底色不能照抄承载它的卡片**。四角黑块没修之前，边界其实一直是黑块在提供；
  修好之后弹出层和设置页分区卡同为 `panel_bg`，整张菜单直接隐身。现在底色走
  `theme.popup_bg()`（深色比卡片亮、浅色比卡片略暗），边界再补一条**自绘**
  抗锯齿圆角描边 `theme.popup_border()`——绝不能写进 QSS 的 `border`，否则
  `QFrame` 会在圆角外又画一圈带斜接的方角框。
- **`QComboBox::showPopup()` 会把当前项对齐到控件本身**，列表正好盖住控件，用户看不到
  自己选的是什么。`Combo._place_below()` 改成右边缘对齐、控件下方留 8px，
  下方放不下才翻到上方，控件和 `^` 箭头全程可见。
- **下拉列表里的 `State_Selected` 不是"已选值"，是"鼠标/键盘临时停在哪"**。
  Qt 的 `QComboBoxPrivateContainer` 每次鼠标划过都会 `view->setCurrentIndex()`，
  那块跟着跑的高亮只是光标位置；真正的值在 `combo.currentIndex()`，弹出期间不变。
  以前把两者混为一谈，用户一移到别的选项就忘了原本选的是哪项。现在
  `PopupDelegate` 按 `currentIndex()` 给那一行画稳定的选中底 + 一支自绘的
  accent 色 ✓（不用字体，中文黑体缺这个字形会渲染成豆腐块），临时高亮只用
  `theme.popup_row_hover()` 的淡中性底；**每一行都预留同一个右侧勾槽**，否则文字的
  elide 边界会随鼠标忽长忽短。悬停色必须相对 `popup_bg()` 算，沿用相对分区卡的
  `panel_row_hover()` 会在深色下挖出一块暗洞。
- **`QComboBox` 默认吃滚轮**：鼠标停在下拉框上想滚页面，会顺手把设备属性改掉。
  `Combo.wheelEvent` 整个 ignore（键盘上下键保留，那是明确的输入），未接受的
  滚轮事件留给父级滚动区。设置页和设备详情页共用同一个 `Combo`，样式只在
  `combo_qss()` 里写一份。
- **自绘标签行的盒模型必须所有项一致**。胶囊档曾给每个 rect 都算上左右
  `PILL_PAD`，却只把激活项文字居中、其余左对齐——激活项平白多出 14px 左偏移，
  看起来"明显偏右"，切换家庭时文字还会在居中/左对齐之间来回跳。改成所有项统一
  居中后间距均匀、文字不动。但整排会因此右移 14px，父容器得抵消：
  **Qt 会把负的 `setContentsMargins` 静默换成样式默认值**（实测 `-14` → `9`/`11`），
  所以顶栏改用正边距扣减（`24 - inset`），设置页预览把标签控件当作子控件
  `move(-inset, 0)`（Qt 不裁剪子 widget 的绘制，卡片本身还有 16px 内边距兜底）。
  `HomeTabs.content_inset` 是这个缩进的唯一来源，切样式后必须重新读取。
- **截图脚本里 `grab()` 出来的 QImage 带着 1.75 的 `devicePixelRatio`**。直接拿它当
  画布、又按物理像素坐标 `drawImage`，会被再缩放一次，图层缩小甚至跑出画布。
  拼板前先把所有图层的 DPR 抹成 1（`shot_ui.py` 里的 `step_combo()`）。
  同理，**直接 `grab()` 一张自绘圆角卡片，四角外会被填成不透明黑**：按圆角裁剪
  叠上去，抗锯齿边缘已经和黑色混过，浅色底上会留一圈暗描边。正确做法是给它套一层
  填了 `pal.bg` 的父容器再 `grab(容器)`，让边缘和真实界面一样与背景混合。
- **截图脚本的定时回调链会吞异常**：`shot_ui.py` 全靠 `QTimer.singleShot` 串联，
  PySide6 对回调里的异常默认只把 traceback 打到 stderr 然后**继续跑事件循环**，
  脚本就挂在那里等一个永远不会来的 `done()`——不设外层 timeout 会永久卡死，设了
  也只是 `124`，和「图漏截了但正常退出」一样看不出原因。现在装了 `sys.excepthook`
  统一收尾（还原 prefs → 退出循环 → `main()` 返回 1），**只看 exit code 就能判断
  这轮截图是否完整**。这也是上次那批图少几张、却只能靠文件时间戳发现的原因。
  同理，**等 30 秒仍拿不到设备数据也算失败**（`failed=True`）：这时截出来只是一面
  空墙，脚本会直接提示「先运行 run.bat 登录」而不是生成一堆空白图。这条降级路径
  原来直接 `app.quit()` 跳过了 `done()`，会把测试改过的家庭/主题和 `prefs.json`
  留在日常配置里。
- **收起一个装着 `FlowLayout` 的分区，父 `QVBoxLayout` 不会自己收高度**。`_body` 是
  带 `heightForWidth` 的布局宿主，`setVisible(False)` 之后分区仍旧保持展开时的
  高度（实测 746px 纹丝不动，只是卡片不画了，下面多出一大片空白）。`invalidate()`
  也不够，必须 `invalidate()` + `activate()` 显式重排（`WallView._relayout()`）。
  反过来，`apply_card_style()` 里**只** `invalidate()` 不做 activate：那里换的是卡片
  自身宽度，`FlowLayout` 会自己重排，立刻重算根布局反而让滚动位置跟着跳一下
  （`verify_card_style.py` 的「局部刷新自己就会重排」就是盯这件事的）。
- **嵌套的 `FlowLayout` 要显式把 `heightForWidth` 传上去**。「已隐藏设备」按房间分层后，
  卡片流不再直接挂在 `_body` 上，而是各有一层宿主 `QWidget`。宿主必须
  `setSizePolicy(...)` 且 `policy.setHeightForWidth(True)`，否则父 `QVBoxLayout`
  按 `sizeHint` 给高度，分组后卡片被压成一条或撑出空白。
- **`wall.grab()` 是透明的**：墙的底色由主窗口铺，单独抓这个控件只得到一张全透明图
  （`alpha=0`，看着像"全白/全黑"，很容易误判成没渲染）。截图要么抓整个窗口，要么先
  把主题底色铺上去再 `drawPixmap`。
- **小图标要按像素判断，不能用平滑放大**。顶栏刷新图标只有 ~19px，放大 6 倍看
  "还行"的三个候选，换成 `Qt.FastTransformation`（最近邻）一放大全是糊的。另外
  1.7–2.0px 的**描边弧**在这么小的尺寸上会被抗锯齿摊成灰圈，箭头根本读不出来。
- **小尺寸的箭头别自己拿三角拼**。三角箭头的底边是沿**半径**方向的，缩到 20px 后
  那条 6–7px 的底边会读成"环上鼓起一根竖刺"，而它真正指的方向反而看不见——两次
  自绘都是这个毛病（第一版是 290° 弧 + 一根斜线）。改成 Material `refresh` 字形的
  **方头箭头**（一整块实心色块，缩小后仍是一个色块）才读得出来，路径见
  `main_window._refresh_path` 与「开源致谢」里的 Material Icons 一行。教训是：
  这个尺寸下要判断"缩小后还剩几个可辨识的色块"，而不是"放大后线条好不好看"。
- **浅色主题的卡片底色要守一条下限**。`bg #F1F2F4` 只比白卡暗 13，开启态再混 10%
  品类色就只剩 1–3；电视、摄像头这类深灰品类混完**比背景还暗**，卡片整个糊进背景
  （用户报的「电脑」就是这个）。`theme.tint(..., min_gap=6)` 会把染色强度往小收到
  刚好够看为止——亮度是强度的线性函数，解方程即可，不用二分。
- **卡片模板的 body 必须是真实 `QWidget`，不是 `QPainter` 手绘**。拖动捕获、
  移出控件后的追踪、「点空白开详情 / 点滑条调亮度」的事件分流都是白送的。代价是
  三条要自己兜：鼠标移到子控件时**父卡片也会收到 `leaveEvent`**（用
  `QCursor.pos()` 判断光标是否还在卡片 rect 内，别让悬停底色闪一下）；子控件默认
  `ignore()` 的 `ContextMenu` 要在 body 上接住转发给卡片；切主题时子控件不会跟着
  外壳重绘，得显式 `apply_theme()`。
- **右键菜单的子菜单是另一层原生窗口**。`parent.addMenu("标题")` 让 Qt 自己建的那一层
  既没有 `WA_TranslucentBackground` 也没去边框，圆角背后又会冒出方块底——和主菜单
  当初一模一样。用 `menu.submenu()` 显式建同一个 `Menu` 类；勾选也别用
  `QAction.setCheckable(True)`（系统 check indicator 带方底），改成自绘 `QPainterPath` ✓，
  未选项放一枚同尺寸透明图标占位，文字左边缘才齐。
- **悬停提示只在卡片标题带触发**。`tooltip.watch()` 默认把整个控件矩形算命中区，
  可 2×1 灯具的亮度/色温滑条是真实子控件，气泡从光标右下 16px 展开会压住轨道。
  现在 `watch(..., hit_test=...)` 让卡片只认「名称 + 状态文字」那一条（右上电源键
  也扣掉）；Enter / Leave / 定时到点 / MouseMove 四处共用 `_still_inside()` 这一份
  判据——**卡片内部从标题移到内容带只有 MouseMove**，不补这一条提示不会收。
- **卡片「大小」和「模板」曾是两份偏好，其实不独立**：模板的 `min_span` 会悄悄
  把用户设的 1×1 撑成 2×1，宽度变成只在切回标准时才复活的影子状态；而
  `2×1 + 标准` 只是把 76px 产品图放进一张更宽的空卡，`1×1 + 双滑块` 根本不可达。
  现在宽度写进样式（`standard` 1×1 / `light_dual` 2×1 / `light_bright` 2×1），
  `prefs.card_styles` 一条记录就是一个 `did → style_id` 字符串。v3 的
  `{span,template}` 在读取时迁移：`template` 沿用为样式 id，只设过 span 的记录
  回默认小卡。默认样式由品类声明（多数设备是 1×1 标准，存在传感器是状态卡），
  菜单第一项就是当前默认（点它会删掉偏好记录），
  所以「恢复默认样式」也删了。改样式仍走 `WallView.apply_card_style(did)` 局部刷新
  ——重建整面墙会连滚动位置、其它卡上的悬停、正在拖动的滑条一起丢掉。
  菜单文字的格式统一成 **`宽卡 2×1：亮度 + 色温`**：尺寸是「占多大格子」的硬信息，
  放在冒号前先读到；同尺寸的项前缀完全一致，纵向扫一眼就能分组，不用逐条读内容。
- **合并的是"用户那一次选择"，不是数据模型里的两个维度**。尺寸（`span`）是**全品类
  通用**的：只有 1×1 / 2×1，定义在 `SIZES` 表里，菜单前段完全由它生成；内容
  （`label` + `slots`）跟品类走，灯是「亮度 + 色温」，空调是「温度控制」。
  `CardStyle.label` **只准写内容名**，写了尺寸会在 `__post_init__` 直接报错——否则
  同一段尺寸就有 `span` 和字符串两个来源。`span` 也必须在 `SIZES` 里（写 3 会在
  import 时报错，而不是菜单上显示成"宽卡 3×1"）。加一个品类的模板只需声明
  `CardStyle(id=..., label="温度控制", span=2, category="ac", slots=...)`，
  尺寸文案、菜单前缀、几何宽度都不用碰。
- **同一内容可以出现在多个尺寸上，靠 id 区分、label 不用加后缀**：两条记录
  `id` 不同、`label` 相同、`span` 不同，菜单自然读成「小卡 1×1：温度」/
  「宽卡 2×1：温度」。**同名判重按"单台设备可见的集合"来做**：两个样式的
  `category` 相同、或任一方是通用的 `None`，它们才可能同时出现在一张菜单里，
  这时 `(label, span)` 撞车必须拒；品类不同的两条（灯的「亮度」和浴霸的
  「亮度」）永不同框，放行。id 若重复会静默覆盖偏好，同样拦掉。
- **`standard` 不是某品类的模板，是所有设备的兜底外壳**：`category=None`、
  `slots=()`，含义是"不挂内容带，产品图 / 多键面板由外壳自绘"。任何品类里
  看到「小卡 1×1：标准」都是同一件事——**回到产品图外观**。
- **「默认」和「标准」不是一回事**。品类可以在 `catalog` 的 `CategoryProfile`
  上声明 `default_style`（存在传感器 → `presence_state`、空调 → `temp_control`），
  表示"没有偏好时用
  哪个样式"，`resolve_card_style()` 三道兜底（没设过 / id 不认识 / 能力不够）
  都落到 `default_style(card)`，而不是硬编码标准。所以：
  - `prefs.card_style(did)` 对"没设过"必须返回 **`None`**，不能预填 `standard`
    ——两者混成一个值，品类默认永远轮不到；
  - 菜单里默认项排第一（排序键 `(不是默认, span)`），且"点默认项"= 删记录；
  - 在默认≠标准的设备上点「标准」是一条**真实偏好**，必须落盘（`set_card_style`
    带 `default_id` 参数），否则用户点完又回到状态卡，菜单像失灵。
  - `catalog` 是 core、样式表在 ui，两处只能靠 id 字面量对齐；
    `validate_profile_defaults()` 在 import 时校验拼写，拼错会当场报错而不是静默
    退回标准。
- **body 不只有滑条**（`CardStyle.body`）：`sliders` 建 `CardSlider`，
  `readout` 建 `CardReadout`（只读大字），`controls` 则**按属性类型各配一种控件**
  （布尔 → 开关行、枚举 → 胶囊组、数值 → 步进器）；空调「温度控制」是第一个
  用户：2×1 里一行「− 26°C +」加一行五个模式胶囊。**减号在数字左侧**是刻意的：
  卡片 body 有 312 宽，两个按钮都挤在右边时手指要在同一处分辨减/加；
  数字两边各一枚和空调遥控器一致，左边调低、右边调高。详情页那一行左侧还要
  放属性名，仍是「数值 …… − +」。存在传感器的卡片正中一个「有人」，
  副标题仍然是「有人 · 49%」——**不为了去重去动副标题**：`skip` 需要知道 body
  实际建出了哪些控件（声明了但没绑上的槽位会被 body 丢掉），而副标题和 body 是
  两条独立的渲染路径，耦合后详情页（`DeviceState.subtitle`）那份还得另算一遍。
  让副标题只认 `card.metrics`，换样式时它一个字都不变，代价只是重读一次状态。
  两种 body 的可用性判据不同：滑条要求 `value_range`（没刻度映射不了轨道像素），
  读数只要属性存在。槽位查的是 `CardSpec.bound()`（**先控件后读数**），
  所以样式可以引用 `occupancy` 这种登记在 `metrics` 里的只读指标，不必知道
  它当初登记在哪一边。
- **2×1 宽卡的 body 要主动用掉宽度**。卡片内容带是 312×78，而「数字 + 两个按钮」
  只有 ~130 宽：按详情页那套「数字靠左、按钮贴右」排，中间会空出 200px，看着像
  少画了一块。所以给 `CardStepper` 加了 `centered`（整组居中）、给 `CardChips`
  加了 `spread`（余宽均分给每一项，变成等宽分段控件），两个开关**只在卡片 body
  里打开**，详情页那套默认行为一个字没动。温度用步进器不用滑条，是因为它是个
  精确的小整数（16–32），拖拽反而选不准——这也是米家的做法。
- **可见性只有一条链路**：`CardStyle.available()` 先卡 `category`（`None` = 通用），
  再按 body 类型要求 `slots` 里每个槽位都真绑上属性。`catalog.build_card()`
  会把没绑上的槽位直接丢掉（只对 `critical` 的记进 `missing`），所以卫生间那种
  只能调亮度的平板灯 `card.bound("color_temp")` 返回 `None`，菜单里就没有
  「亮度 + 色温」，只剩「标准」和「亮度」；如果它以前存过双滑块，`resolve_card_style()`
  会兜回默认样式，不会留一张空卡。
- **`QMediaPlayer` 重播同一个 RTSP 地址必须先 `stop()`**。直接 `setSource(同一 URL)`
  会被判定成「源没变」而不重连；表现是点重试毫无反应、画面一直停在错误态。
- **Qt 的 RTSP 第一次 `SETUP` 会收到 `461 Unsupported transport`**，然后自己回退 TCP。
  日志里那行不是错误，别照着它去改传输方式。
- **`QGuiApplication` 下建 `QWidget` 会直接把进程带走**：退出码 127、没有任何 traceback。
  截图 / 验证脚本只要碰控件就必须用 `QApplication`，哪怕平台是 `offscreen`。
- **go2rtc 的 `frame.jpeg` 每帧都要调 ffmpeg 转码**（实测 700ms/帧），适合「抓一张图」，
  不适合当视频源。实时播放走 RTSP 直通，解码交给 Qt 自带的 FFmpeg 后端。
- **go2rtc 的 API 比协议模块先就绪**。`xiaomi` 排在 `main.go` 的 `modules` 列表最后，
  `GET /api` 已经 200 的时候它可能还没 `HandleFunc("xiaomi")`；此时发来的第一条 RTSP
  请求会报 `unsupported scheme: xiaomi://…`，而且这个错误会被 stream 缓存下来，后面
  再连也一直失败。所以 `start()` 在 API 就绪后要再等 300ms，控件首帧前的报错也自动
  重连一次。症状很怪：同一个配置手动跑必成功、从程序里跑约 1/3 失败。
- **Windows 上不能用 `SO_REUSEADDR` 探测端口空闲**。它允许 `bind` 到别人已经 `listen`
  的端口（Linux 不允许），于是「端口占用检测」永远返回空闲，新 go2rtc 抢不到 8554 却
  以为起来了，Qt 连上旧实例、错误还对不上。要用 `SO_EXCLUSIVEADDRUSE`。顺带把就绪
  探测从 `/api` 换成 `/api/streams` 并核对流名，避免连到别人的实例还以为是自己。
- **`offscreen` 平台下 Qt 的字体库是空的**，`QFont("Microsoft YaHei UI")` 拿不到字形，
  `draw_text` 画的中文全是方块（`QFontInfo().family()` 返回空串）。离屏脚本的截图
  只能用来看布局，核对文字得用 `shot_camera.py` 这种真实窗口。

## 目前没做的

完整的待办在 [TODO.md](TODO.md)。近期明确排在后面的是：

- **把电脑接进全屋智能**（下一阶段主线）：电脑事件触发米家场景、米家设备变化反过来
  操作电脑。设计草案、延迟预期和落地顺序都写在 TODO 里，动手前要先定三件事
- **多账号切换**、**排序页改版**
- **系统托盘常驻**、**桌面挂件**（复用现有卡片组件）
- 门锁开锁记录、人体触发这类**事件**：规格里有 `events`，但云端没有查到可用的拉取接口
- **局域网直连**（miIO / MIoT LAN）：需要设备 token，且蓝牙设备不支持
- 摄像头只能看实时画面；云台、画质切换、SD 卡回放、云存储录像都还没做

## 开源致谢 / Credits

本项目不是从零写的：传输层、几个架构判断、规格解析思路，以及一批待办功能的可行性
结论都来自别人的开源工作。这里按**具体贡献了什么**登记，不按知名度排；许可证也一并
核实过，因为本项目是 GPL-3.0，能不能抄代码、能不能进发行包全看这一栏。

**登记规则**：新增依赖 / 移植 / 借鉴 / 协议参考，在引入那一轮就补这张表，流程见
[TODO.md](TODO.md) 末尾的「工程约定：开源致谢流程」。状态只用这五种：
`已依赖` = 运行时代码真的在用；`已沿用` = 吸收了架构判断、未复制代码；
`仅参考` = 读过实现或文档、还没写进代码；`拟定方案` = 打算照它做、还没实现；
`明确排除` = 读过但不纳入发行包。

### 已经在代码里

| 项目 | 许可证 | 具体贡献 | 状态 |
|---|---|---|---|
| [Do1e/mijia-api](https://github.com/Do1e/mijia-api)（PyPI `mijiaAPI`） | GPL-3.0-or-later | 云端传输层：扫码登录、`gen_nonce` / RC4 签名与加解密、`serviceToken` 刷新、`/miotspec/*` 封装；`core/api.py` 和 `scripts/probe_geek_passcode.py` 直接调用。**它决定了本项目也必须是 GPL-3.0** | `已依赖` |
| [huanyuejue/MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows)（仓库内 `MiHome-Windows-0.5.0/`） | GPL-3.0 | 起点参考项目，详见「和参考项目的关系」：沿用单线程串行队列、子线程不直接回调 UI、乐观 UI + 版本号这三个判断，以及 16ms / 3s / 5min 定时器的分工。界面层完全重写。TODO 里的小爱文字指令、定时开关、托盘、桌面挂件也以它的功能清单为参照 | `已沿用` |
| PySide6 / Qt for Python | LGPL-3.0 / GPL-2.0 / GPL-3.0 三选一 | 整个 UI 框架 | `已依赖` |
| [Material Icons](https://github.com/google/material-design-icons) | Apache-2.0 | 顶栏「刷新」按钮的图标形状：`ui/main_window.py::_refresh_path` 按它的 `refresh` 字形（24×24 视口）逐段抄写坐标后缩放，形状未改。自绘两版（弧 + 斜线、环 + 三角箭头）在 20px 下都读不出来，理由见踩坑清单 | `已沿用` |
| `qrcode`（BSD）、`Pillow`（MIT-CMU）、`pycryptodome`（BSD-2-Clause / Unlicense / MIT） | 见 `requirements.txt` 与各发行包 | 二维码渲染、图片处理、X25519（探测 `miss_get_vendor` 时用过，正式引入要显式声明）。经 pip 安装，不随本项目分发 | `已依赖`；X25519 那条是 `仅参考` |

> `MiHome-Windows-0.5.0/` 只是随仓库保留的参考副本，它自己的 UI 依赖
> `H1DDENADM1N/PySide6-SiliconUI`、`qtawesome` 等，本项目**没有**沿用，故不单独登记。
> 设备能力数据取自小米官方 [MIoT-Spec / miot-spec.org](https://miot-spec.org) 与米家 App
> 私有接口——它们是规范和数据服务、不是开源项目，在此注明来源。

### 摄像头画面（已实现）

2026-11-07 四轮调研的结论：账号里两台摄像头都没有 `stream_address`，实时画面只能走
P2P；`miss_get_vendor` 已用本项目凭据实测拿到 `vendor=4`（CS2）的全套取流授权。
最终实现**没有自研协议栈**：由 go2rtc 负责 P2P/CS2 → 本机 RTSP，Qt 侧只解码渲染，
代码在 `core/camera.py` 与 `ui/camera_view.py`。下表按实际采用情况登记：

| 项目 | 许可证 | 具体贡献 | 状态 |
|---|---|---|---|
| [AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc) | MIT | **实际采用的取流组件**：可选外部进程，把 P2P/CS2 码流转成本机 RTSP；程序只消费 `rtsp://127.0.0.1:<port>/cam_<did>`。由 `scripts/install_go2rtc.py` 下载到 `<data>/go2rtc/`，**不随仓库 / 发行包分发** | `已依赖` |
| [Reign9201/mihome-mac](https://github.com/Reign9201/mihome-mac) | GPL-3.0 | 协议蓝本：`core/src/` 的 X25519→ChaCha20、PPPP 帧、relay 状态机、MISS 认证与取流，以及 `docs/04`、`docs/06` 里的字节序和坑；**`chuangmi.camera.069a01` 由该项目真机验证出画**。本次只借判断、没有移植代码 | `仅参考` |
| [DHJ999/mi-home](https://github.com/DHJ999/mi-home) | 注意：应用层**无开源许可**（GitHub 标 MIT 是误判，仓库只有 `LICENSE-go2rtc.txt`） | 只借接口约定、不抄码：`app/go2rtc.mjs` 的 `xiaomi://` 流源、`/api/streams`、`/api/xiaomi/recordings` 回放参数，以及云台按住连转、回放时间轴的交互 | `仅参考` |
| [Songmanual/MijiaDeck](https://github.com/Songmanual/MijiaDeck) | GPL-3.0 | 同源 Python/PySide6 的 `camera_preview.py` / `camera_access.py` 集成形态；它的请求写法补上了我们缺的 `app_pubkey`，P2P 授权接口因此打通 | `仅参考` |
| [dashi45/mihome-pc](https://github.com/dashi45/mihome-pc) | MIT | `camhls.py` 的 go2rtc → HLS 播放链路 | `仅参考` |
| [LiuLiuxc/FnDepot](https://github.com/LiuLiuxc/FnDepot)（miNVR） | 无 LICENSE、闭源 | 只看产品形态：九宫格、事件时间轴、分段录像 | `仅参考` |
| [al-one/hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot) | Apache-2.0 | 云录像链路（`get/eventlist` → `m3u8` → `mp4` / `img` 与 AES 解密），以及「`stream_address` 路线不适用」的判定依据 | `仅参考` |
| IMILAB「IMI Camera」Windows 客户端 | 闭源商业软件 | 静态分析其日志与 PE 字符串，确认 `sid=xiaomiio` 换领、`/device/devicepass`、TUTK/MISS 打洞的完整链路 | `明确排除` |
| [eugeneRover/micam](https://github.com/eugeneRover/micam)（MijiaDeck 的 Java 后端） | 无 LICENSE | 上游再分发许可未确认 | `明确排除` |

> TUTK/MISS 厂商 SDK、micam JAR、`go2rtc-cs2.exe` 一律**不随 GPL 发行包分发**。
> 这里只用官方 go2rtc（MIT）当外部进程，二进制由安装脚本按需下载；2026-11-07
> 已用 `chuangmi.camera.069a01` 实测出画（2960×1666 HEVC，首帧约 6 秒，约 20fps）。

### 同一批项目对其它待办的贡献

链接见上表，这里只记落点（均为 `仅参考`，尚未写进代码）：

- **mihome-mac**：多账号隔离（`docs/09-multi-account.md` 的 accounts.json 0600、
  自动化按 `sha256(uid)` 分开、切换账号前停摄像头）；局域网设备 token 取自
  `cloud.rs::device_network()` 的 `/home/device_list`。
- **MijiaDeck**：小爱文字指令的 `execute-text-directive` action 用法、
  `app/core/diagnostics.py` 脱敏诊断报告、`app/core/spec_zh.py` 枚举中文短名、悬浮托盘。
- **dashi45/mihome-pc**：能耗数据源 `/v2/user/statistics` + `get_device_data_raw`、
  `cardsort.js` 长按拖动、`tray.py` / `suspend.js`、`evtlog.py` / `scheduler.py` /
  `procman.py` / `netguard.py` 的规则引擎触发源与重试策略、能力三态与分层轮询。
- **FnDepot / miNVR**：按天回放与事件时间轴的产品形态。

### 调研文档引用过的项目

`docs/reference/mijia-geek-login-code.md` 第 8 节的资料表同样属于致谢范围，登记在此免得漏：

- [eyaeya/xiaomi-central-hub-gateway-cli](https://github.com/eyaeya/xiaomi-central-hub-gateway-cli)（GPL-3.0）：云端取址 / 取码 RPC 契约
- [ChishFoxcat/MIJIA_GEEK](https://github.com/ChishFoxcat/MIJIA_GEEK)（无 LICENSE）：油猴脚本抓包重放思路
- [609293964/MIJIA_GEEK-for-fnos](https://github.com/609293964/MIJIA_GEEK-for-fnos)（无 LICENSE）：飞牛版 HAR 自动解析
- [weieast1314/oh-my-sage](https://github.com/weieast1314/oh-my-sage) / [allocnode/oh-my-sage](https://github.com/allocnode/oh-my-sage)（均 MIT）：网关本地协议与「常驻服务只登录一次」
- 小米官方 MIoT-Spec RPC 文档、`asmslight.com` 操作路径：非开源项目，仅作资料

`docs/reference/drag-sort-spec.md` 是用户提供的《拖动排序实现方案》行为契约，原始出处
未标注；只作规范参考，真要照它抄实现时先补出处并登记。README 里的
`docs/reference/米家App-UI风格参考.md` 是从自家真机采集的，不属于第三方成果。

## 许可证

GPL-3.0-or-later。依赖的 [mijiaAPI](https://github.com/Do1e/mijia-api) 是 GPL-3.0，
本项目直接复用它的传输层，所以整体也必须是 GPL-3.0。第三方项目、各自许可证与
再分发边界见上节「开源致谢 / Credits」。
