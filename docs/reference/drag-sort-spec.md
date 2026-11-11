# 拖动排序实现方案（平台无关版）

> 目标：让另一个 AI 在**非 HTML 程序**（桌面 / 移动 App / Canvas / 游戏引擎 / 自绘 UI）里复刻本项目的拖动排序效果。
> 本文只描述「行为契约 + 状态机 + 几何算法 + 伪代码」，不依赖 DOM API。括号内标注本项目 Web 实现的对应做法，便于对照。
>
> 索引：`docs/reference/README.md`（状态 = **已落地**）。本程序里的落点是
> `mihome/ui/dnd.py`（拖拽骨架：阈值 / 长按 / 幽灵 / 帧合并）与 `mihome/ui/wall.py`
> （分区与卡片的落位、`set_drag_placeholder`），见 `docs/DEVELOPMENT.md`「排序与隐藏设备」。

---

## 1. 效果契约（先定义验收标准）

拖动全程分四段，任何一段缺失都会让手感变差：

| 阶段 | 视觉 / 行为 | 要点 |
|---|---|---|
| 按下（Pending） | 源项**不动**，只记录起点 | 不能一按就变拖拽，否则列表无法滚动 |
| 激活（Dragging） | ① 源项变淡（本项目 opacity 0.4）或直接从布局中移除<br>② 指针下方出现**幽灵**：与源项等大的克隆，浮在所有内容之上<br>③ 源项原位置变成**占位块**：空白 + 绿色虚线描边 + 淡绿填充<br>④ 其余项**实时让位**，松手前就能看到排序后的样子 | 幽灵不参与布局，占位块参与布局 |
| 移动（Tracking） | 幽灵跟随指针，且**保持按下时的抓握点**（不瞬移到指针中心）<br>占位块随指针在列表中换位 | 幽灵必须 `pointer-events: none`（不可命中），否则命中测试永远打到自己 |
| 松手（Drop） | 只做一次数据移动（`splice` / `move`），然后清理幽灵与占位块，重新渲染 / 重排 | 落位下标必须现算，不能提前缓存 |

**统一视觉参数（可直接照搬）**

- 幽灵：2px 实线描边 `#4ade80`，不透明度 0.85，投影 `0 8px 24px rgba(0,0,0,.5)`，最顶层（本项目 z-index 9999）
- 占位块：2px **虚线** `#4ade80`，填充 `rgba(74,222,128,.08)`，圆角与源项一致，不可命中（pointer-events: none）
- 源项拖拽态：opacity 0.4
- 幽灵里隐藏交互件：删除按钮、状态按钮、次数角标、名字浮层（避免出现"能点但不能点"的假控件）

---

## 2. 组件拆分

```
DragController        手势状态机：按下 / 阈值 / 长按 / 移动 / 松手 / 取消
  ├─ GhostLayer       幽灵层：最高 z 序，独立于布局
  ├─ Placeholder      占位块：插入真实布局容器的一个特殊子项
  └─ DropResolver     落位计算：命中测试 → 目标 index
DataSource            真正的有序数据（数组 / 列表模型）
Renderer              按数据渲染；落位后重排
```

关键约束：**占位块不是数据**。它是渲染层临时插入的"假兄弟节点"，数据只在松手时改一次。这样取消拖拽时只需删掉幽灵和占位块，数据天然没被污染。

---

## 3. 手势状态机

```
IDLE
 └─(按下, 命中可拖项)→ PRESSED
      ├─(鼠标: 位移 ≥ 8px)─────────────→ DRAGGING
      ├─(触屏: 持续 220ms 未移动)───────→ DRAGGING
      ├─(触屏: 位移 > 10px, 未到 220ms)→ IDLE (判定为滚动，交还滚动手势)
      └─(松开 / pointercancel)─────────→ IDLE (无任何视觉变化)

DRAGGING
 ├─(移动) → 更新幽灵位置 + 更新占位块位置
 ├─(松开) → DROP → 提交数据 → CLEANUP → IDLE
 └─(pointercancel / 第二根手指按下 / 窗口失焦) → CANCEL → CLEANUP → IDLE
```

