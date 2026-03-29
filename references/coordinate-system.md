# Coordinate System & CSS Mapping

## CoreAnimation Model → CSS Model

### Position in CoreAnimation

In CoreAnimation (Keynote's rendering engine):
- `layer.position` = the point in **parent coordinate space** where the layer's `anchorPoint` is located
- `layer.anchorPoint` = normalized point within the layer (0,0=top-left, 1,1=bottom-right) — default (0.5, 0.5)
- `layer.bounds` = size of the layer in its own local coordinate space

The top-left corner of a layer in parent coordinates:
```
left = position.pointX - anchorPoint.pointX * width
top  = position.pointY - anchorPoint.pointY * height
```

### CSS Mapping

| CoreAnimation | CSS |
|-------------|-----|
| `left` computed above | `position: absolute; left: {left}px` |
| `anchorPoint` | `transform-origin: {ax*100}% {ay*100}%` |
| `transform.translation` | CSS `translate(tx, ty)` |
| `transform.scale.x/y` | CSS `scaleX(sx) scaleY(sy)` |
| `transform.rotation.z` | CSS `rotate({rz}rad)` |
| `opacity` | CSS `opacity` |

### CSS Transform Order

Keynote applies: **translate AFTER scale** (both from anchorPoint in parent space).

In CSS: `transform: translate(tx, ty) scaleX(sx) scaleY(sy)` achieves the same result
when combined with the correct `transform-origin`.

Proof that `translate(tx) scaleX(sx)` with `transform-origin: ox%` gives the correct position:
```
canvas_left_of_element = css_left + ox*(1-sx) + tx
                       = (pos_x - ax*w) + (ax*w)*(1-sx) + tx
                       = pos_x - ax*w*sx + tx     ← matches CoreAnimation formula
```

### CRITICAL: Parent-Relative Coordinates

**WRONG approach (canvas-space):** Compute canvas-space left/top for every layer and use as CSS
→ For a nested div, CSS left/top is relative to parent div, not canvas
→ Nested elements appear at `parent_canvas_pos + child_canvas_pos` (double-counted)

**RIGHT approach (parent-relative):** Each layer's left/top is relative to its parent div only
→ Root layer emits a div at its canvas position
→ Children inside that div use their LOCAL coordinates (relative to parent)

```python
# CORRECT:
left = position.pointX - anchorPoint.pointX * width
top  = position.pointY - anchorPoint.pointY * height
# These are parent-relative (CSS-correct)
```

### Root Layer Canvas Position

The animation's `baseLayer` (root of the layer tree) CAN have a non-zero position on canvas.
For character-animation slides (slides 09-16), root is typically at ~(238, 348) — not (0,0).

**WRONG:** Skip root, iterate its children directly → children land at (0,0) on canvas
**RIGHT:** Emit root as a positioned div, children positioned inside it

```python
# In generate_slide_tsx:
_gen_layer(root_layer, ...)  # NOT: for child in root_layer['children']: _gen_layer(child, ...)
```

## AnchorPoint Edge Cases

### Normal case: anchorPoint in [0,1] range
Most layers: anchorPoint = (0.5, 0.5) → transform-origin = "50% 50%"

### External anchor point: anchorPoint > 1.0
Some character-animation layers have anchor points like (0.885, 0.462) or even (4.426, 0.545).
This means the transform pivot is OUTSIDE the element's bounds.

CSS `transform-origin: 442.6% 54.55%` is valid — CSS accepts percentages >100%.
The math still works correctly: `442.6% of 9px = 39.83px` from left edge.

This enables two adjacent characters to share the same canvas anchor point:
- "设": element 13px wide, anchor at 88.5% → canvas x_anchor = left + 11.5
- "计": element 13px wide, anchor at 11.5% → canvas x_anchor = left + 1.5
When both share the same canvas anchor, they scale outward from that shared point — correct "split apart" animation.

## Opacity: initialState vs animation.from

Keynote stores TWO opacity values:
- `initialState.opacity`: opacity BEFORE animation starts (pre-animation state)
- `animation.from.scalar`: opacity AT animation start (may differ from initialState)

For container layers, `initialState.opacity` may be 0 but the animation from=1.0, to=1.0
meaning children are always visible. Using `initialState.opacity` for the CSS would make the
container `opacity:0` blocking all children.

Rule:
```python
if animated_opacity:      # from != to → use CSS variable
    style.opacity = var
elif initial_hidden:       # hidden flag → use opacity:0 (NOT display:none)
    style.opacity = 0      # display:none would block descendant opacity animations!
elif op_from != 1.0:      # static non-1 opacity → use animation's from value
    style.opacity = op_from
# else: no opacity style needed (defaults to 1.0)
```

## Texture Coordinates

### contentsRect
Always `{x:0, y:0, width:1, height:1}` in observed Keynote exports → always full texture.
No need to implement partial texture rendering.

### Dimension match
Verified: PDF page size in points = CSS layer size in pixels. Ratio = 1.000 exactly.
No scale factor needed when rasterizing at `fitz.Matrix(1, 1)`.
