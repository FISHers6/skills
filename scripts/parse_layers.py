#!/usr/bin/env python3
"""
Parse Keynote JSON layer tree into normalized Python structures.

Layer dict schema:
  left, top          - CSS position relative to parent (px, float)
  width, height      - CSS size (px, float)
  anchor_x, anchor_y - anchorPoint (0.0-1.0, controls transform-origin)
  scale              - static initialState scale (uniform, float)
  rotation           - static initialState rotation in radians (float)
  opacity            - initial opacity (0.0-1.0)
  hidden             - initial visibility (bool)
  z_position         - z-order value (float, default 0.0). Extracted from CAAnimationGroup
                       sub-animations because Keynote does NOT put zPosition in initialState.
                       Uses the TO-state value (destination slide) — Magic Move can swap z-positions
                       mid-transition (e.g. finger from=0.007→to=0.008, circle from=0.008→to=0.007).
                       Children are sorted ascending by z_position before DOM emission so that
                       higher-z layers render on top (later in DOM = in front in CSS stacking).
  texture_id         - asset tex_id if leaf layer, else None
  children           - list of child layer dicts (sorted by z_position ascending)
  anims              - list of animation dicts:
      property       - 'transform.translation' | 'transform.scale.x' |
                       'transform.scale.y' | 'transform.scale.xy' |
                       'transform.rotation.z' | 'opacity' | 'contents' | 'hidden'
      from_val       - starting value (float, tuple, or str for texture)
      to_val         - ending value
      timing         - 'EaseInEaseOut' | 'Linear' | 'EaseIn' | 'EaseOut'
      begin_time     - float seconds
      duration       - float seconds
  prev_slide_left, prev_slide_top - inferred previous-slide top-left for this layer when available
"""

SKIP_PROPERTIES = {'position'}          # position is handled as a layout value, not an animation
POINT_PROPERTIES = {'transform.translation', 'position'}
SCALAR_PROPERTIES = {
    'transform.scale.x', 'transform.scale.y', 'transform.scale.xy',
    'transform.rotation.z', 'opacity',
}


def _parse_value(val):
    """Extract typed value from Keynote animation value dict."""
    if val is None:
        return None
    if 'scalar' in val and isinstance(val['scalar'], bool):
        return bool(val['scalar'])
    if 'scalar' in val:
        return float(val['scalar'])
    if 'pointX' in val:
        return (float(val.get('pointX', 0)), float(val.get('pointY', 0)))
    if 'texture' in val:
        return val['texture']   # texture ID string
    if 'boolean' in val:
        return bool(val['boolean'])
    v = val.get('scalar')
    if isinstance(v, bool):
        return v
    return None


def _get_property_anims(layer: dict) -> tuple[list, float]:
    """
    Flatten all property animations from a layer's 'animations' array.
    Handles both direct property anims and CAAnimationGroup nesting.

    Returns:
        (anims_list, z_position)
        z_position: extracted from 'zPosition' sub-animation (scalar from_val).
                    Keynote does NOT store zPosition in initialState — only here.
                    Defaults to 0.0 if not found.
    """
    result = []
    z_position = 0.0
    for anim_group in layer.get('animations', []):
        prop = anim_group.get('property')
        if prop and prop not in SKIP_PROPERTIES:
            result.append({
                'property': prop,
                'from_val': _parse_value(anim_group.get('from')),
                'to_val': _parse_value(anim_group.get('to')),
                'timing': anim_group.get('timingFunction', 'EaseInEaseOut'),
                'begin_time': float(anim_group.get('beginTime', 0)),
                'duration': float(anim_group.get('duration', 0)),
            })
        # CAAnimationGroup: property anims inside .animations[]
        for sub in anim_group.get('animations', []):
            sub_prop = sub.get('property')
            if sub_prop == 'zPosition':
                # In Magic Move, zPosition can swap between FROM and TO states
                # (e.g. finger: from=0.007 to=0.008; circle: from=0.008 to=0.007).
                # The TO state is the destination slide — what users see after the
                # transition. Sort by TO so the final stacking order is correct.
                # Fall back to FROM if TO is absent (static slides).
                to_val = _parse_value(sub.get('to'))
                from_val = _parse_value(sub.get('from'))
                val = to_val if isinstance(to_val, float) else from_val
                if isinstance(val, float):
                    z_position = val
            elif sub_prop and sub_prop not in SKIP_PROPERTIES:
                result.append({
                    'property': sub_prop,
                    'from_val': _parse_value(sub.get('from')),
                    'to_val': _parse_value(sub.get('to')),
                    'timing': sub.get('timingFunction', 'EaseInEaseOut'),
                    'begin_time': float(sub.get('beginTime', 0)),
                    'duration': float(sub.get('duration', 0)),
                })
    return result, z_position


