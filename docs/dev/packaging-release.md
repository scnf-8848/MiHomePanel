# 打包与发布

> 专题文档，入口见 [DEVELOPMENT.md](../DEVELOPMENT.md)。
> 本文覆盖：PyInstaller 打包、裁剪与体积、GitHub Releases 发版流程。

## 打包成 exe 的实现

```bash
.venv\Scripts\python.exe scripts\build_exe.py
```

产物是 `dist\MiHomePanel\`（80 MB / 132 个文件，zip 后约 45 MB），**整个目录一起发**，
不是只发那个 exe：

```
dist\MiHomePanel\
  MiHomePanel.exe
  _internal\            Qt / Python 运行时（PyInstaller 收集后裁掉了用不到的部分）
  go2rtc\               摄像头取流组件（外部进程，PyInstaller 不会自动带）
    go2rtc.exe          打补丁的那份 → 别人装完也是首帧约 2 秒
    BUILD.txt           构建记录（自检靠它区分 2 秒 / 5 秒）
    LICENSE             go2rtc 的 MIT 原文（再分发要求）
  LICENSE  NOTICE.txt   本项目 GPL-3.0 + 第三方组件说明
```

脚本结尾会**在产物上**跑两项检查，都过才算打完：

1. `MiHomePanel.exe --self-check`：找得到 go2rtc、`go2rtc -v` 跑得起来、Qt 的 HEVC
   解得了。结果同时落在 `<data>\logs\self-check.txt`（`--windowed` 的 exe 没有可读的
   stdout，看文件）。
2. **窗口冒烟**：按"双击"的方式（`DETACHED_PROCESS`，不带任何标准句柄）再拉一次，
   然后看两件事：窗口标题里有没有 `Unhandled exception`、`<data>\logs\crash.log`
   有没有被重新写出来。任一命中就判失败。**这一步不能省**——自检是带管道跑的，
   stdout 反而是有效句柄，见下面第 3 个坑。

`--console` 打带控制台的版本排障，`--onefile` 打单文件（启动慢，go2rtc 仍要放旁边），
`--skip-check` / `--skip-smoke` 跳过对应检查，`--force-clean` 让 PyInstaller 清缓存
再编（有些环境会拦批量删除）。

上面两项都**不碰摄像头**，所以还得手动真播一次——打包产物里的 Python 是嵌进去的，
外部脚本驱动不了它，所以做成了 exe 自带的开关：

```
dist\MiHomePanel\MiHomePanel.exe --probe-camera            # 跑一次
dist\MiHomePanel\MiHomePanel.exe --probe-camera --rounds 3 # 冷启动三次看区间
```

它起完整界面、找到那台在线摄像头、打开详情页、**点一下播放**，等到画面真的上屏才
停表，报告写 `<data>\logs\probe-camera.txt`（含分辨率，能顺带证明不是黑屏）。`--lane
lan|remote` 只改内存里的通道偏好，不动用户设置。这一步过了才算"别人装完能看到画面"。

本机实测（2026-11-09，`chuangmi.camera.069a01`、补丁版 go2rtc、局域网）：产物上
`3.97 s / 1.98 s（2960×1666）`，源码模式 `3.46 / 1.97 / 0.89`。第一次总是慢一点，
因为 go2rtc 进程要冷启动。

四个只在打包后才暴露的坑，脚本已经兜住，但改打包方式时要留意：

- **go2rtc 不会被自动带上**。缺了它摄像头**完全不可用**（不是慢）——`find_binary()`
  会去 exe 旁边找，找不到就明确报路径。
- **Qt 多媒体后端是插件**。漏收集时程序能启动、日志干净，只有点播放才黑屏；
  `--self-check` 查的就是它。
- **`--windowed` 下 `sys.stdout` 是 `None`**（不是"没输出"，是对象不存在）。任何库在
  导入期碰一下 `sys.stdout.isatty()` 就会让程序在 `import` 阶段崩掉，连窗口都建不起来
  ——mijiaAPI 的彩色日志正好这么干。`mihome/_stdio.py` 在包初始化时给 stdout/stderr
  兜一个 devnull，`ensure_streams()`，别删。
  这个坑很阴：**从终端跑没问题**（Windows 把控制台句柄继承给子进程），只有双击才崩。
  崩溃时 PyInstaller 只弹一句英文 "Unhandled exception in script"，所以打包入口
  （`scripts/build_exe.py` 生成的 `_launcher.py`）另外接住了异常，写
  `<data>\logs\crash.log` 并弹一个中文提示框。
- **子进程的标准输入句柄无效**。窗口化进程没有控制台，`subprocess` 继承到的是空句柄
  （典型症状 `OSError: [WinError 6] 句柄无效`），所以所有 `subprocess.run/Popen` 都显式
  传 `stdin=subprocess.DEVNULL`。

### 体积：119 → 79 MB（2026-11-09）

PySide6 的 PyInstaller hook 是"宁多勿少"：整个 QML / Quick / PDF / 虚拟键盘都会收进来，
哪怕程序一行都没 import。全量 119 MB / 888 个文件，zip 后约 60 MB。

`build_exe.py` 打完会按 `_TRIM` 清单删一遍，实测 **119 → 79 MB / 132 个文件**，zip 45 MB。
（2026-11-11 带云台重测：80 MB / 132 个文件 / zip 45 MB，多出的 1 MB 是云台与叠加控件。）
逐项（按省得多排）：

| 删掉 | 省 | 依据 |
|---|---|---|
| QML / Quick / OpenGL 六个 DLL | 14.7 MB | 只用 Widgets；`Qt6Multimedia` 的静态依赖是 Core / Gui / Network，这一组只互相引用 |
| Qt 翻译只留中文 + 英文 | 6.9 MB | 128 个 `.qm` 里用得上的只有 4 个（程序目前没接 `QTranslator`） |
| `opengl32sw.dll` | 5.3 MB | 软件 OpenGL 后备；默认删，见下面的兼容性说明 |
| `Qt6Pdf.dll` + `qpdf` 图片插件 | 4.4 MB | 只有彼此引用 |
| PIL 的 avif / webp / cms / tk 扩展 | 2.0 MB | 只用来存设备图标 PNG，AVIF 一个就 1.8 MB |
| tiff / webp / icns / tga / wbmp 图片插件 | 1.6 MB | 图标是 PNG/JPEG，PNG 解码内建于 Qt6Gui |
| direct2d / minimal / offscreen 平台插件 | 1.2 MB | 只用 `qwindows`（offscreen 只有开发机截图脚本会设） |
| Qt 的 TLS 插件 | 0.6 MB | 云端走 Python `requests`，Qt 只连本机 go2rtc |
| `tzdata` | 0.5 MB | Windows 上 `tzlocal` 走系统时区 API，不读 zoneinfo 数据 |
| 虚拟键盘 + 输入法插件、TUIO | 0.5 MB | 物理键盘 |

判据不是"看着没用"，是 `scripts/pe_imports.py --who <名字>` 扫一遍产物里所有
`.dll` / `.pyd`，确认**没有任何 PE 静态引用它**：

```
.venv\Scripts\python.exe scripts/pe_imports.py --who Qt6Qml.dll dist\MiHomePanel\_internal
```

换成新版 PySide6 后要重跑一遍：上游合并或拆模块是常事，旧结论会过期。三条边界：

- **插件和它指向的 DLL 要成对删**。Qt 插件是运行时 `LoadLibrary` 按名字找的，导入表里
  看不出"谁要它"——`Qt6Pdf` 能删，是因为唯一引用它的 `qpdf.dll` 一起删了。只删插件会
  留一个 4.4 MB 的死文件，只删 DLL 会让那个插件加载失败。
- **`opengl32sw.dll` 是兼容性保险，不是垃圾**。显卡驱动正常的机器永远用不到它，
  虚拟机 / 远程桌面 / 老驱动才会回退到它。默认删（省 5.3 MB），`--keep-sw-opengl` 保留；
  **要发到那种环境请务必加上**，否则表现是没画面而不是报错。
- **`--no-slim` 是完全不裁的对照版**。怀疑"装完和别人不一样"时用它重打一份二分。

裁剪发生在自检与窗口冒烟**之前**，所以删过头当场就会 FAIL，不会拖到用户机器上。

`go2rtc.exe` 那 21 MB **不要 UPX**。实测：压到 6.1 MB，但冷启动从 80 ms 变成 486 ms
（每次开播都吃这一下），而且 Go 二进制 + UPX 是杀软的高误报组合；更要紧的是分发包
几乎没省——原版 20.6 MB zip 后 8.0 MB，UPX 版 6.1 MB 已经压不动，只差 1.9 MB。

## 发布一个版本（GitHub Releases）

程序自身的 Release 打 **`v<版本>`**（`v0.1.0`），和组件的 **`go2rtc-<ref>`** 是两条线，
同一个仓库各发各的，别混到同一个 tag 上。下面按顺序走，**每一步都拿到证据再进下一步**
（发出去的 tag 不该删了重发：用户手里的 `SHA256SUMS` 会对不上）。

**1. 只改一处版本号**

`mihome/__init__.py` 的 `__version__`。窗口标题（`MainWindow.setWindowTitle`）与
「设置 → 关于」都读它，**别在 UI 里另写字面量**。另外两处是**文字**不是代码：
README「下载与安装」里的 `releases/tag/v0.1.0` 与 `MiHomePanel-v0.1.0.zip`。

`build_exe.py` **不写 exe 版本资源**，所以 `MiHomePanel.exe` 右键「属性 → 详细信息」
里没有版本号。这是已知缺口（TODO「技术债」），不是"版本号没生效"：程序内是对的。

**2. 打包，显式指补丁版 go2rtc**

```powershell
.venv\Scripts\python.exe scripts\build_exe.py --go2rtc dist-go2rtc\go2rtc.exe
```

`--go2rtc` 要写死。不写时 `find_binary()` 会按「程序目录 → 数据目录 → PATH」找，
本机 `<data>\go2rtc\` 若装的是官方版，打出去的包首帧就是 5 秒（脚本会打一行
「本地补丁：没有」提醒，别忽略）。打给虚拟机 / 远程桌面用户再加 `--keep-sw-opengl`。

脚本自带两步门禁：产物 `--self-check` + **无控制台窗口冒烟**。`--skip-check` /
`--skip-smoke` 是排障用的，发版别加。

> **`dist/` 会被整个删掉**：`build_exe.py` 开工先 `rmtree(dist)`，上一版的 zip、
> `SHA256SUMS`、release notes 都会没。notes 另存一份进 `docs/release-notes/`
> （`docs/` 入库，`dist/` 在 `.gitignore` 里），要重发时不用现写。

**3. 在产物上真播一次**（自检不碰摄像头）

```powershell
dist\MiHomePanel\MiHomePanel.exe --probe-camera --rounds 3
```

报告写 `<data>\logs\probe-camera.txt`。看首帧区间和**分辨率**——有分辨率才证明不是黑屏。

**4. 组包 + 校验和**

```powershell
Compress-Archive -Path dist\MiHomePanel -DestinationPath dist\MiHomePanel-v0.1.0.zip -Force
Get-FileHash dist\MiHomePanel-v0.1.0.zip -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash.ToLower())  MiHomePanel-v0.1.0.zip" } |
    Set-Content dist\SHA256SUMS -Encoding ascii