常量（本项目实测值）：

| 常量 | 值 | 作用 |
|---|---|---|
| `DRAG_THRESHOLD` | 8 px | 鼠标位移阈值，避免手抖误触 |
| `TOUCH_HOLD_DELAY` | 220 ms | 触屏长按确认拖拽，区分"滑列表"与"拖东西" |
| `TOUCH_SCROLL_CANCEL` | 10 px | 长按确认前位移超过它 → 判定为滚动，取消拖拽 |

### 3.1 触屏必须遵守的三条

1. **激活前不要拦截 move 事件**（Web 上不要提前 `preventDefault`，否则面板滚不动）。
2. 激活后才锁滚动（Web：`touchmove` + `preventDefault`）。
3. 拖拽把手区域设 `touch-action: none`（本项目 `.team-handle`），让系统滚动手势不抢。

### 3.2 双通道

- 鼠标：位移阈值即激活，按下左键才响应（`button === 0`）。
- 触屏：定时器长按激活；激活前记录位移，超阈值视为滚动。
- 另有"立即生效"通道（本项目用于拉伸分配块）：不判阈值、不等长按，按下即进入 DRAGGING。

---

## 4. 幽灵（Ghost）实现

平台无关的做法是**快照**：

1. 激活时对源项做一次视觉快照（DOM 克隆 / `drawViewHierarchy` / RenderTexture / Canvas 重绘）。
2. 快照尺寸严格等于源项在屏幕上的包围盒（宽、高都显式设置，不要靠内容撑开）。
3. 快照放进**最顶层 overlay**，与列表布局完全解耦（Web：`position: fixed`）。
4. 每帧只改 overlay 的位置，不做重排、不做重新渲染。

### 4.1 抓握点偏移（避免瞬移）

激活瞬间记录指针在源项内的相对位置，之后幽灵位置 = 指针 − 该偏移：

```
grabX = clamp(pointerX - sourceRect.left, 0, sourceRect.width)
grabY = clamp(pointerY - sourceRect.top,  0, sourceRect.height)
ghostX = pointerX - grabX
ghostY = pointerY - grabY
```

**不要**把幽灵放在指针中心：长条形的行被"居中"后，视觉上会明显跳一下。

### 4.2 源项处理（两种流派，选一种）

| 流派 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A（本项目） | 源项 `display:none`（从布局中摘除），占位块接替其位置 | 其它项立刻重排，预览干净 | 源项消失，需要占位块补位 |
| B | 源项保留但 opacity 0.2 | 实现简单 | 会同时出现"源项占位"和"占位块"两份空位，需二选一 |

推荐 A：`source.hidden = true`（或等价的"从布局流移除"），再用占位块补位，这样"空位"只有一个，位置由占位块决定。

---

## 5. 占位块（Placeholder）实现

- 尺寸 = 被拖项的包围盒（宽高都显式设定，网格里同样要宽高）。
- 作为**真实布局容器的子项**插入（与其它项同级），由布局引擎自然把后续项挤开。
- 不可命中、不可聚焦、不可点击（Web：`pointer-events: none`）。
- 更新逻辑：算出目标 index → `insertBefore(placeholder, items[index] ?? null)`，即"插到第 index 个**其它项**之前"。
- 清理时移除；被拖项恢复显示。

**为什么用"插入真实子项"而不是覆盖层画框？**
覆盖层只能画一个框，不能让其它项让位；而插入子项能直接复用列表 / 网格的布局算法，得到"松手后的真实预览"，零额外布局代码。

---

## 6. 落位计算（核心算法）

### 6.1 单列纵向列表 —— 中线规则

```
输入：指针 y；列表容器；被拖项 source
items = 容器内所有项，排除 source 与 placeholder
idx = items.length
for i in 0..items.length-1:
    if pointerY < items[i].top + items[i].height / 2:
        idx = i
        break
placeholder 插到 items[idx] 之前
targetIndex = idx
```

**为什么这个 idx 可以直接用于 `splice`**：因为源项已从布局中移除（visibility 为 hidden / 不在 items 里），`items` 就是"删掉源项后的数组"，`idx` 正是插入位置。松手时：

