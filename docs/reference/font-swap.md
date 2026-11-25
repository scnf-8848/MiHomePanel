# 界面字体：换不换、换什么、要花多少

> 2026-11-16 调研。起因是待办里那句「13 px 下笔画边缘能看到毛刺，观感一般」。
> **2026-11-16 当天 A 已落地**（见第 3 节 A，「已落地」小表）。
> **2026-11-17 用户定 B（换字体）不做**：A 落地后这版灰度字已经能接受，"换不换字形"
> 就此闭环。下面第 3 点里那些成本只作为技术备忘保留，不再排期。
>
> **结论先说**：
> 1. **「毛刺」不是字体选得不好，是 ClearType 子像素彩边**。真实窗口截图放大 7 倍，
>    笔画边上就是红蓝两色；**三种字体都有**，换字体不解决。
> 2. 有一个**零体积、几行代码**的修法：把字体引擎切成 FreeType 并关掉子像素抗锯齿。
>    **已实测落地**：彩边最大 176 → 9、平均 15.9 → 2.0，明暗两档一致；**布局零位移**
>    （换前换后截图 99.65 % 像素完全相同）。
> 3. 换字体（MiSans / 思源黑体）解决的是**字形本身**，不是毛刺。**11-17 判定不做**
>    （A 落地后的灰度字能接受）。真要换，前提是接受 **3.5–17.8 MB** 的体积、
>    一条许可义务、以及一次**没有自动化网兜底**的肉眼回归（现有 53 个脚本里只有
>    `verify_fonts.py` 碰字体，而它测的是引擎开关与策略，渲染质量仍要看图，见第 5 节）。

---

## 1. 现状：字体出口已收敛到 `mihome/ui/fonts.py`（2026-11-16 改）

| 位置 | 内容 | 为什么必须管 |
|---|---|---|
| `mihome/ui/fonts.py` | `FAMILY` / `PIXEL_SIZE` / `make_font()`（带 `NoSubpixelAntialias`） | 唯一出口。`app.py` 两条启动路径都走 `fonts.apply_to(app)` |
| ~~`mihome/ui/theme.py` 的 `FONT_STACK`~~ | **2026-11-25 已删除**：QSS 不再下发 `font-family` | 那个 `*` 规则让 Qt 给每个控件各建一套字体引擎，真实主窗口多占 **31 MB 私有内存**（30.8 vs 61.9）。字体现在只剩 `fonts.py` 一处 |
| `mihome/ui/fonts.py` 的 `prepare_engine()` | 在 `QApplication` 之前把 `QT_QPA_PLATFORM` 改成 `windows:fontengine=freetype` | 平台插件只在构造时读一次环境变量，顺序错了完全不报错、也没效果 |

改动前是"三处各写一遍"（`app.py:390`、`app.py:173`、`theme.py` 的 `FONT_STACK`）；
现在**只剩 `fonts.py` 一处**。2026-11-25 把 `FONT_STACK` 也删了：QSS 的 `*` 下发
五段字体栈时，Qt 会为每个控件单独解析、各建一套字体引擎（实测 143 个控件的主窗口
构造 + show 多 31 MB 私有内存）。回退能力交给 Qt 自己的字体替换，换字体改
`fonts.FAMILY` 一处即可。

旁支（不影响运行时，但改名后要一起扫）：

- `setPixelSize` 共 **26 处**（12 / 13 / 15 px 混用），字号单位是**像素**，不随 DPI 逻辑缩放；
- 仓库里还有 **20 个脚本**写着 `Microsoft YaHei UI`（29 处）：10 个 `verify_*.py`（自己起 QApplication
  时显式设字体）、10 个截图 / 探针脚本。**它们不会跟着 `fonts.FAMILY` 变**，换字体那天要一起扫；
- **打包侧没有任何字体资源**：`build_exe.py` 目前只 stage `go2rtc/`，没有 `--add-data`，
  也没有字体加载失败的回退分支。

字体是从系统拿的，所以**今天在别人机器上等于听天由命**：Windows 都有雅黑，观感不会崩，
但也永远只会是雅黑。

---

## 2. 实测：13 px 下三种字体到底差在哪

测量环境：PySide6 6.12.0 / Qt 6.12.0，Windows 平台插件，`setPixelSize(13)`。

### 2.1 度量

