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
import json
import sys
from pathlib import Path

# Allow running from any directory
script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir))

from parse_layers import parse_effect_layers
from generate_tsx import generate_slide_tsx, HOLD_BEFORE, HOLD_AFTER


def get_slide_description(data: dict) -> str:
    for event in data.get('events', []):
        texts = [a.get('text', '') for a in event.get('accessibility', [])]
        joined = ' | '.join(t for t in texts if t)
        if joined:
            return joined[:60]
    return ''


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


def transpile_slide(slide_num: int, slide_id: str, base: Path, all_tex: dict, fps: int = 30) -> dict | None:
    slide_dir = base / 'assets' / slide_id
    json_path = slide_dir / f'{slide_id}.json'
    if not json_path.exists():
        return None

    with open(json_path) as f:
        data = json.load(f)

    tex_map = all_tex.get(slide_id, {})
    event = data['events'][0]
    effects = event.get('effects', [])
    effect = effects[0] if effects else {}

    effect_name = effect.get('name', 'none')
    duration_sec = float(effect.get('duration', 0.001))
    slide_desc = get_slide_description(data)

    if effect:
        root_layer = parse_effect_layers(effect)
    else:
        root_layer = {
            'children': [], 'anims': [], 'texture_id': None,
            'left': 0, 'top': 0, 'width': 1920, 'height': 1080,
            'opacity': 1, 'hidden': False,
        }

    anim_frames = max(1, round(duration_sec * fps))
    total_frames = round(HOLD_BEFORE * fps) + anim_frames + round(HOLD_AFTER * fps)

    tsx = generate_slide_tsx(
        slide_num=slide_num,
        slide_name=slide_desc,
        effect_name=effect_name,
        duration_sec=duration_sec,
        root_layer=root_layer,
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
