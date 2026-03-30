#!/usr/bin/env python3
"""
Keynote → Remotion main transpiler. Runs both extraction and TSX generation.

Usage:
    python3 transpile.py --base-dir /path/to/keynote-project [--skip-extract]

Steps performed:
    1. Extract PDF textures to PNGs (skip with --skip-extract if already done)
    2. Parse each slide's JSON layer tree
    3. Generate Remotion TSX for each slide
    4. Generate Root.tsx registering all slides

Directory structure expected:
    <base>/
    ├── assets/
    │   ├── header.json          # {"slideList": ["UUID", ...]}
    │   └── {UUID}/
    │       ├── {UUID}.json      # animation + layer data
    │       └── assets/
    │           └── {UUID}.pdf   # texture pages
    ├── remotion/                # Remotion project (from assets/remotion-template)
    │   ├── public/textures/     # extracted PNGs go here
    │   └── src/
    │       ├── index.ts
    │       ├── Root.tsx         # generated
    │       └── slides/          # generated TSX files go here
    └── transpiler/              # this script's directory
        ├── texture_map.json     # generated
        └── slide_registry.json  # generated
"""

import argparse
import copy
import json
import sys
from pathlib import Path

# Allow running from any directory
script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir))

from parse_layers import get_prev_slide_positions, parse_layer
from generate_tsx import generate_slide_tsx, HOLD_BEFORE, HOLD_AFTER


def get_slide_description(data: dict) -> str:
    for event in data.get('events', []):
        texts = [a.get('text', '') for a in event.get('accessibility', [])]
        joined = ' | '.join(t for t in texts if t)
        if joined:
            return joined[:60]
    return ''


def _collect_texture_ids(layer: dict) -> tuple[str, ...]:
    textures: list[str] = []
    texture_id = layer.get('texture')
    if isinstance(texture_id, str):
        textures.append(texture_id)
    for child in layer.get('layers', []):
        textures.extend(_collect_texture_ids(child))
    return tuple(textures)


def _layer_signature(layer: dict) -> tuple:
    state = layer.get('initialState', {})
    pos = state.get('position', {})
    return (
        layer.get('texture'),
        round(float(state.get('width', 0)), 3),
        round(float(state.get('height', 0)), 3),
        round(float(pos.get('pointX', 0)), 3),
        round(float(pos.get('pointY', 0)), 3),
        len(layer.get('layers', [])),
        _collect_texture_ids(layer),
    )


