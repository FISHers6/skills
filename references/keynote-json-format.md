# Keynote Web Export JSON Format

Keynote's "Export to HTML" creates a directory structure per slide.

## Directory Structure

```
export-dir/
├── assets/
│   ├── header.json              # Master slide list
│   └── {UUID}/                  # One per slide
│       ├── {UUID}.json          # Animation + layer data
│       ├── {UUID}.jsonp         # JSONP variant (same data)
│       ├── thumbnail.jpeg       # Slide thumbnail
│       └── assets/
│           └── {UUID}.pdf       # Texture pages (one per texture)
└── index.html
```

## header.json

```json
{
  "slideList": ["UUID1", "UUID2", ...],
  "slideCount": 71
}
```

The `slideList` order = presentation slide order.

## Per-Slide JSON: {UUID}.json

```json
{
  "assets": {
    "{tex-UUID}": { "type": "texture", "index": 0 },
    "{tex-UUID2}": { "type": "texture", "index": 1 }
  },
  "events": [
    {
      "effects": [{ ... effect object ... }],
      "baseLayer": { ... layer tree for static content ... },
      "accessibility": [{ "text": "slide title" }]
    }
  ]
}
```

## Effect Object

```json
{
  "name": "apple:magic-move-implied-motion-path",
  "type": "transition",
  "duration": 0.5,
  "beginTime": 0,
  "baseLayer": { ... animated layer tree ... },
  "effects": [],
  "attributes": {}
}
```

Known effect names:
- `"none"` / missing → static slide, no animation
- `"apple:magic-move-implied-motion-path"` → Magic Move transition
- `"com.apple.iWork.Keynote.FromDarkness"` → fade from black
- `"fade and move character"` → per-character fade+move
- `"dissolve character"` → per-character dissolve
- `"bc-zoom-big character"` → per-character zoom
- `"keyboard"` → typewriter character animation
- `"BUKAnvil"`, `"Trace"`, `"BUKLensFlare"` → various effects
- `"LineDrawForLine"` → line drawing animation
- `"action-motion-path"`, `"action-rotation"`, etc. → action effects

## Layer Object

```json
{
  "texture": "tex-UUID",        // present only on leaf layers with image content
  "initialState": {
    "position": { "pointX": 960.0, "pointY": 540.0 },  // center of layer in PARENT space
    "anchorPoint": { "pointX": 0.5, "pointY": 0.5 },   // which point = position (default 0.5,0.5)
    "width": 789.0,
    "height": 193.0,
    "opacity": 1.0,
    "hidden": false,
    "contentsRect": { "x": 0, "y": 0, "width": 1, "height": 1 }  // texture crop (always full)
  },
  "animations": [ ... animation objects ... ],
  "layers": [ ... child layers ... ]
}
```

**CRITICAL**: `position.pointX/Y` is the point in the PARENT's coordinate system where this layer's
`anchorPoint` sits. Default anchorPoint is (0.5, 0.5) = center. To compute CSS `left`:
```
left = position.pointX - anchorPoint.pointX * width
top  = position.pointY - anchorPoint.pointY * height
```
`anchorPoint` CAN exceed [0,1] range (external anchor point). CSS handles >100% `transform-origin` correctly.

## Animation Object (Direct Property)

```json
{
  "property": "transform.translation",
  "from": { "pointX": 0, "pointY": 0 },
  "to":   { "pointX": -208, "pointY": 0 },
  "timingFunction": "EaseInEaseOut",
  "beginTime": 0.0,
  "duration": 0.5
}
```

## CAAnimationGroup (Nested Animations)

Many animations are grouped under a CAAnimationGroup with sub-animations:

```json
{
  "property": null,     // group has no direct property
  "from": {},
  "to": {},
  "animations": [       // <-- sub-animations here
    { "property": "transform.translation", "from": {...}, "to": {...} },
    { "property": "opacity", "from": {"scalar": 0}, "to": {"scalar": 1} }
  ]
}
```

## Animation Value Types

| JSON shape | Python parsed value |
|-----------|-------------------|
| `{"scalar": 0.5}` | `float(0.5)` |
| `{"pointX": 100, "pointY": 200}` | `(100.0, 200.0)` |
| `{"texture": "UUID"}` | `"UUID"` string |
| `{"boolean": true}` | `True` |

## Texture / PDF Mapping

```json
"assets": {
  "946EB2D3...": { "type": "texture", "index": 9 }
}
```

→ Extract PDF page 9 from `{UUID}/assets/{UUID}.pdf`
→ Save as `remotion/public/textures/s{slide_num:03d}_9.png`
→ Serve via `staticFile("textures/s009_9.png")` in Remotion

PDF page dimensions = CSS pixel dimensions of the layer (verified: ratio = 1.000 exactly).
Use `alpha=True` in PyMuPDF to preserve transparency. `alpha=False` composites over white (WRONG).

## Slide Without Animation

If `events[0].effects` is empty, the slide is static (no animation playback).
Generate a placeholder TSX with 3 seconds of black screen (91 frames at 30fps).
