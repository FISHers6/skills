#!/usr/bin/env python3
"""
Extract individual PDF pages (textures) to PNG files for each Keynote slide.
Uses PyMuPDF (fitz) to rasterize at native resolution with alpha transparency.

Usage:
    python3 extract_textures.py --base-dir /path/to/keynote-project
    python3 extract_textures.py  # uses BASE constant below

Output:
    <base>/remotion/public/textures/s{slide_num:03d}_{index}.png
    <base>/transpiler/texture_map.json  (slide_id → {tex_id → "textures/s{N}_{i}.png"})

Requirements:
    pip install pymupdf
"""

import json
import sys
import argparse
from pathlib import Path
import fitz  # PyMuPDF


def extract_slide_textures(slide_dir: Path, slide_num: int, textures_out: Path) -> dict:
    """
    Returns {tex_id: "textures/s{slide_num:03d}_{index}.png"} for each asset.
    Scale=1 matches Keynote's CSS pixel sizes exactly (confirmed by dimension check).
    alpha=True preserves transparency (alpha=False composites over white — WRONG).
    """
    slide_id = slide_dir.name
    json_path = slide_dir / f"{slide_id}.json"
    pdf_path = slide_dir / 'assets' / f"{slide_id}.pdf"

    if not json_path.exists() or not pdf_path.exists():
        print(f"  Slide {slide_num:02d}: missing json or pdf, skipped")
        return {}

    with open(json_path) as f:
        data = json.load(f)

    assets = data.get('assets', {})
    if not assets:
        return {}

    # tex_id → index mapping
    id_to_index = {
        tex_id: asset['index']
        for tex_id, asset in assets.items()
        if asset.get('type') == 'texture'
    }

    id_to_path: dict = {}
    pdf = fitz.open(str(pdf_path))
    total_pages = len(pdf)

    for tex_id, idx in id_to_index.items():
        out_path = textures_out / f"s{slide_num:03d}_{idx}.png"
        static_path = f"textures/s{slide_num:03d}_{idx}.png"
        id_to_path[tex_id] = static_path

        if out_path.exists():
            continue  # already extracted

        if idx >= total_pages:
            print(f"  WARNING: slide {slide_num}, tex {tex_id[:8]}: index {idx} >= {total_pages} pages")
            continue

        page = pdf.load_page(idx)
        # Scale=1: native resolution matches CSS pixel sizes in JSON (1:1 ratio confirmed)
        mat = fitz.Matrix(1, 1)
        pix = page.get_pixmap(matrix=mat, alpha=True)  # alpha=True preserves transparency
        pix.save(str(out_path))

    pdf.close()
    return id_to_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-dir', default=None, help='Keynote project base directory')
    args = parser.parse_args()

    base = Path(args.base_dir) if args.base_dir else Path(__file__).parent.parent
    textures_out = base / 'remotion' / 'public' / 'textures'
    header_path = base / 'assets' / 'header.json'
    tex_map_out = base / 'transpiler' / 'texture_map.json'

    textures_out.mkdir(parents=True, exist_ok=True)

    header = json.loads(header_path.read_text())
    slide_list = header['slideList']

    tex_map: dict = {}
    for i, slide_id in enumerate(slide_list, 1):
        slide_dir = base / 'assets' / slide_id
        result = extract_slide_textures(slide_dir, i, textures_out)
        tex_map[slide_id] = result
        print(f"Slide {i:02d}/{len(slide_list)}: {len(result)} textures  [{slide_id[:8]}]")

    tex_map_out.write_text(json.dumps(tex_map, indent=2))
    print(f"\nDone. Texture map: {tex_map_out}")
    print(f"Total PNGs: {len(list(textures_out.glob('*.png')))}")


if __name__ == '__main__':
    main()
