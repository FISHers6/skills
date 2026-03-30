#!/usr/bin/env python3
"""
Generate Remotion TSX composition code from a parsed layer tree.

Each slide becomes:
  src/slides/Slide{N:03d}.tsx
  exports: Slide{N:03d} component + SLIDE_{N:03d}_FRAMES constant

Animation model: single linear progress 0→1 interpolated over [START, END] frames.
All property animations (opacity, translate, scale, rotation, contents) are
mapped to this unified progress variable.
"""

from typing import Any

FPS = 30
HOLD_BEFORE = 1.0   # seconds of static hold before animation starts
HOLD_AFTER = 2.0    # seconds of static hold after animation ends

TIMING_MAP = {
    'EaseInEaseOut': 'Easing.inOut(Easing.ease)',
    'EaseIn':        'Easing.in(Easing.ease)',
    'EaseOut':       'Easing.out(Easing.ease)',
    'Linear':        'undefined',
}


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f'{v:.6g}'
    if isinstance(v, int):
        return str(v)
    return repr(v)


def _layer_has_content(layer: dict) -> bool:
    """True if this layer or any descendant has a texture or real animations."""
    if layer['texture_id']:
        return True
    if layer['anims']:
        return True
    return any(_layer_has_content(c) for c in layer['children'])


def _timing_to_frames(anim: dict | None, start_offset_frames: int, fps: int) -> tuple[int, int, str]:
    """
    Convert a Keynote animation's begin/duration into absolute composition frames.

    Per-property beginTime/duration matter. Collapsing everything to the slide-level
    `progress` makes later cross-dissolves and delayed fades start too early, which is
    exactly what causes "the next frame arrives before the current animation is done".
    """
    if anim is None:
        start_frame = start_offset_frames
        end_frame = start_offset_frames + max(1, round(0.001 * fps))
        return start_frame, end_frame, 'Easing.inOut(Easing.ease)'

    start_frame = start_offset_frames + round(float(anim.get('begin_time', 0.0)) * fps)
    end_frame = start_frame + max(1, round(float(anim.get('duration', 0.0)) * fps))
    timing = TIMING_MAP.get(anim.get('timing', 'EaseInEaseOut'), 'Easing.inOut(Easing.ease)')
    return start_frame, end_frame, timing


def _emit_timed_interpolate(
    lines: list[str],
    var_name: str,
    from_val: Any,
    to_val: Any,
    anim: dict | None,
    start_offset_frames: int,
    fps: int,
):
    start_frame, end_frame, timing = _timing_to_frames(anim, start_offset_frames, fps)
    options = ['...CLAMP']
    if timing != 'undefined':
        options.append(f'easing: {timing}')
    options_str = ', '.join(options)
    lines.append(
        f'  const {var_name} = interpolate(frame, [{start_frame},{end_frame}], '
        f'[{_fmt(from_val)},{_fmt(to_val)}], {{ {options_str} }});'
    )


def _subtree_motion_timing(layer: dict) -> dict | None:
    """
    Find the visible animation window for a layer subtree.

    Magic Move wrapper travel should line up with when that specific element is
    actually animating on screen. Using the whole slide progress makes transient
    highlight rings fade out before they ever reach the destination.

    Hidden animations are excluded because they usually just keep the layer
    hidden after the real motion/fade has already finished.
    """
    earliest = None
    latest = None
    timing = 'EaseInEaseOut'

    for anim in layer.get('anims', []):
        if anim['property'] == 'hidden':
            continue
        begin = float(anim.get('begin_time', 0.0))
        end = begin + float(anim.get('duration', 0.0))
        if earliest is None or begin < earliest:
            earliest = begin
            timing = anim.get('timing', 'EaseInEaseOut')
        if latest is None or end > latest:
            latest = end

    for child in layer.get('children', []):
        child_window = _subtree_motion_timing(child)
        if child_window is None:
            continue
        if earliest is None or child_window['begin_time'] < earliest:
            earliest = child_window['begin_time']
            timing = child_window.get('timing', 'EaseInEaseOut')
        if latest is None or child_window['end_time'] > latest:
            latest = child_window['end_time']

    if earliest is None or latest is None:
        return None

    return {
        'begin_time': earliest,
        'duration': max(0.001, latest - earliest),
        'timing': timing,
        'end_time': latest,
    }


