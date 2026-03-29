---
name: keynote-to-remotion
description: Use when converting a Keynote presentation into a Remotion video project, or debugging transpiler visual bugs. Triggers: elements in wrong position, text in top-left corner, wrong scale/rotation origin, white backgrounds on transparent PNGs, animated elements staying invisible, character-by-character animation overlap, position double-counted in nested divs.
---

# Keynote → Remotion Transpiler

## Overview

Automated pipeline: Keynote HTML/Web export → Python scripts → Remotion TSX compositions. Four scripts handle texture extraction, layer parsing, TSX generation, and Root.tsx registration.

**Skill type:** Reference. Use scripts from `scripts/`, consult `references/` for coordinate math.

## When to Use

- Converting a `.key` file to Remotion video (export Keynote as HTML/Web first)
- Debugging visual bugs in transpiled slides (position, opacity, scale, transparency)
- Extending the transpiler to handle new Keynote animation types
- Setting up a new Keynote→Remotion project from scratch

## Prerequisites

```bash
pip install pymupdf          # PyMuPDF for PDF→PNG extraction
# In remotion/ directory (uses package.json from assets/remotion-template):
npm install
```

Keynote must be exported via **File > Export To > HTML** to produce the web format.

## Project Structure

```
project/
├── assets/                  # Keynote web export output (do not modify)
│   ├── header.json          # {"slideList": ["UUID", ...]}
│   └── {UUID}/              # One per slide
│       ├── {UUID}.json      # Animation + layer data
│       └── assets/{UUID}.pdf  # Texture pages (one PDF, multiple pages)
├── remotion/                # Copy from assets/remotion-template/
│   ├── package.json
│   ├── public/textures/     # PNG textures (generated)
│   └── src/
│       ├── index.ts
│       ├── Root.tsx         # Generated
│       └── slides/          # Generated Slide{N:03d}.tsx files
└── transpiler/              # Scripts from skill scripts/ directory
    ├── parse_layers.py
    ├── generate_tsx.py
    ├── extract_textures.py
    └── transpile.py
```

## Quick Start

`SKILL_DIR` = path to the unpacked skill directory (e.g. `~/.claude/skills/keynote-to-remotion`).

```bash
# 0. Export from Keynote: File > Export To > HTML
#    This creates a directory with header.json + per-slide UUID folders.
#    Place or symlink that export as ./assets/ in your project root.

# 1. Set up Remotion project (first time only)
cp -r $SKILL_DIR/assets/remotion-template ./remotion
cd remotion && npm install && cd ..

# 2. Copy transpiler scripts
mkdir -p transpiler
cp $SKILL_DIR/scripts/*.py transpiler/

# 3. Run full pipeline (extract textures + transpile + generate Root.tsx)
#    Default: fps=30 width=1920 height=1080. Override for other Keynote sizes:
#    python3 transpiler/transpile.py --base-dir . --fps 30 --width 1920 --height 1080
python3 transpiler/transpile.py --base-dir .

# 4. Preview in Remotion Studio
cd remotion && npx remotion studio

# 5. Render a specific slide
cd remotion && npx remotion render --composition=Slide041 --output=out/Slide041.mp4

# 5b. Render all slides (uses registry — works for any slide count)
cd remotion && python3 -c "
import json; r=json.load(open('../transpiler/slide_registry.json'))
for e in r: print(e['comp_name'])
" | while read comp; do
  npx remotion render --composition=$comp --output=out/$comp.mp4
done
```

## Pipeline Steps

### Step 1: Extract Textures

`extract_textures.py` reads each slide's PDF (one PDF per slide, pages = individual textures)
and rasterizes each page as PNG to `remotion/public/textures/s{N:03d}_{idx}.png`.

**Critical rules:**
- `alpha=True` — preserves transparency. `alpha=False` composites over white (WRONG).
- `fitz.Matrix(1, 1)` — scale=1 matches CSS pixel dimensions exactly (verified 1:1 ratio).
- Outputs `transpiler/texture_map.json`: `{slide_id → {tex_uuid → "textures/s{N}_{i}.png"}}`

### Step 2: Parse Layers

`parse_layers.py` walks the layer tree converting CoreAnimation coordinates to CSS-ready values.

**Critical rules (see `references/coordinate-system.md` for full math):**
- Coordinates are **parent-relative**, not canvas-space. CSS `position:absolute` is relative
  to the nearest positioned ancestor, not the canvas root.
- `left = position.pointX - anchorPoint.pointX * width` (parent-relative top-left)
- `anchorPoint` → `transform-origin` in CSS. Can exceed 1.0 (external anchor) — CSS handles >100% correctly.
- Emit root_layer as its own div — don't skip it. Some slides have root offset from (0,0).
- **z_position MUST be extracted from CAAnimationGroup sub-animations** — it is NOT in
  `initialState`. Children are sorted ascending by `z_position` before DOM emission.
  Skipping this causes foreground layers (higher z) to appear behind background layers (lower z).
  In magic-move slides the TO-state background (z=0) is often last in the JSON array but must
  render FIRST in DOM (behind the animating content at z=0.001). Without sorting, the background
  covers the content, producing a "horizontal line" sweep artifact as the content edge moves.

### Step 3: Generate TSX

`generate_tsx.py` collapses all animations to a single `progress` variable (0→1).

Generated per-slide pattern:
```tsx
const progress = interpolate(frame, [START, END], [0, 1], { ...CLAMP, easing: ... });
const L_tx = interpolate(progress, [0,1], [from, to], CLAMP);
// ...
<div style={{position:"absolute", left:X, top:Y, width:W, height:H,
             transform:`translate(${L_tx}px,${L_ty}px)`,
             transformOrigin:"49.44% 37.7%"}}>
  <Img src={staticFile("textures/s035_2.png")} style={{width:"100%",height:"100%"}} />
</div>
```