```
from = data.findIndex(x => x.id === payload.id)   // 现查，不用渲染时缓存的下标
if from < 0 or target == from: 不做任何事
moved = data.removeAt(from)
data.insert(target, moved)
```

> 本项目用户列表即此规则（`userDragUpdatePlaceholder`）。

### 6.2 网格（列优先填充）—— 最近列 + 最近行

不能把网格拍平成"线性列表 + 中线"，否则跨列时会把「左列最后一行」错判成「右列第一行」。正确做法：

```
读容器几何：
  colWidth  = 第一列宽
  rowHeight = 第一行高
  colGap, rowGap, padLeft, padTop, padRight, padBottom
  numCols   = 列数
  perCol    = 每列行数（由布局阶段写入的固定值）
  n         = 总项数（含被拖项）

各列容量：
  cap[k] = perCol                        (k < numCols - 1)
  cap[k] = max(0, n - (numCols-1)*perCol) (最后一列，可能未填满)

选列：遍历有容量的列，取 |pointerX - 列中心X| 最小者
     列中心X = rect.left + padLeft + hOffset + k*(colWidth+colGap) + colWidth/2
     hOffset = 当 justify-content:center 且有多余宽度时 = 剩余宽度/2
               （否则所有列整体偏移，落位会算偏，必须补）

选行：row = round((pointerY - (rect.top + padTop + vOffset + rowHeight/2)) / (rowHeight+rowGap))
     vOffset = 当 align-content:center 且有多余高度时 = 剩余高度/2
     row = clamp(row, 0, cap[col]-1)

线性下标 = clamp(col * perCol + row, 0, n-1)
```

> 本项目队伍网格即此规则（`teamDragTargetIndex`）。注释里明确写了：落位按「列 × 行」计算，避免左列末行误判为右列首行。

### 6.3 槽位 / 交换（拖到某个格子）

- 命中测试：用指针坐标做**点命中**（Web：`elementFromPoint` + `closest('.slot')`；Canvas：遍历矩形求交，取最上层）。
- 语义：目标格有内容 → 交换；无内容 → 放入。同一容器内的"重复角色"等业务约束在提交前校验，不满足则**什么都不做**（占位/高亮照常清理）。
- 原地松手（源 == 目标）直接返回，不做无意义写入。

---

## 7. 提交、取消、清理

### 7.1 提交（松手）

1. 读 `targetIndex`（若为 null 则视为无效落位）。
2. 用**稳定 id** 现查源项下标（列表可能已被增删 / 复用节点）。
3. `from === to` 或 `to` 越界 → 不写数据。
4. 只做一次 `removeAt(from)` + `insert(to, moved)`（移动语义），然后重渲染 / 重排。

### 7.2 清理（无论成功、失败、取消都必须执行）

```
cleanup():
    解绑 move / up / cancel 监听
    清长按定时器
    移除全局拖拽态（Web: body 的 pointer-dragging 类）
    移除幽灵（ghost = null）
    恢复源项显示（hidden / display / opacity）
    移除占位块
    清高亮（目标格高亮、释放区高亮遮罩）
    重置 Ctrl 上下文（pointerId、source、activated）
```

清理顺序有个坑：本项目是「**先清理，再落位**」，但落位钩子仍要读 `payload`（它记录了从哪来 / 拖的是什么），所以 `payload` 延后到落位之后再置空。其它平台的等价做法：清理只清视觉与监听，payload 由调用方持有。

### 7.3 吞掉松手带出的 click

松手会触发一次 click（Web）。若行本身有点击语义（如"切换到该用户"），必须吞掉：

```
激活时：suppressClick = true
click 处理开头：if (suppressClick) { suppressClick = false; return; }
每次按下前：suppressClick = false   // 防止上一次残留标记吞掉正常点击
```

其它平台同理：把"拖拽结束"与"点按"用同一个标志位互斥，并且在每次按下时重置该标志。

### 7.4 取消（CANCEL）

`pointercancel`、窗口失焦、第二根手指按下、Esc → 直接 CLEANUP，**不写数据**，源项回到原位，视觉无残留。

---

## 8. 高频事件处理