| 字体 | ascent | descent | **lineSpacing** | height | 米家控制台 | 亮度 68% | 共 12 条 · 本页 5 行 | F3 快捷键 | 电量 87% · 已充电 |
|---|---|---|---|---|---|---|---|---|---|
| Microsoft YaHei UI | 13 | 3 | **16** | 16 | 65 | 57 | 114 | 57 | 106 |
| MiSans | 14 | 4 | **18** | 18 | 65 | 56 | 115 | 58 | 108 |
| Noto Sans SC（思源黑体同源） | 15 | 4 | **19** | 19 | 65 | 55 | 117 | 56 | 113 |

两件事值得记住：

- **纯中文的宽度完全一样**（`米家控制台` 三家都是 65）。中文字形是 1 em 定宽，换字体
  不会让「客厅吸顶灯」变宽变窄，写死宽度的下拉框（`_FEATURE_W = 118` 那种「刚好不省略」）
  **仍然成立**。
- 差异全在**拉丁、数字、标点**：Noto 的数字和 `%` 更宽，`电量 87% · 已充电` **+6.6%**；
  MiSans 基本在 ±2% 以内。要复查 elide 的就是 `F3`、`68%`、`26.5℃` 这类混排。
- **行距 16 → 18/19（+12%～+19%）**。26 / 32 / 36 px 的行高仍装得下，但垂直居中会下移
  ~1.5 px；`48`（警告条）、`52`（详情页行）、`36`（页脚按钮）这些定高要看一眼。

### 2.2 字重：现在的 Medium 其实不存在

| 请求字重 | 雅黑实际 | MiSans 实际 | Noto VF 实际 |
|---|---|---|---|
| Normal | Regular | Regular | Regular |
| Medium | **Regular**（没有 Medium 面） | **Regular** | Medium |
| DemiBold | **Bold** | Bold（**合成加粗**，本机只有 Regular 一个文件） | Bold |
| Bold | Bold | Bold | Bold |

墨量（13 px 同一串字的平均暗度）：雅黑 Regular **48.7**、MiSans **41.2**、Noto Regular **39.2**、
Noto Medium **45.6**、Noto DemiBold **50.4**、Noto Bold **56.5**。

→ **换成 Noto 的 Regular，视觉上比现在浅 19%**。不调基准字重会显"发虚"，反而更像"糊"。
真换的话，基准要落在 **450～500**（Noto Medium 的 45.6 与现在最接近）。

→ 顺带暴露一个现存的实现瑕疵：代码里写了一堆 `Medium`，实际全是 Regular；
`DemiBold` 全部落到 Bold。想要真正的中间档，只有可变字体（VF）能给。

### 2.3 「毛刺」的根因：子像素彩边（这是本节的重点）

真实窗口截图（不是离屏渲染）放大 7 倍，13 px 中文笔画两侧是**红 / 蓝彩边**——
Windows ClearType 的典型形态。量化一下（同一串字，平均 `|R−B|`）：

| 字体 | 平均彩边 | 最大彩边 |
|---|---|---|
| Microsoft YaHei UI | 20～28 | 168 |
| MiSans | 22～28 | 168 |
| Noto Sans SC | 22～24 | — |

**三种字体一样脏**，所以"换一款字体"并不能消掉用户看到的那层毛。

试过无效的路（都是像素级同一张图，hash 相同）：

- `QFont.setStyleStrategy(QFont.NoSubpixelAntialias)` —— Qt 6.12 的 DirectWrite 路径**直接忽略**；
- `QFont.PreferAntialias` —— 同上；
- `setHintingPreference(PreferNoHinting)` —— 有效果，但方向是**更糊**（墨量 45.7 → 41.4，边缘更散），
  不是更清楚。

**唯一有效的一条**（实测）：

```python
os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=freetype")  # 必须在 QApplication 之前
font.setStyleStrategy(QFont.NoSubpixelAntialias)
```

效果：平均彩边 **20.09 → 0.00**（纯灰阶），最大彩边 168 → 0；整窗（`MainWindow.grab()`）确认一致。
副作用两条，都必须在真机上看：

- 全局走 FreeType 的 autohint，字形与 DirectWrite 有细微差别（行高量出来 30 → 28 px，
  `QFontMetrics` 的数字会变，布局可能有 1–2 px 的位移）；