def _gen_layer(
    layer: dict,
    tex_map: dict,
    slide_num: int,
    var_prefix: str,
    lines: list,
    jsx_lines: list,
    start_offset_frames: int,
    segment_duration_sec: float,
    fps: int,
):
    """
    Recursively generate animation variables and JSX for one layer.
    Only emits code for layers with textures, animations, or content children.
    """
    left = layer['left']
    top = layer['top']
    w = layer['width']
    h = layer['height']
    anchor_x = layer.get('anchor_x', 0.5)
    anchor_y = layer.get('anchor_y', 0.5)
    initial_scale = layer.get('scale', 1.0)
    initial_rotation = layer.get('rotation', 0.0)
    initial_opacity = layer['opacity']
    initial_hidden = layer['hidden']
    texture_id = layer['texture_id']
    anims = layer['anims']
    children = layer['children']
    prop_anims = {anim['property']: anim for anim in anims}

    is_leaf = texture_id is not None
    has_anims = bool(anims)
    has_children_content = any(_layer_has_content(c) for c in children)

    if not is_leaf and not has_anims and not has_children_content:
        return

    p = var_prefix

    # Resolve hidden state from animation (overrides initialState.hidden).
    # Keynote stores hidden=True on layers that are invisible in that state.
    # A hidden=True layer MUST stay at opacity:0 regardless of any opacity animation
    # (the opacity value is Keynote's internal representation, not the visible opacity).
    # Examples:
    #   hidden True→True + opacity 1→0  → always invisible (Slide015 white cards)
    #   hidden False→False + opacity 0→1 → fades in normally (Slide006 green bar)
    hidden_from = initial_hidden
    hidden_to = initial_hidden
    hidden_anim = prop_anims.get('hidden')
    for anim in anims:
        if anim['property'] == 'hidden':
            # Respect delayed hidden animations. A hidden=True animation that starts
            # later in the transition must not force the layer invisible at frame 0.
            if anim['begin_time'] <= 1e-6 and anim['from_val'] is not None:
                hidden_from = bool(anim['from_val'])
            if anim['to_val'] is not None:
                hidden_to = bool(anim['to_val'])

    # Collect animation from/to values
    tx_from = ty_from = 0.0
    tx_to = ty_to = 0.0
    sx_from = sy_from = initial_scale
    sx_to = sy_to = initial_scale
    rz_from = rz_to = initial_rotation
    # Seed opacity from initialState, but force 0 when hidden
    op_from = 0.0 if hidden_from else initial_opacity
    op_to   = 0.0 if hidden_to   else initial_opacity
    contents_from = texture_id
    contents_to = texture_id
    translation_anim = prop_anims.get('transform.translation')
    sx_anim = prop_anims.get('transform.scale.xy') or prop_anims.get('transform.scale.x')
    sy_anim = prop_anims.get('transform.scale.xy') or prop_anims.get('transform.scale.y')
    rz_anim = prop_anims.get('transform.rotation.z')
    opacity_anim = prop_anims.get('opacity')
    contents_anim = prop_anims.get('contents')

    for anim in anims:
        prop = anim['property']
        fv = anim['from_val']
        tv = anim['to_val']

        if prop == 'transform.translation':
            if fv: tx_from, ty_from = fv
            if tv: tx_to, ty_to = tv
        elif prop == 'transform.scale.x':
            if fv is not None: sx_from = fv
            if tv is not None: sx_to = tv
        elif prop == 'transform.scale.y':
            if fv is not None: sy_from = fv
            if tv is not None: sy_to = tv
        elif prop == 'transform.scale.xy':
            if fv is not None: sx_from = sy_from = fv
            if tv is not None: sx_to = sy_to = tv
        elif prop == 'transform.rotation.z':
            if fv is not None: rz_from = fv
            if tv is not None: rz_to = tv
        elif prop == 'opacity':
            # Only apply opacity animation when the hidden flag permits
            if not hidden_from and fv is not None: op_from = fv
            if not hidden_to   and tv is not None: op_to   = tv
        elif prop == 'contents':
            if isinstance(fv, str): contents_from = fv
            if isinstance(tv, str): contents_to = tv

    animated_transform = (
        tx_from != tx_to or ty_from != ty_to or
        sx_from != sx_to or sy_from != sy_to or
        rz_from != rz_to
    )
    animated_opacity = op_from != op_to
    animated_contents = (
        contents_from != contents_to and
        contents_from is not None and
        contents_to is not None
    )

    # Emit animation variable declarations
    if animated_transform:
        _emit_timed_interpolate(lines, f'{p}_tx', tx_from, tx_to, translation_anim, start_offset_frames, fps)
        _emit_timed_interpolate(lines, f'{p}_ty', ty_from, ty_to, translation_anim, start_offset_frames, fps)
        if sx_from != sx_to:
            _emit_timed_interpolate(lines, f'{p}_sx', sx_from, sx_to, sx_anim, start_offset_frames, fps)
        if sy_from != sy_to:
            _emit_timed_interpolate(lines, f'{p}_sy', sy_from, sy_to, sy_anim, start_offset_frames, fps)
        if rz_from != rz_to:
            _emit_timed_interpolate(lines, f'{p}_rz', rz_from, rz_to, rz_anim, start_offset_frames, fps)

    if animated_opacity:
        _emit_timed_interpolate(
            lines, f'{p}_op', op_from, op_to, opacity_anim or hidden_anim, start_offset_frames, fps
        )

    if animated_contents:
        _emit_timed_interpolate(lines, f'{p}_cop', 1, 0, contents_anim, start_offset_frames, fps)
        lines[-1] += ' // contents A → B'

    # Build CSS style
    style_parts = [
        f'position:"absolute"',
        f'left:{_fmt(left)}',
        f'top:{_fmt(top)}',
        f'width:{_fmt(w)}',
        f'height:{_fmt(h)}',
    ]

    if animated_opacity:
        style_parts.append(f'opacity:{p}_op')
    elif initial_hidden:
        # Use opacity:0 NOT display:none — display:none blocks descendant opacity animations
        style_parts.append('opacity:0')
    elif op_from != 1.0:
        # Use animation's from-value, not initialState.opacity
        # (initialState may be 0 for containers whose animation keeps them at 1 throughout)
        style_parts.append(f'opacity:{_fmt(op_from)}')

    # Magic Move outer container: animate from prev-slide position to current (Slide5 destination).
    # Keynote stores outer containers at the DESTINATION position. The FROM position (where the
    # element was in the previous slide) is injected by transpile.py from the prev slide's JSON.
    # This creates the "enter from below canvas" / "exit canvas" visual motion.
    mm_from_left = layer.get('magic_move_from_left')
    mm_from_top  = layer.get('magic_move_from_top')
    mm_motion_timing = None
    if mm_from_left is not None and mm_from_top is not None:
        dx = mm_from_left - left   # positive = element starts to the right of dest
        dy = mm_from_top  - top    # positive = element starts below dest
        if abs(dx) > 0.5 or abs(dy) > 0.5:
            mm_motion_timing = _subtree_motion_timing(layer)
            if mm_motion_timing is None:
                mm_motion_timing = {
                    'begin_time': 0.0,
                    'duration': max(0.001, segment_duration_sec),
                    'timing': 'EaseInEaseOut',
                }
            _emit_timed_interpolate(
                lines, f'{p}_ctx', dx, 0, mm_motion_timing, start_offset_frames, fps
            )
            _emit_timed_interpolate(
                lines, f'{p}_cty', dy, 0, mm_motion_timing, start_offset_frames, fps
            )

    transform_parts = []
    # Magic Move container translation (outer div moves from prev-slide pos to dest)
    if mm_from_left is not None and mm_from_top is not None:
        dx = mm_from_left - left
        dy = mm_from_top  - top
        if abs(dx) > 0.5 or abs(dy) > 0.5:
            transform_parts.append(f'translate(${{{p}_ctx}}px,${{{p}_cty}}px)')
    if animated_transform:
        transform_parts.append(f'translate(${{{p}_tx}}px,${{{p}_ty}}px)')
        if sx_from != sx_to:
            transform_parts.append(f'scaleX(${{{p}_sx}})')
        elif sx_from != 1.0:
            transform_parts.append(f'scaleX({_fmt(sx_from)})')
        if sy_from != sy_to:
            transform_parts.append(f'scaleY(${{{p}_sy}})')
        elif sy_from != 1.0:
            transform_parts.append(f'scaleY({_fmt(sy_from)})')
        if rz_from != rz_to:
            transform_parts.append(f'rotate(${{{p}_rz}}rad)')
        elif rz_from != 0.0:
            transform_parts.append(f'rotate({_fmt(rz_from)}rad)')
    else:
        if sx_from != 1.0: transform_parts.append(f'scaleX({_fmt(sx_from)})')
        if sy_from != 1.0: transform_parts.append(f'scaleY({_fmt(sy_from)})')
        if rz_from != 0.0: transform_parts.append(f'rotate({_fmt(rz_from)}rad)')

    if transform_parts:
        transform_str = ' '.join(transform_parts)
        style_parts.append(f'transform:`{transform_str}`')
        # anchor_x/y can exceed 1.0 for elements with external anchor points (e.g. 4.42)
        # CSS handles >100% percentages correctly — origin is outside element bounds
        style_parts.append(f'transformOrigin:"{anchor_x*100:.4g}% {anchor_y*100:.4g}%"')

    style_str = '{' + ','.join(style_parts) + '}'
    jsx_lines.append(f'      <div style={{{style_str}}}>')

    # Inner content
    if is_leaf:
        if animated_contents and contents_from and contents_to:
            png_from = tex_map.get(contents_from, '')
            png_to = tex_map.get(contents_to, '')
            jsx_lines.append(f'        <Img src={{staticFile("{png_from}")}} style={{{{position:"absolute",inset:0,width:"100%",height:"100%",opacity:{p}_cop}}}} />')
            jsx_lines.append(f'        <Img src={{staticFile("{png_to}")}} style={{{{position:"absolute",inset:0,width:"100%",height:"100%",opacity:1-{p}_cop}}}} />')
        elif texture_id and texture_id in tex_map:
            png = tex_map[texture_id]
            jsx_lines.append(f'        <Img src={{staticFile("{png}")}} style={{{{width:"100%",height:"100%"}}}} />')
    else:
        for i, child in enumerate(children):
            _gen_layer(
                child,
                tex_map,
                slide_num,
                f'{p}c{i}',
                lines,
                jsx_lines,
                start_offset_frames,
                segment_duration_sec,
                fps,
            )

    jsx_lines.append('      </div>')