指针事件频率常高于帧率（高刷鼠标、触屏抖动）。移动阶段的"重排 + 写位置"合并到每帧一次：

```
push(v):   pending = v;  if !rafScheduled: rafScheduled = true; requestFrame(flush)
flush():   rafScheduled = false; v = pending; pending = null; apply(v)
flushNow(): 松手前把 pending 立刻落地（取消帧回调），否则最后一帧丢失，
           拉伸 / 最终宽度会少算一帧
```

每帧内只做：改幽灵坐标 + 算一次 index + 一次 `insertBefore`（index 没变就不动 DOM）。

---

## 9. 各平台落地映射

| 概念 | Web / DOM | Canvas / 自绘 | Flutter / Compose / RN | 桌面 GUI（WPF/Qt） | 引擎（Unity/UE） |
|---|---|---|---|---|---|
| 幽灵 | 克隆节点 + `position:fixed` 顶层 | 快照纹理，最后绘制 | Overlay 层 | 顶层 Adorner / 浮层 Window | 顶层 Canvas / Widget |
| 占位块 | 插入真实子节点（虚线样式） | 布局模型里插入一个空占位项参与排版 | 列表数据源插入"占位项"或使用 `Sliver` 空位 | 布局容器插入占位元素 | 布局组插入占位节点 |
| 命中测试 | `elementFromPoint` | 逆序遍历矩形求交 | `hitTest` / 手动矩形 | `InputHitTest` | 射线 / 矩形求交 |
| 源项从布局移除 | `display:none` | 布局数据中标记 hidden | `Visibility(visible:false)` | `Collapsed` | `SetVisibility(false)` |
| 帧合并 | `requestAnimationFrame` | 渲染循环 | 下一帧回调 | Dispatcher / CompositionTarget | Tick |
| 提交 | 数组 splice + 重渲染 | 模型 splice + 重排版 | `setState` / 不可变列表 | 集合 Move + 刷新 | 容器数组 Move |

**迁移时唯一不能省的三件事**：①幽灵不参与布局；②占位块参与布局；③落位下标按"移除源项后"的数组语境计算。

---

## 10. 边界情况清单（实现后逐条自测）

- [ ] 鼠标按下不动 → 无任何视觉变化，只是普通点击
- [ ] 鼠标位移 8px 内松开 → 不进入拖拽，正常 click
- [ ] 触屏长按 220ms → 进入拖拽；长按前滑动 → 正常滚动，不拖
- [ ] 拖动中快速抖动 / 高刷 → 不重复重排，不闪
- [ ] 拖到自己原位置松手 → 数据不变，无多余重渲染
- [ ] 拖到列表外（容器上/下/左/右）松手 → 不写数据，或按产品定义落两端
- [ ] 列表只有 1 项 → 占位块与自身重合，松手无变化
- [ ] 拖拽过程中源项被外部删除 / 列表被重渲染 → 落位时按 id 找不到 → 安全退出
- [ ] 多指同时按下 → 先取消上一次，避免监听器叠加
- [ ] 拖拽中窗口失焦 / 系统手势打断 → `cancel`，视觉无残留
- [ ] 松手后紧跟的 click 不被误判为"点击该项"
- [ ] 网格：左列最后一行 ↔ 右列第一行，落位正确
- [ ] 网格：最后一列未填满时，落位被钳制在容量内
- [ ] 容器 `padding` / `gap` / 居中偏移全部纳入坐标换算（否则整列/整行偏一截）
- [ ] 幽灵覆盖层必须不拦截命中（否则 `elementFromPoint` 永远返回幽灵）

---

## 11. 核心伪代码（可直接交给实现方）