- 灰度抗锯齿在高分屏上通常更"干净"，但在浅色底上可能显得略淡——这正是"比彩边好还是差"
  需要肉眼裁决的地方。

### 2.4 体积与加载耗时（决定"自带字体"到底多贵）

用 fontTools 子集化 + `instancer` 实测（子集范围 = CJK 基本区或 GB2312 6763 字 + ASCII + 中文标点）：

| 方案 | 体积 | `addApplicationFont` 耗时 | 覆盖 |
|---|---|---|---|
| Noto VF **原样**（不改文件） | **17.77 MB** | 84.7 ms | 全字符集、真实 4+ 档字重 |
| Noto VF 子集 · 全 CJK 基本区 20992 字 | **11.92 MB** | — | 几乎不缺字 |
| Noto VF 子集 · GB2312 6763 字 | **3.52 MB** | 10.8 ms | 常用字齐全，生僻字回退系统字体 |
| Noto 静态 3 档（400/500/600）· GB2312 | **6.30 MB**（2.1 × 3） | — | 同上，但要带 3 个文件 |
| Noto 静态 3 档 · 全 CJK | 21.30 MB | — | 同上 |
| MiSans Regular **原样**（本机就这一个文件） | 6.50 MB | 12.7 ms | 只有 Regular 一档 |

对照：`dist/MiHomePanel` 现在 **81 MB**。带 3.5 MB = **+4.3%**；带 17.8 MB = **+22%**。

加载耗时这一列说明：**子集不只是省体积，还省启动时间**（85 ms → 11 ms）。这 85 ms 花在
每次启动、每个 exe 上，值得顺手赚回来。

### 2.5 许可：这是唯一"卡死"的约束

| 字体 | 条款 | 对随包分发意味着什么 |
|---|---|---|
| **Noto Sans SC / 思源黑体** | SIL OFL 1.1 | 允许**捆绑、嵌入、修改、再分发**（不得单独售卖）。name 表里写着 **Reserved Font Name `Source`** |
| **MiSans** | 小米自有许可 | 免费商用 + **允许嵌入**，但**不得改编或二次开发**；随包时"应在软件中特别注明使用了 MiSans 字体" |

逐条落到本项目：

1. **Noto 子集 = 修改版**。OFL 把"删除组件（子集化）"算 Modified Version；RFN 是 `Source`，
   所以子集**不能用 `Source` 命名**。保留 "Noto Sans SC" 严格说没用到 RFN，但属于灰区；
   规范做法是**改名**（如 `MiHome Sans`）后在 `NOTICE.txt` 附 OFL 原文与版权行。
   改名是 `fontTools` 改 name 表的事，**建议在构建期做，不进仓库**，这样仓库里只有原始字体文件。
2. **MiSans 不能子集**（"不得改编"）。只能原样带 → 想三档字重就是 3 × 6.5 MB ≈ 20 MB，
   而且要写明"使用了 MiSans"。**这条基本判死 MiSans**，除非只接受 Regular 一档。
3. **与 GPL-3.0 的关系**：字体是独立文件、各自许可，OFL 与 GPL 不冲突；按仓库规矩要进
   `README.md`「开源致谢」表 + `NOTICE.txt`（`build_exe.py` 里已有 go2rtc 的先例）。

---

## 3. 三档方案与真实成本

### A. 只关彩边 —— **2026-11-16 已落地**

**做法**：新建 `mihome/ui/fonts.py`；`prepare_engine()` 在 `QApplication` 之前把
`QT_QPA_PLATFORM` 拼成 `windows:fontengine=freetype`，`make_font()` 加
`NoSubpixelAntialias`，`app.py` 两条启动路径各加两行（`prepare_engine()` + `apply_to(app)`）。