def generate_slide_tsx(
    slide_num: int,
    slide_name: str,
    segments: list[dict],
    tex_map: dict,
    fps: int = FPS,
) -> str:
    comp_name = f'Slide{slide_num:03d}'
    frames_const = f'SLIDE_{slide_num:03d}_FRAMES'

    hold_before_frames = round(HOLD_BEFORE * fps)
    anim_frames = sum(max(1, round(float(segment['duration_sec']) * fps)) for segment in segments)
    hold_after_frames = round(HOLD_AFTER * fps)
    total_frames = hold_before_frames + anim_frames + hold_after_frames

    var_lines: list = []
    segment_blocks: list[str] = []
    segment_start_frames: list[int] = []
    running_frames = hold_before_frames

    for idx, segment in enumerate(segments):
        segment_start = running_frames
        segment_start_frames.append(segment_start)
        segment_frames = max(1, round(float(segment['duration_sec']) * fps))
        segment_jsx_lines: list[str] = []

        # Emit root_layer itself — preserves its canvas-space offset.
        _gen_layer(
            segment['root_layer'],
            tex_map,
            slide_num,
            f'L{idx}',
            var_lines,
            segment_jsx_lines,
            segment_start,
            float(segment['duration_sec']),
            fps,
        )

        segment_body = '\n'.join(segment_jsx_lines) if segment_jsx_lines else '        {/* no animated layers */}'
        next_start = segment_start + segment_frames
        if idx == 0 and len(segments) == 1:
            segment_blocks.append(segment_body)
        elif idx == 0:
            segment_blocks.append(
                f'      {{frame < {next_start} ? (\n{segment_body}\n      ) : null}}'
            )
        elif idx == len(segments) - 1:
            segment_blocks.append(
                f'      {{frame >= {segment_start} ? (\n{segment_body}\n      ) : null}}'
            )
        else:
            segment_blocks.append(
                f'      {{frame >= {segment_start} && frame < {next_start} ? (\n{segment_body}\n      ) : null}}'
            )

        running_frames += segment_frames

    jsx_body = '\n'.join(segment_blocks) if segment_blocks else '      {/* no animated layers */}'
    effect_name = ' + '.join(segment['effect_name'] for segment in segments)
    duration_sec = sum(float(segment['duration_sec']) for segment in segments)

    tsx = f'''\
import React from "react";
import {{ AbsoluteFill, Easing, Img, interpolate, staticFile, useCurrentFrame, useVideoConfig }} from "remotion";

// Slide {slide_num:03d}: {slide_name}
// Effect: {effect_name}  Duration: {duration_sec:.2f}s
export const {comp_name}: React.FC = () => {{
  const frame = useCurrentFrame();
  const {{ fps }} = useVideoConfig();
  const CLAMP = {{ extrapolateLeft: "clamp" as const, extrapolateRight: "clamp" as const }};

{chr(10).join(var_lines) if var_lines else "  // static slide — no animations"}

  return (
    <AbsoluteFill style={{{{ backgroundColor: "#ffffff", overflow: "hidden" }}}}>
{jsx_body}
    </AbsoluteFill>
  );
}};

export const {frames_const} = {total_frames}; // {total_frames / fps:.1f}s @ {fps}fps
'''
    return tsx