```pseudo
// ---------- 控制器 ----------
onPointerDown(e, source, payload):
    if alreadyDragging: cancel()
    ctx = {pointerId, payload, sourceEl, startX, startY, isTouch, activated:false}
    bind move/up/cancel
    if isTouch:
        holdTimer = after 220ms: if !activated: activate(startX, startY)
    else:
        await move

onMove(e):
    if e.pointerId != ctx.pointerId: return
    ctx.x, ctx.y = e.x, e.y
    if ctx.activated:
        applyGhostAndPlaceholder(ctx.x, ctx.y); return
    if ctx.isTouch:
        if holdTimer exists and dist(dx,dy) > 10:
            clearTimer(); cleanup()          // 判定为滚动
        return
    if dist(dx,dy) >= 8: activate()

activate(x, y):
    ctx.activated = true
    setGlobalDragging(true)
    rect = sourceEl.bounds
    ctx.grabX = clamp(x - rect.left, 0, rect.width)
    ctx.grabY = clamp(y - rect.top,  0, rect.height)
    ctx.ghost = snapshot(sourceEl)            // 显式设置宽高
    showGhostAboveAll(ctx.ghost)
    sourceEl.hidden = true                   // 从布局移除
    ctx.placeholder = createEmptyBox(rect.w, rect.h, dashedGreen)
    container.addChild(ctx.placeholder)
    applyGhostAndPlaceholder(x, y)
    lockScroll()                             // 仅触屏

applyGhostAndPlaceholder(x, y):
    ghost.x = x - ctx.grabX;  ghost.y = y - ctx.grabY
    idx = resolver.resolve(x, y)
    if idx != ctx.targetIndex or placeholder not placed:
        ctx.targetIndex = idx
        container.insertBefore(placeholder, others[idx] ?? null)

onPointerUp(e):
    if e.pointerId != ctx.pointerId: return
    pendingVisual.flushNow()
    savedPayload = ctx.payload
    cleanup(keepPayload = true)
    if ctx.wasActivated:
        commit(savedPayload, ctx.targetIndex)
    ctx.payload = null

cleanup():
    unbind(); clearTimer(); setGlobalDragging(false)
    ghost?.remove(); ghost = null
    if sourceEl: sourceEl.hidden = false
    placeholder?.remove(); placeholder = null
    clearHighlights()
    reset pointerId/sourceEl/activated

commit(payload, to):
    if to == null: return
    from = data.findIndex(item => item.stableId == payload.stableId)
    if from < 0 or to == from or to >= data.length: return
    data.insert(to, data.removeAt(from))
    rerender()
```

```pseudo
// ---------- 单列 resolvers ----------
resolveSingleColumn(y, container, source, placeholder):
    items = container.children.filter(it => it != source && it != placeholder)
    for i in 0..items.length-1:
        r = items[i].bounds
        if y < r.top + r.height/2: return i
    return items.length

// ---------- 网格 resolvers（列优先） ----------
resolveGrid(x, y, container, source, placeholder, n, perCol):
    g = readLayout(container)   // cols, rows, gap, padding, centering offsets
    numCols = g.cols.length
    cap[k] = (k < numCols-1) ? perCol : max(0, n - (numCols-1)*perCol)
    col = argmin over k with cap[k] > 0 of abs(x - centerX(k))
    row = round((y - (g.top + g.padTop + g.vOffset + g.rowH/2)) / (g.rowH + g.rowGap))
    row = clamp(row, 0, cap[col]-1)
    return clamp(col * perCol + row, 0, n-1)
```

---

## 12. 对照实现（Web 版参考，**外部仓库，不在本仓库内**）

> 下表路径属于产出这份规格的 Web 项目，本仓库里没有 `js/` 目录，点开是空链接。
> 本程序的落地位置在文件头的「索引」行：`mihome/ui/dnd.py` + `mihome/ui/wall.py`。

| 内容 | 位置 |
|---|---|
| 通用拖拽骨架（阈值 / 长按 / 幽灵 / 清理 / 帧合并） | [drag.js](../js/drag.js) |
| 单列用户行排序（中线规则、吞 click） | [app.js](../js/app.js#L87-L172) |
| 队伍网格排序（最近列 + 最近行、列容量、居中偏移） | [page-team.js](../js/page-team.js#L134-L248) |
| 槽位拖放 / 交换校验 | [page-team.js](../js/page-team.js#L28-L132) |
| 幽灵 / 占位块 / 高亮样式 | [styles.css](../styles.css#L570-L608)、[styles.css](../styles.css#L396-L408)、[styles.css](../styles.css#L711-L732) |