```

- 压缩**目录本身**，不是 `dist\MiHomePanel\*`：要的就是解压后多一层 `MiHomePanel\`
- `SHA256SUMS` 是「小写哈希 + **两个空格** + 文件名」，ASCII 无 BOM；这样
  `sha256sum -c SHA256SUMS` 和 GitHub 页面上肉眼核对都能直接用
- **换工具 / 换顺序就会得到另一个哈希**（v0.1.0 那份是 135 个条目、不带目录条目、
  正斜杠；`Compress-Archive` 会多出目录条目、体积差约 0.1 MB）。所以每次组包都要
  **重算**，不能沿用上一版的 `SHA256SUMS`
- 体积按**三个口径**记：目录 MB、文件数、zip MB。只报一个容易得出"省了 15 MB"
  这种误导结论（zip 后其实只差 1.9 MB）

**5. 写 release notes**（`docs/release-notes/v<版本>.md`，再拷一份到 `dist/` 上传）

四段就够：
- 开头一句「谁该下、怎么用」：免安装、自带取流组件、Windows 10 / 11 x64
- 「本次更新」按**用户能感知的变化**写，内部重构不进这一节
- 「自带组件」点名 go2rtc 的上游 ref、组件 Release 链接、MIT 许可
- 「已知限制」照实写（占位入口、单账号、摄像头只做实时画面 / 声音 / 云台…）

**6. 发布**

```powershell
gh release create v0.1.0 dist\MiHomePanel-v0.1.0.zip dist\SHA256SUMS `
  --title "MiHomePanel v0.1.0" --notes-file dist\release-notes-v0.1.0.md
```

发完核对三件事（资产传错了用 `gh release upload v0.1.0 <文件> --clobber` 补）：

- Release 页**匿名**打开能看到两个资产（登录状态看得见不算数，用户不登录）
- 下载 zip 算一遍 sha256，与 `SHA256SUMS` 一致
- 解压后 `MiHomePanel\go2rtc\BUILD.txt` 在——补丁版标记，`--self-check` 靠它区分 2 秒 / 5 秒

**7. 收尾**

README 下载链接指到新 tag；`TODO.md` 补一条完成记录（体积 / 哈希 / 实测首帧）；
这轮动过的调研结论回填 `docs/reference/README.md`。