For contents (texture cross-dissolve):
```tsx
const L_cop = interpolate(progress, [0,1], [1,0], CLAMP);
<Img src={staticFile("textures/from.png")} style={{opacity:L_cop}} />
<Img src={staticFile("textures/to.png")}   style={{opacity:1-L_cop}} />
```

### Step 4: Generate Root.tsx

Registers all slide compositions with correct `durationInFrames`. Generated automatically by `transpile.py`.

## Common Bugs & Fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Text in top-left corner | Root layer's canvas position skipped | Call `_gen_layer(root_layer)` not `for child in root_layer['children']` |
| Nested elements at 2× position | Canvas-space coords in CSS nested divs | Use parent-relative coords only |
| Wrong scale origin / misaligned after scale | `transform-origin:"center center"` | Use `anchorPoint`: `"{ax*100}% {ay*100}%"` |
| White backgrounds on transparent PNGs | `alpha=False` in PyMuPDF | Use `alpha=True` |
| Animated elements stay invisible | `display:none` on initially-hidden layers | Use `opacity:0` instead — `display:none` blocks descendant animations |
| Container opacity:0 blocks children | Used `initialState.opacity` for containers | Use `animation.from_val` (may be 1.0 even if `initialState.opacity=0`) |
| Ghost elements visible during transition (white blocks sweep, colored bar appears mid-animation) | `hidden=True→True` in leaf animation, but `opacity 1→0` anim was applied anyway — `hidden=True` should force opacity:0 regardless of opacity anim value | In `generate_tsx.py`, parse the `hidden` animation's `from_val`/`to_val`. If `hidden_from=True`, force `op_from=0`; if `hidden_to=True`, force `op_to=0`. Skip opacity animation for hidden states. |
| `interpolate` error: non-monotonic inputRange | `round(duration_sec * fps) = 0` | `max(1, round(duration_sec * fps))` |
| Foreground layer appears behind background (gesture under circle, green bar, magic-move shows horizontal sweep line) | **`zPosition` was in `SKIP_PROPERTIES`** — never read, DOM order used instead. JSON array order ≠ z-order. TO-state background (z=0) is last in array → rendered last in DOM → covers animating content (z=0.001) | Extract `zPosition` scalar from CAAnimationGroup sub-animations in `_get_property_anims()`; store as `z_position` in layer dict; **sort `children` ascending by `z_position`** before emitting DOM. See `parse_layers.py`. |
| Finger still under circle after first fix (or any z-swap after magic move) | **Magic Move swaps `zPosition` between FROM and TO states** — finger: `from=0.007 to=0.008`, circle: `from=0.008 to=0.007`. First fix used `from` value → circle still on top (FROM-state order). Destination slide is what users see. | Use **`to_val`** (not `from_val`) for `z_position` in `_get_property_anims()`, falling back to `from_val` only when `to` is absent. This ensures DOM order matches the destination slide's stacking. |
| Slide shows black background (transparent textures) | `backgroundColor: "#000000"` hardcoded in template. Slide backgrounds are often transparent PNGs (the white is a Keynote theme color, not stored in the texture). Black shows through. | Change `backgroundColor` to `"#ffffff"` in `generate_tsx.py` template. |
| Magic Move: elements don't enter/exit canvas (stay at Slide5 position throughout) | Keynote stores outer containers at the DESTINATION (Slide5) position in the effect's baseLayer. The FROM position (Slide4) is NOT encoded — only the inner element's scroll offset is stored. For elements entering from below canvas, the transpiler must read the previous slide's JSON to get the Slide4 outer-container position, then animate the outer container from Slide4 → Slide5. | In `transpile.py`, load prev slide's JSON for magic-move slides. Call `get_prev_slide_positions(prev_json)` from `parse_layers.py` to extract non-canvas-wrapper element positions. Inject as `magic_move_from_left`/`from_top` on each root child. In `generate_tsx.py`, generate `{p}_ctx`/`{p}_cty` vars and add outer-container translate animation. |

## Animation Properties

| Keynote property | CSS equivalent |
|----------------|---------------|
| `transform.translation` | `translate(tx, ty)` — in **parent** coordinate space |
| `transform.scale.x/y/xy` | `scaleX(sx) scaleY(sy)` |
| `transform.rotation.z` | `rotate({rz}rad)` (Keynote uses radians) |
| `opacity` | CSS `opacity` |
| `contents` | Two `<Img>` with complementary opacity cross-dissolve |
| `hidden` | `opacity:0` (NEVER `display:none`) |

CAAnimationGroup nesting: animations can live in `layer.animations[0].animations[]`. `parse_layers.py` flattens both levels.

## Integration with remotion-best-practices

Generated TSX files are standard Remotion compositions. Combine with:
- `spring()` for physics-based animations on top of transpiled animations
- `<Sequence>` for staggered per-element timings (current transpiler collapses all to one `progress`)
- `<Audio>` for voiceover
- Custom Remotion components replacing specific slides entirely

## References

- `references/keynote-json-format.md` — Full JSON schema: events, effects, layers, assets, animation values
- `references/coordinate-system.md` — CoreAnimation → CSS math, anchorPoint edge cases, all known gotchas
- `assets/remotion-template/` — Remotion project scaffold (package.json, tsconfig.json, remotion.config.ts, src/index.ts)
- `scripts/` — Four Python scripts: parse_layers, extract_textures, generate_tsx, transpile (main entry point)
