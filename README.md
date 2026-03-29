# keynote-to-remotion Skill

## 这个 Skill 是什么

这是一个把 **Keynote 演示文稿自动转换为 Remotion（React 视频）** 的工具集。

Keynote → HTML 导出 → Python 脚本解析 → Remotion TSX 代码 → 可渲染视频

---

## 人类如何使用（直接调用脚本）

**对人类来说，不需要 AI 参与，直接跑脚本就行。**

### 第一步：从 Keynote 导出

Keynote → File > Export To > **HTML**（不是 PDF）

导出目录结构：
```
keynote-export/
├── header.json          # 幻灯片列表
└── {UUID}/              # 每张幻灯片一个目录
    ├── {UUID}.json      # 动画数据
    └── assets/{UUID}.pdf  # 纹理贴图（PDF 格式，每页一个纹理）
```

### 第二步：搭建项目

```bash
# 在项目根目录创建结构
mkdir my-video && cd my-video

# 把 Keynote 导出内容放到 assets/
ln -s /path/to/keynote-export ./assets

# 复制 Remotion 项目模板
cp -r ~/.claude/skills/keynote-to-remotion/assets/remotion-template ./remotion
cd remotion && npm install && cd ..

# 复制脚本
mkdir transpiler
cp ~/.claude/skills/keynote-to-remotion/scripts/*.py transpiler/
```

### 第三步：运行转换

```bash
# 安装 Python 依赖
pip install pymupdf

# 全量转换（提取纹理 + 生成 TSX + 生成 Root.tsx）
python3 transpiler/transpile.py --base-dir .

# 仅重新生成 TSX（不重新提取纹理，节省时间）
python3 transpiler/transpile.py --base-dir . --skip-extract

# 自定义参数（60fps、竖屏）
python3 transpiler/transpile.py --base-dir . --fps 60 --width 1080 --height 1920
```

### 第四步：预览和渲染

```bash
cd remotion

# 启动预览服务器（浏览器访问 localhost:3000）
npx remotion studio

# 渲染单张幻灯片
npx remotion render --composition=Slide003 --output=out/Slide003.mp4

# 渲染全部幻灯片
python3 -c "
import json; r=json.load(open('../transpiler/slide_registry.json'))
for e in r: print(e['comp_name'])
" | while read comp; do
  npx remotion render --composition=$comp --output=out/$comp.mp4
done
```

---

## AI 如何使用这个 Skill

**对 AI（Claude）来说，Skill 就是一份「预加载的知识文档」。**

当用户说"帮我把 Keynote 转成 Remotion 视频"，Claude 会：
1. 读取 `SKILL.md`，获得完整的脚本位置、使用流程、常见 Bug 表
2. 用 Bash 工具调用 `transpile.py`
3. 如果出现视觉 Bug，查 `SKILL.md` 的 Bug 表定位根因
4. 如果需要修改脚本，直接编辑 `scripts/` 下的 Python 文件

**AI 的优势在于**：遇到 Bug 时可以深入分析 Keynote JSON、像素颜色、坐标系，自动修复脚本并重新生成。

---

## 系统架构与原理

### 整体流程

```
Keynote.key
    │
    ▼ File > Export To > HTML
assets/
├── header.json          ← 幻灯片顺序列表
└── {UUID}/
    ├── {UUID}.json      ← CoreAnimation 动画树（每张幻灯片）
    └── assets/{UUID}.pdf ← 纹理贴图合集

    │
    ▼ Step 1: extract_textures.py
remotion/public/textures/
└── s001_0.png, s001_1.png, ...   ← 每个纹理一个 PNG

    │
    ▼ Step 2: parse_layers.py
Python dict 树（规范化图层结构）

    │
    ▼ Step 3: generate_tsx.py
remotion/src/slides/Slide001.tsx  ← 每张幻灯片一个 React 组件

    │
    ▼ Step 4: Root.tsx 注册
remotion/src/Root.tsx             ← Remotion 入口，注册所有 Composition
```

---

### 脚本详解

#### `extract_textures.py` — 提取纹理

Keynote HTML 导出的纹理不是单张图片，而是**每张幻灯片打包成一个 PDF**，每一页对应一个纹理。

关键逻辑：
- 用 PyMuPDF (`fitz`) 以 `scale=1` 光栅化每一页 → PNG
- **必须用 `alpha=True`**，否则透明区域变白色背景
- 输出文件名：`s{幻灯片序号:03d}_{纹理页序号}.png`
- 同时生成 `texture_map.json`：`{幻灯片UUID → {纹理UUID → 文件名}}`

#### `parse_layers.py` — 解析图层树

Keynote 的动画数据是 CoreAnimation 格式的图层树。每张幻灯片是一棵树，节点包含：

| 字段 | 含义 |
|------|------|
| `initialState.position` | 图层中心点（父坐标系，非左上角） |
| `initialState.width/height` | 尺寸 |
| `anchorPoint` | 变换原点（默认 0.5,0.5 = 中心） |
| `initialState.opacity` | 初始透明度 |
| `initialState.hidden` | 是否在该状态不可见 |
| `animations[]` | 动画数组（每个属性一条） |

**坐标系转换**（最容易出 Bug 的地方）：

Keynote 存储的是「中心点 + 宽高」，CSS 需要「左上角 left/top」：

```python
left = center_x - anchor_x * width   # anchor_x 默认 0.5
top  = center_y - anchor_y * height  # 坐标相对父元素，不是画布
```