| 项 | 事前预估 | 落地实测 |
|---|---|---|
| 改动面 | ~8 行、1 个文件 | 新增 `mihome/ui/fonts.py`（74 行，大半是注释）+ `app.py` 换掉 4 处调用（净减 2 行） |
| 体积 / 依赖 | 0 字节、无新依赖 | **0 字节**、无新依赖（freetype 编在 PySide6 的 `qwindows.dll` 里，不用另带 dll） |
| 彩边 | — | 顶栏标题最大 **176 → 9**、平均 **15.9 → 2.0**；深色 **165 → 7 / 17.8 → 5.6**；「正在连接」109 → 21 |
| 笔画最深处落差 | 担心整体变淡 | **不变**：浅色 233、深色 219，前后一模一样——只是边缘过渡从"彩"变"灰" |
| 行高 | 预判 30 → 28 | **没变**：13 px 下 `QFontMetrics.height()` 两侧都是 **16**。当初那个 30→28 是拿 20 px 量的，记错了 |
| 布局 | 担心定高控件位移 1–2 px | **零位移**：前后整窗截图 **99.65 % 像素完全相同**（明暗两档同值），差异只落在文字笔画像素上 |
| 度量 | — | 中文宽 65 = 65；`87%` 27 → 28、`MiHomePanel` 87 → 86（±1 px，没有控件卡在临界） |

**回退**：启动前设 `MIHOME_FONT_ENGINE=default` 即退回 DirectWrite，不改代码。

**新增回归**：`scripts/verify_fonts.py` 钉住平台串拼装（含"不许把 `offscreen` 改成
windows"）、开关与幂等、字号/字体名与 `theme.FONT_STACK` 一致、**QSS 的
`*{font-family}` 不吃掉 `styleStrategy`**。它测不到渲染质量（offscreen 没有字体库），
那部分只能出图看。

### B. 换字体（0.5～1 天，含许可与体积）

| 项 | 内容 |
|---|---|
| 新文件 | `mihome/ui/fonts.py`（~35 行）：定位字体、`addApplicationFont`、返回族名、**失败静默回退雅黑** |
| 改 | `app.py` 两处 → 用 `fonts.install()`；`theme.py` 的 `FONT_STACK` 由族名拼；10 个 `verify_*.py` 的字体常量收敛到同一处 |
| 打包 | `build_exe.py` 加 `stage_fonts()`（照 `stage_go2rtc()` 的写法把字体放到 exe 同级 `fonts/`）+ `NOTICE.txt` 补一段 |
| 体积 | 子集 GB2312 **+3.5 MB**（推荐）／ 全 CJK **+11.9 MB**／ 原样 Noto **+17.8 MB** |
| 许可 | OFL 原文 + 版权行进 `NOTICE.txt`；若子集，族名改掉（构建期改名） |
| 字重 | 建议把基准从 Normal 抬到 **Medium/500**，否则整体偏浅 19% |
| 验证 | 同 A；另外要看**混排**：`F3`、`68%`、`26.5℃`、日志表头「快捷键」、自动化行里的「发送 / 延迟」 |

### C. A + B（做完 A 之后仍嫌字形丑）

成本 = A + B，`fonts.py` 里顺手 `setStyleStrategy(NoSubpixelAntialias)` 即可。
**这是唯一能同时解决"彩边"和"字形观感"的组合。**
实测提醒：A + B 叠起来，Noto 会明显比现在的雅黑**更细**（灰度抗锯齿不吃 ClearType 那点加粗感，
Noto 本身就浅 19%，两件事同向叠加）——所以 B 里"基准字重抬到 500"不是可选项，是必做项。

### 明确不做的

- **为了换字体去改字号体系**（13 px → 14 px 之类）。26 处 `setPixelSize` 牵一发动全身，
  收益最多是"字大一点"，不如直接换字形。
- **不带字体、只把 `FONT_STACK` 改个顺序**（把 `PingFang SC` / `Noto Sans SC` 提前）。
  非 Windows 才命中，Windows 用户看到的还是雅黑，等于没做。

---

## 4. 落地时要踩的坑（动手前读一遍）

1. **两处都要改**：`fonts.FAMILY` 和 `theme.FONT_STACK` 的首项。QSS 的 `*{font-family}`
   会覆盖 app 字体，只改一处会出现"部分控件还是雅黑"。
2. **加载顺序**：现在是
   `fonts.prepare_engine() → QApplication → fonts.apply_to(app) → theme.apply_to_app()`。
   `prepare_engine()` 必须在 `QApplication` 之前（平台插件只读一次环境变量）；
   若做 B，`QFontDatabase.addApplicationFont()` 插在 `QApplication` 与 `apply_to` 之间。
3. **打包路径**：frozen 下字体在 exe 同级 `fonts/`（跟 go2rtc 一样的摆法），源码运行时在
   仓库 `assets/fonts/`。**找不到字体必须静默回退雅黑**并写一行日志，不能让程序起不来。