def _find_matching_paths(layer: dict, matcher, path: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
    matches = []
    if matcher(layer):
        matches.append(path)
    for idx, child in enumerate(layer.get('layers', [])):
        matches.extend(_find_matching_paths(child, matcher, path + (idx,)))
    return matches


def _replace_layer_at_path(layer: dict, path: tuple[int, ...], replacement: dict) -> dict:
    if not path:
        return copy.deepcopy(replacement)

    merged = copy.deepcopy(layer)
    cursor = merged
    for idx in path[:-1]:
        cursor = cursor['layers'][idx]
    cursor['layers'][path[-1]] = copy.deepcopy(replacement)
    return merged


def merge_effect_base_layer(event_base: dict, effect_base: dict) -> dict:
    """
    Keep the full scene from event.baseLayer, then swap in the animated subtree from effect.baseLayer.

    Keynote often stores only the animated object tree inside effect.baseLayer. Rendering that tree by
    itself drops static siblings such as dark backgrounds, chopsticks/spoons, or other non-animated
    scene elements. Matching by objectID is the primary path; a structural signature fallback handles
    rare exports where the animated subtree is missing objectID.
    """
    full_scene = copy.deepcopy(event_base)
    animated_subtree = copy.deepcopy(effect_base)

    effect_object_id = animated_subtree.get('objectID')
    if effect_object_id:
        matches = _find_matching_paths(full_scene, lambda layer: layer.get('objectID') == effect_object_id)
        if len(matches) == 1:
            return _replace_layer_at_path(full_scene, matches[0], animated_subtree)

    signature = _layer_signature(animated_subtree)
    matches = _find_matching_paths(full_scene, lambda layer: _layer_signature(layer) == signature)
    if len(matches) == 1:
        return _replace_layer_at_path(full_scene, matches[0], animated_subtree)

    return full_scene


def _is_canvas_sized(layer: dict, canvas_width: int = 1920, canvas_height: int = 1080) -> bool:
    state = layer.get('initialState', {})
    width = float(state.get('width', 0))
    height = float(state.get('height', 0))
    return abs(width - canvas_width) < 2 and abs(height - canvas_height) < 2


def _descendant_textures(layer: dict) -> list[str]:
    textures: list[str] = []
    texture = layer.get('texture')
    if isinstance(texture, str):
        textures.append(texture)
    for child in layer.get('layers', []):
        textures.extend(_descendant_textures(child))
    return textures


def _contents_pairs(layer: dict) -> list[tuple[str | None, str | None]]:
    pairs: list[tuple[str | None, str | None]] = []
    for anim in layer.get('animations', []):
        if anim.get('property') == 'contents':
            frm = anim.get('from', {}).get('texture')
            to = anim.get('to', {}).get('texture')
            pairs.append((frm, to))
        for sub in anim.get('animations', []):
            if sub.get('property') == 'contents':
                frm = sub.get('from', {}).get('texture')
                to = sub.get('to', {}).get('texture')
                pairs.append((frm, to))
    for child in layer.get('layers', []):
        pairs.extend(_contents_pairs(child))
    return pairs


def _has_intrinsic_motion(layer: dict) -> bool:
    """
    True when the subtree already carries its own transform motion.

    Adding an extra outer-container translate on top of an existing per-layer
    translate / scale / rotation double-counts the movement and makes objects
    disappear before snapping back in. We only inject magic-move container motion
    for highlight-like layers that otherwise just cross-dissolve or fade.
    """
    motion_props = {
        'transform.translation',
        'transform.scale.x',
        'transform.scale.y',
        'transform.scale.xy',
        'transform.rotation.z',
    }
    for anim in layer.get('animations', []):
        if anim.get('property') in motion_props:
            return True
        for sub in anim.get('animations', []):
            if sub.get('property') in motion_props:
                return True
    return any(_has_intrinsic_motion(child) for child in layer.get('layers', []))


def inject_magic_move_positions(base_layer: dict, prev_slide_json: dict | None) -> dict:
    """
    Attach previous-slide positions only to high-confidence matching top-level children.

    Strategy:
    1. Prefer object-level matching by `contents.from` texture → previous slide top-level leaf texture.
       This works for slides where Keynote already split the scene into per-object layers.
       Do NOT skip layers just because their descendants also animate. In many Magic Move slides,
       the outer container still needs the previous slide's position while the inner leaf handles
       scale/contents/rotation. Skipping those layers is what makes the highlight ring move while
       the spoon/chopsticks beneath it jump to the destination frame.
    2. If no texture-based matches exist at all, fall back to top-level order only for small,
       full-canvas wrapper slides. This preserves simple cases without damaging dense magic-move
       grids where counts diverge.
    """
    if not prev_slide_json:
        return base_layer

    prev_event_base = prev_slide_json.get('events', [{}])[0].get('baseLayer', {})
    prev_children = prev_event_base.get('layers', [])
    curr_children = base_layer.get('layers', [])
    if not prev_children or not curr_children:
        return base_layer

    prev_positions = get_prev_slide_positions(prev_slide_json)
    prev_by_texture: dict[str, list[tuple[int, tuple[float, float] | None]]] = {}
    for idx, child in enumerate(prev_children):
        textures = _descendant_textures(child)
        if len(textures) == 1:
            prev_by_texture.setdefault(textures[0], []).append((idx, prev_positions[idx]))

    current_from_occurrence: dict[str, int] = {}
    matched_positions: dict[int, tuple[float, float]] = {}

    for idx, child in enumerate(curr_children):
        if _has_intrinsic_motion(child):
            continue
        pairs = [pair for pair in _contents_pairs(child) if pair[0]]
        if not pairs:
            continue

        from_texture = pairs[0][0]
        if not isinstance(from_texture, str):
            continue

        occurrence = current_from_occurrence.get(from_texture, 0)
        current_from_occurrence[from_texture] = occurrence + 1

        candidates = prev_by_texture.get(from_texture, [])
        if occurrence >= len(candidates):
            continue

        prev_pos = candidates[occurrence][1]
        if prev_pos is None:
            continue
        matched_positions[idx] = prev_pos

    if not matched_positions:
        # Conservative fallback for simple "previous slide was just full-canvas wrappers" cases.
        if (
            len(prev_positions) == len(curr_children) and
            len(curr_children) <= 6 and
            all(_is_canvas_sized(child) for child in prev_children)
        ):
            merged = copy.deepcopy(base_layer)
            for child, prev_pos in zip(merged.get('layers', []), prev_positions):
                if _has_intrinsic_motion(child):
                    continue
                if prev_pos is None:
                    continue
                child['magic_move_from_left'] = prev_pos[0]
                child['magic_move_from_top'] = prev_pos[1]
            return merged
        return base_layer

    merged = copy.deepcopy(base_layer)
    for idx, prev_pos in matched_positions.items():
        merged['layers'][idx]['magic_move_from_left'] = prev_pos[0]
        merged['layers'][idx]['magic_move_from_top'] = prev_pos[1]
    return merged


def generate_root_tsx(registry: list, src_dir: Path, fps: int = 30, width: int = 1920, height: int = 1080):
    """Generate Root.tsx that registers all slide compositions."""
    imports = []
    comps = []
    for entry in registry:
        n = entry['comp_name']
        f = f"SLIDE_{entry['slide_num']:03d}_FRAMES"
        imports.append(f"import {{ {n}, {f} }} from './slides/{n}';")
        comps.append(
            f"      <Composition id=\"{n}\" component={{{n}}} "
            f"durationInFrames={{{f}}} fps={{{fps}}} width={{{width}}} height={{{height}}} />"
        )

    root_tsx = "import React from \"react\";\n"
    root_tsx += "import { Composition } from \"remotion\";\n"
    root_tsx += '\n'.join(imports) + '\n\n'
    root_tsx += "export const Root: React.FC = () => (\n"
    root_tsx += "  <>\n"
    root_tsx += '\n'.join(comps) + '\n'
    root_tsx += "  </>\n);\n"

    out = src_dir / 'Root.tsx'
    out.write_text(root_tsx)
    print(f"Generated Root.tsx with {len(registry)} compositions → {out}")


def _load_prev_slide_data(base: Path, slide_num: int) -> dict | None:
    if slide_num <= 1:
        return None
    header = json.loads((base / 'assets' / 'header.json').read_text())
    prev_slide_id = header['slideList'][slide_num - 2]
    prev_json_path = base / 'assets' / prev_slide_id / f'{prev_slide_id}.json'
    if not prev_json_path.exists():
        return None
    with open(prev_json_path) as f:
        return json.load(f)


def _build_segment(event: dict, prev_data: dict | None) -> dict:
    effects = event.get('effects', [])
    effect = effects[0] if effects else {}
    effect_name = effect.get('name', 'none')
    duration_sec = float(effect.get('duration', 0.001))

    if effect and effect.get('baseLayer'):
        merged_base = merge_effect_base_layer(event['baseLayer'], effect['baseLayer'])
        if 'magic-move' in effect_name.lower():
            merged_base = inject_magic_move_positions(merged_base, prev_data)
        root_layer = parse_layer(merged_base)
    else:
        root_layer = parse_layer(event['baseLayer'])

    return {
        'effect_name': effect_name,
        'duration_sec': duration_sec,
        'root_layer': root_layer,
    }


def transpile_slide(slide_num: int, slide_id: str, base: Path, all_tex: dict, fps: int = 30) -> dict | None:
    slide_dir = base / 'assets' / slide_id
    json_path = slide_dir / f'{slide_id}.json'
    if not json_path.exists():
        return None

    with open(json_path) as f:
        data = json.load(f)

    tex_map = all_tex.get(slide_id, {})
    slide_desc = get_slide_description(data)
    prev_data = _load_prev_slide_data(base, slide_num)
    segments = [_build_segment(event, prev_data) for event in data.get('events', [])]
    if not segments:
        segments = [{
            'effect_name': 'none',
            'duration_sec': 0.001,
            'root_layer': parse_layer({}),
        }]

    duration_sec = sum(float(segment['duration_sec']) for segment in segments)
    effect_name = ' + '.join(segment['effect_name'] for segment in segments)
    anim_frames = sum(max(1, round(float(segment['duration_sec']) * fps)) for segment in segments)
    total_frames = round(HOLD_BEFORE * fps) + anim_frames + round(HOLD_AFTER * fps)

    tsx = generate_slide_tsx(
        slide_num=slide_num,
        slide_name=slide_desc,
        segments=segments,
        tex_map=tex_map,
        fps=fps,
    )
    return {
        'tsx': tsx,
        'comp_name': f'Slide{slide_num:03d}',
        'slide_num': slide_num,
        'slide_id': slide_id,
        'effect_name': effect_name,
        'duration_sec': duration_sec,
        'total_frames': total_frames,
        'desc': slide_desc,
    }


def main():
    parser = argparse.ArgumentParser(description='Keynote → Remotion transpiler')
    parser.add_argument('--base-dir', default=None, help='Keynote project base directory')
    parser.add_argument('--skip-extract', action='store_true', help='Skip PDF texture extraction')
    parser.add_argument('--fps', type=int, default=30, help='Video frame rate (default: 30)')
    parser.add_argument('--width', type=int, default=1920, help='Video width in px (default: 1920)')
    parser.add_argument('--height', type=int, default=1080, help='Video height in px (default: 1080)')
    args = parser.parse_args()

    base = Path(args.base_dir).resolve() if args.base_dir else script_dir.parent
    slides_out = base / 'remotion' / 'src' / 'slides'
    src_dir = base / 'remotion' / 'src'
    header_path = base / 'assets' / 'header.json'
    tex_map_path = base / 'transpiler' / 'texture_map.json'
    registry_path = base / 'transpiler' / 'slide_registry.json'

    slides_out.mkdir(parents=True, exist_ok=True)

    # Step 1: Extract textures
    if not args.skip_extract:
        print("=== Step 1: Extracting textures ===")
        from extract_textures import extract_slide_textures
        textures_out = base / 'remotion' / 'public' / 'textures'
        textures_out.mkdir(parents=True, exist_ok=True)
        header = json.loads(header_path.read_text())
        slide_list = header['slideList']
        tex_map_data: dict = {}
        for i, slide_id in enumerate(slide_list, 1):
            slide_dir = base / 'assets' / slide_id
            result = extract_slide_textures(slide_dir, i, textures_out)
            tex_map_data[slide_id] = result
            print(f"  Slide {i:02d}: {len(result)} textures")
        tex_map_path.write_text(json.dumps(tex_map_data, indent=2))
        print(f"  → {tex_map_path}")
    else:
        print("=== Step 1: Skipping texture extraction ===")

    # Step 2: Transpile slides
    print("\n=== Step 2: Transpiling slides ===")
    header = json.loads(header_path.read_text())
    slide_list = header['slideList']
    all_tex = json.loads(tex_map_path.read_text())

    registry = []
    errors = []

    for i, slide_id in enumerate(slide_list, 1):
        try:
            result = transpile_slide(i, slide_id, base, all_tex, fps=args.fps)
            if result:
                tsx = result.pop('tsx')
                out_path = slides_out / f'Slide{i:03d}.tsx'
                out_path.write_text(tsx)
                registry.append(result)
                effect = result['effect_name'].split('.')[-1].split(':')[-1]
                print(f"  Slide {i:02d}: [{effect:<40s}] {result['total_frames']:4d}fr")
        except Exception as e:
            errors.append((i, slide_id, str(e)))
            print(f"  Slide {i:02d}: ERROR - {e}", file=sys.stderr)

    registry_path.write_text(json.dumps(registry, indent=2))

    # Step 3: Generate Root.tsx
    print("\n=== Step 3: Generating Root.tsx ===")
    generate_root_tsx(registry, src_dir, fps=args.fps, width=args.width, height=args.height)

    print(f"\nDone: {len(registry)} slides transpiled, {len(errors)} errors")
    if errors:
        for num, sid, err in errors:
            print(f"  Slide {num} ({sid[:8]}): {err}")


if __name__ == '__main__':
    main()