def parse_layer(layer: dict) -> dict:
    """
    Parse a single layer and all its children.
    Coordinates are parent-relative (CSS position:absolute semantics).

    CRITICAL: Uses parent-relative coordinates, NOT canvas-space.
    CSS position:absolute is relative to nearest positioned ancestor (parent div),
    so children must be positioned relative to parent, not canvas.

    The anchorPoint (default 0.5,0.5) controls which point in the layer
    coincides with position.pointX/Y. This also sets CSS transform-origin.
    """
    s = layer.get('initialState', {})
    pos = s.get('position', {})
    w = float(s.get('width', 0))
    h = float(s.get('height', 0))
    cx_in_parent = float(pos.get('pointX', 0))
    cy_in_parent = float(pos.get('pointY', 0))

    # anchorPoint: which point in the layer position refers to (default 0.5,0.5 = center)
    ap = s.get('anchorPoint', {})
    anchor_x = float(ap.get('pointX', 0.5))
    anchor_y = float(ap.get('pointY', 0.5))

    # Parent-relative top-left corner
    left = cx_in_parent - anchor_x * w
    top = cy_in_parent - anchor_y * h

    anims, z_position = _get_property_anims(layer)
    texture_id = layer.get('texture')

    # Parse children and sort by z_position ascending (back→front).
    # CRITICAL: Keynote does NOT store zPosition in initialState — it lives
    # in CAAnimationGroup sub-animations.  The JSON array order is NOT
    # guaranteed to match z-order (in magic-move slides the TO-state
    # background layer is often last in the array but has z=0, while the
    # animating foreground has z=0.001).  Sorting here ensures the DOM
    # order produces the correct CSS stacking (later = in front).
    raw_children = [parse_layer(child) for child in layer.get('layers', [])]
    children = sorted(raw_children, key=lambda c: c['z_position'])

    return {
        'left': left,
        'top': top,
        'width': w,
        'height': h,
        'anchor_x': anchor_x,
        'anchor_y': anchor_y,
        'scale': float(s.get('scale', 1.0)),
        'rotation': float(s.get('rotation', 0.0)),
        'opacity': float(s.get('opacity', 1.0)),
        'hidden': bool(s.get('hidden', False)),
        'z_position': z_position,
        'texture_id': texture_id,
        'anims': anims,
        'children': children,
        'magic_move_from_left': layer.get('magic_move_from_left'),
        'magic_move_from_top': layer.get('magic_move_from_top'),
        'prev_slide_left': layer.get('prev_slide_left'),
        'prev_slide_top': layer.get('prev_slide_top'),
    }


def parse_effect_layers(effect: dict) -> dict:
    """Parse the baseLayer of an effect (the animated layer tree)."""
    return parse_layer(effect['baseLayer'])


def has_any_animation(layer: dict) -> bool:
    """True if this layer or any descendant has non-trivial animations."""
    non_trivial = [a for a in layer['anims'] if a['property'] not in ('hidden',)]
    if non_trivial:
        return True
    return any(has_any_animation(child) for child in layer['children'])


def get_prev_slide_positions(slide_json: dict, canvas_width: int = 1920, canvas_height: int = 1080) -> list:
    """
    Extract the canvas top-left position of each content element group in the slide's baseLayer.

    For non-magic-move slides, Keynote wraps each content element in a full-canvas (1920x1080)
    group layer. This function skips those wrappers and returns the actual element position.

    Returns a list with one entry per direct child of the baseLayer:
      - (left, top) tuple if the child contains a non-canvas-sized element
      - None for purely full-canvas children (background layers with no distinct sub-element)

    Used by transpile.py to seed the FROM positions for the next magic-move slide's
    outer container translation animation (elements enter/exit from their source position).
    """
    event = slide_json.get('events', [{}])[0]
    base_layer = event.get('baseLayer', {})
    result = []

    for group in base_layer.get('layers', []):
        s = group.get('initialState', {})
        gw = float(s.get('width', 0))
        gh = float(s.get('height', 0))

        # Full-canvas group: look inside for the actual content element
        if abs(gw - canvas_width) < 2 and abs(gh - canvas_height) < 2:
            found = None
            for sub in group.get('layers', []):
                ss = sub.get('initialState', {})
                sw = float(ss.get('width', 0))
                sh = float(ss.get('height', 0))
                if not (abs(sw - canvas_width) < 2 and abs(sh - canvas_height) < 2):
                    # First non-canvas-sized child = the outer container element
                    ap = ss.get('anchorPoint', {'pointX': 0.5, 'pointY': 0.5})
                    pos = ss.get('position', {})
                    cx = float(pos.get('pointX', 0))
                    cy = float(pos.get('pointY', 0))
                    ax = float(ap.get('pointX', 0.5))
                    ay = float(ap.get('pointY', 0.5))
                    found = (cx - ax * sw, cy - ay * sh)
                    break
            result.append(found)
        else:
            # Direct non-canvas element
            ap = s.get('anchorPoint', {'pointX': 0.5, 'pointY': 0.5})
            pos = s.get('position', {})
            cx = float(pos.get('pointX', 0))
            cy = float(pos.get('pointY', 0))
            ax = float(ap.get('pointX', 0.5))
            ay = float(ap.get('pointY', 0.5))
            result.append((cx - ax * gw, cy - ay * gh))

    return result