**zPosition 提取**（第二大 Bug 来源）：

zPosition **不在** `initialState` 里，只在 CAAnimationGroup 的 sub-animation 里。脚本提取后按 `z_position` 升序排列子节点，确保 DOM 顺序 = 渲染层级。

Magic Move 中 zPosition 会在 FROM/TO 状态互换（例如手指 `from=0.007 to=0.008`，圆形 `from=0.008 to=0.007`），所以用 **TO 值** 排序，反映目标幻灯片的层级。

**hidden 动画处理**（第三大 Bug 来源）：

`hidden=True` 的图层在该状态完全不可见，但 Keynote 仍会为它记录 `opacity` 动画值（内部表示用，不是实际可见性）。规则：

- `hidden: False→False` + `opacity: 0→1` → 正常淡入，按 opacity 动画渲染
- `hidden: True→True` + `opacity: 1→0` → 全程不可见，强制 opacity=0，忽略 opacity 动画

#### `generate_tsx.py` — 生成 React 组件

将解析好的图层树转换为 Remotion TSX 代码。

核心设计：**所有动画都压缩到单一 `progress` 变量（0→1）**。

```tsx
const progress = interpolate(frame, [START, END], [0, 1], {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
  easing: Easing.inOut(Easing.ease),
});
```

每个有动画的属性生成一个插值变量：

```tsx
const L_tx = interpolate(progress, [0,1], [fromX, toX], CLAMP);
const L_ty = interpolate(progress, [0,1], [fromY, toY], CLAMP);
const L_op = interpolate(progress, [0,1], [0, 1], CLAMP);
```

纹理交叉溶解（contents 动画）：

```tsx
const L_cop = interpolate(progress, [0,1], [1, 0], CLAMP);
<Img src={staticFile("from.png")} style={{opacity: L_cop}} />
<Img src={staticFile("to.png")}   style={{opacity: 1 - L_cop}} />
```

时间轴结构（每张幻灯片）：

```
|← HOLD_BEFORE (1s) →|← 动画 (duration_sec) →|← HOLD_AFTER (2s) →|
frame:   0            START                    END               total
```

#### `transpile.py` — 主入口

串联以上三个步骤，同时：
- 读取 `header.json` 获取幻灯片顺序
- 生成 `slide_registry.json`（供批量渲染用）
- 生成 `Root.tsx`（注册所有 Composition）

---

### Keynote Magic Move 原理

Magic Move 是 Keynote 最复杂的过渡类型，效果名为 `apple:magic-move-implied-motion-path`。

每张 Magic Move 幻灯片的 JSON 包含**目标状态的完整图层树**，每个图层同时记录 FROM 和 TO 的属性值：

| 图层类型 | FROM | TO | 动画 |
|---------|------|-----|------|
| 两端都有的元素 | 原位置 | 新位置 | translation + scale + rotation |
| 目标新增元素 | hidden=True, opacity=0 | hidden=False, opacity=1 | 淡入 |
| 源端消失元素 | visible, opacity=1 | hidden=True, opacity=0 | 淡出 |
| 纹理变化的元素 | FROM 纹理 | TO 纹理 | contents 交叉溶解 |

---

## 目录结构

```
keynote-to-remotion/
├── SKILL.md                      # AI 读取的指令文档（流程 + Bug 速查表）
├── README.md                     # 本文档
├── scripts/
│   ├── transpile.py              # 主入口（人类直接调这个）
│   ├── extract_textures.py       # Step 1：PDF → PNG
│   ├── parse_layers.py           # Step 2：JSON → Python dict 树
│   └── generate_tsx.py           # Step 3：dict 树 → TSX 代码
├── assets/
│   └── remotion-template/        # Remotion 项目模板（复制到项目用）
│       ├── package.json
│       ├── remotion.config.ts
│       ├── tsconfig.json
│       └── src/index.ts
└── references/
    ├── coordinate-system.md      # CoreAnimation → CSS 坐标系数学推导
    └── keynote-json-format.md    # Keynote JSON 格式完整说明
```

---

## 常见 Bug 速查

| 现象 | 根因 | 修复位置 |
|------|------|---------|
| 文字/元素跑到左上角 | 跳过了 root_layer 本身，直接遍历子节点 | `generate_tsx.py`：对 root 调用 `_gen_layer(root)` |
| 嵌套元素位置×2 | 把画布坐标当父相对坐标用了 | `parse_layers.py`：坐标必须相对父元素 |
| 缩放/旋转原点错误 | `transform-origin:"center center"` | `parse_layers.py`：用 anchorPoint 算 |
| PNG 有白色背景 | PyMuPDF 用了 `alpha=False` | `extract_textures.py`：改 `alpha=True` |
| 动画元素不显示 | 用了 `display:none` | `generate_tsx.py`：改 `opacity:0` |
| 图层遮挡顺序错 | zPosition 在 SKIP_PROPERTIES 被跳过 | `parse_layers.py`：从 CAAnimationGroup 子动画提取 zPosition |
| Magic Move 手势在圆形下（z 互换） | 用 FROM 值排序，目标状态层级相反 | `parse_layers.py`：zPosition 用 TO 值排序 |
| 幽灵白块扫过画面 / 颜色条闪现 | `hidden=True` 图层的 opacity 动画被照常渲染 | `generate_tsx.py`：`hidden_from=True` 时强制 `op=0` |
| `interpolate` 报 non-monotonic | `round(duration * fps) = 0` | `generate_tsx.py`：`max(1, round(...))` |