4. **子集范围要留余量**：GB2312 之外的生僻字（如 `𠮷`）会**逐字回退**系统字体——
   实测能正常显示（宽 13 px），只是字形/宽度和上下文不一致。设备名是用户自己起的，
   名字里出现生僻字是真实概率，别假设不会。
5. **行距变化**：16 → 19（Noto）。定高控件（48 / 52 / 36）与"上下居中"的画法都要肉眼看。
6. **日志窗的列宽是运行时算的**（`_sync_widths` 用 `QFontMetrics`），换字体后列宽会自己变，
   不用改代码，但要看一眼"说明列"是不是被撑太宽。
7. **验证脚本测不到字体渲染**：10 个 `verify_*.py` 自己写死雅黑，改字体后它们仍在测雅黑。
   现在多了 `verify_fonts.py`，但它只测"引擎开关有没有生效、QSS 有没有吃掉策略"，
   **不测字形好不好看**。把换字体当成"没有网"，必须出图 + 真机看。

---

## 5. 回归网的现状（为什么说"没有网"）

把 `FONT_STACK` 和 `app.py` 的字体临时改成 `Noto Sans SC` 后跑**全套 52 个验证脚本：
52/52 通过**。不是"没问题"，是**测不到**——脚本要么自己设回雅黑，要么只断言相对几何
（`QFontMetrics(font).horizontalAdvance(x) + 16` 这类，字体一换等式两边同时变）。

所以换字体的**真实成本主要是肉眼回归**，不是改代码：代码 ~50 行，回归要跑
4 个页签 × 2 主题 × 4 个自动化窗口 × 触发日志窗，并逐项确认没有裁字、没有意外的省略号。

A 落地时补了 `verify_fonts.py`（23 项）填的是**另一类空白**：顺序、环境变量边界、
QSS 与 `styleStrategy` 的关系。这类错误不会报错、只会静默不生效，正适合钉死；
渲染质量它管不了。

---

## 6. 建议的执行顺序

1. ~~**先做 A**（关彩边，0 字节，可回退）~~ —— **2026-11-16 已做**，数据见第 3 节 A。
   剩下的判断只有一条：**你自己看这版灰度字能不能接受**。能接受就到此为止。
2. 只在仍不满意字形时做 B：**Noto VF 子集 GB2312 → 构建期改名 `MiHome Sans` → +3.5 MB
   → 基准字重 500**。这条组合体积最小、许可最干净、字重最全。
   注意 B 是在 A 之上叠加：灰度渲染会在视觉上"减薄"，Noto 又比雅黑浅 19%，
   两件事同向叠加，**字重抬到 500 是必做项**。
3. 不做 MiSans（许可禁改，无法子集，未子集又只有 Regular 一档）。
4. 真要上全字符集，再考虑 11.9 MB 的版本——**先量有没有人真的缺字**，别默认要。

---

## 7. 复现步骤（探针脚本未入库）

调研用的探针都是临时文件（`scripts/out/_tmp_*.py`，已删），要点留在这里：

```bash
# 度量 / 字重 / 缺字回退（需要真实平台插件，不能 offscreen——那里没有字体库）
.venv/Scripts/python.exe -c "from PySide6.QtGui import *; ..."   # QFontMetrics(f).lineSpacing()

# 体积：fontTools 子集 + 实例化（uv 临时环境，不污染 .venv）
uv tool run --from fonttools pyftsubset NotoSansSC-VF.ttf \
  --unicodes="U+0020-007E,U+00A0-00FF,U+2000-206F,U+3000-303F,U+FF00-FFEF,U+4E00-9FFF" \
  --layout-features="kern,liga,vert,vrt2,palt,halt" --name-IDs="*" --output-file=out.otf

# 彩边对照：同一段文字走三种抗锯齿路径，数 |R−B|
QT_QPA_PLATFORM="windows:fontengine=freetype"   # + QFont.NoSubpixelAntialias → 彩边 0
```

色度量化口径：把控件 `grab()` 成 `QImage`，对每个像素取 `abs(R − B)` 求平均——
灰度抗锯齿严格为 0，ClearType 在 20 以上。这个指标可以直接当"毛刺改好了没有"的判据。
