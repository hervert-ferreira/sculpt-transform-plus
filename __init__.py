# Sculpt Transform Plus - improves the Sculpt Mode Transform tool in Blender 5.3+.
# Copyright (C) 2026 Hervert Ferreira
# SPDX-License-Identifier: GPL-3.0-or-later

import math
import os
import time

import bpy
import numpy as np
from bpy.app.handlers import persistent
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Gizmo, GizmoGroup, Operator
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

# Name of the custom transform orientation that mirrors the native sculpt pivot rotation.
PIVOT_ORIENTATION = "Sculpt Pivot"

_AXIS_INDEX = {'X': 0, 'Y': 1, 'Z': 2}
_TRANSFORM_TOOLS = ("builtin.transform", "builtin.move", "builtin.rotate", "builtin.scale")


# ----------------------------------------------------------------------------
# Context helpers
# ----------------------------------------------------------------------------
def _is_sculpt(ctx):
    return ctx.mode == 'SCULPT'


def _active_tool_id(ctx):
    tool = ctx.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
    return getattr(tool, "idname", "") or ""


def _is_transform_tool(ctx):
    return _is_sculpt(ctx) and _active_tool_id(ctx) in _TRANSFORM_TOOLS


# Pointer of the last 3D Viewport region the mouse was over (see STP_GT_hover_tracker).
_HOVER_REGION = None


def _view3d(ctx):
    """Return (window, screen, area, region, space, region_3d) of the 3D Viewport to use.
    Works from operators and from timers (where ctx.screen is None): there it uses the
    viewport region last under the mouse (several viewports, Quad View), else the largest."""
    area = getattr(ctx, "area", None)
    region = getattr(ctx, "region", None)
    if area and region and area.type == 'VIEW_3D' and region.type == 'WINDOW':
        return ctx.window, ctx.screen, area, region, ctx.space_data, ctx.region_data
    if _HOVER_REGION is not None:
        for window in ctx.window_manager.windows:
            for a in window.screen.areas:
                if a.type != 'VIEW_3D':
                    continue
                for r in a.regions:
                    if r.type == 'WINDOW' and r.as_pointer() == _HOVER_REGION:
                        return window, window.screen, a, r, a.spaces.active, r.data
    for window in ctx.window_manager.windows:
        areas = sorted((a for a in window.screen.areas if a.type == 'VIEW_3D'),
                       key=lambda a: a.width * a.height, reverse=True)   # largest viewport first
        for a in areas:
            for r in a.regions:
                if r.type == 'WINDOW':
                    return window, window.screen, a, r, a.spaces.active, r.data
    return None, None, None, None, None, None


def _override(ctx):
    window, screen, area, region, space, _ = _view3d(ctx)
    if area is None:
        return None
    # No screen= : the screen comes with the window, and passing it fails while a
    # temporary screen is active (e.g. the add-on disabled from the Preferences window).
    return ctx.temp_override(window=window, area=area, region=region, space_data=space)


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ----------------------------------------------------------------------------
# Transform Only Pivot (native, Blender 5.3+)
# ----------------------------------------------------------------------------
def _pivot_only(ctx):
    return ctx.scene.tool_settings.use_transform_data_pivot


def _set_pivot_only(ctx, value):
    ctx.scene.tool_settings.use_transform_data_pivot = value
    _tag_redraw()


class _PivotOnly:
    """Context manager: temporarily enable Transform Only Pivot so a transform operator
    affects only the native pivot. Restores the previous state afterwards."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.prev = None

    def __enter__(self):
        ts = self.ctx.scene.tool_settings
        self.prev = ts.use_transform_data_pivot
        ts.use_transform_data_pivot = True
        return ts.use_transform_data_pivot   # safety check for the caller

    def __exit__(self, *_exc):
        self.ctx.scene.tool_settings.use_transform_data_pivot = self.prev
        return False


def _rotate_op(delta):
    """Run Blender's rotate by the world-space 3x3 rotation `delta`, about the sculpt pivot."""
    axis, angle = delta.to_quaternion().to_axis_angle()
    if abs(angle) < 1e-6:
        return
    orient = Vector(axis).to_track_quat('Z', 'Y').to_matrix()
    # Measured in Blender 5.3: a constrained rotation of +value turns about +axis.
    bpy.ops.transform.rotate(value=angle, orient_axis='Z', orient_type='GLOBAL',
                             orient_matrix=orient, orient_matrix_type='GLOBAL',
                             constraint_axis=(False, False, True), snap=False)


def _rotate_native_pivot(ctx, delta):
    """Rotate only the native sculpt pivot by the world-space 3x3 rotation `delta`."""
    ov = _override(ctx)
    if ov is None:
        return False
    with ov, _PivotOnly(ctx) as pivot_only:
        if not pivot_only:          # never rotate the mesh
            return False
        _rotate_op(delta)
    return True


# A button that moves the mesh runs native transforms; Blender pushes their Sculpt undo
# step(s) when the button finishes (one or two steps). The pivot state before each transform
# is collected here and given to those steps by the timer (_assign_button_steps).
_BUTTON_RUN = None          # (stack pointer before the button, [(rotation, position) before each transform])


def _begin_mesh_button(ctx):
    global _BUTTON_RUN
    stack = ctx.window_manager.undo_stack
    _BUTTON_RUN = (stack.active.as_pointer() if stack.active else None, [])


def _note_before():
    if _BUTTON_RUN is not None:
        _BUTTON_RUN[1].append((_TRACKED.copy(), _pivot_pos_copy()))


def _assign_button_steps(ctx):
    """Give the undo steps pushed by a mesh button the pivot state from before each transform.
    Returns True when it handled the steps (the timer must not overwrite them)."""
    global _BUTTON_RUN, _LAST_ACTIVE_PTR
    if _BUTTON_RUN is None:
        return False
    start_ptr, befores = _BUTTON_RUN
    _BUTTON_RUN = None
    stack = ctx.window_manager.undo_stack
    if not befores or stack.active is None:
        return False
    start = _step_index(stack, start_ptr) if start_ptr else None
    first = (start + 1) if start is not None else stack.active_index
    count = stack.active_index + 1 - first
    for j, i in enumerate(range(first, stack.active_index + 1)):
        step = stack.steps[i]
        # Measured in Blender 5.3: right after a Global undo step (a click, a pivot-only
        # transform...), the first transform does not push its own step; it is merged into
        # the next one. Undoing that merged step restores the whole mesh, but the native
        # pivot goes back to its state before the LAST transform, so the last step always
        # gets the last "before".
        k = len(befores) - 1 if j == count - 1 else min(j, len(befores) - 1)
        rot, pos = befores[k]
        info = (step.type == 'SCULPT', rot, pos)
        _STEP_INFO[step.as_pointer()] = info
        _STEP_BY_INDEX[i] = info
    _LAST_ACTIVE_PTR = stack.active.as_pointer()
    return True


def _is_straight():
    """True when the tracked pivot rotation is (numerically) Global."""
    return abs(_TRACKED.to_quaternion().to_axis_angle()[1]) < 1e-6


def _rotate_mesh(ctx, delta):
    """Rotate the unmasked mesh (and the pivot with it) about the pivot. Gizmo locked only."""
    ov = _override(ctx)
    if ov is None or _pivot_only(ctx):
        return False
    axis, angle = delta.to_quaternion().to_axis_angle()
    if abs(angle) < 1e-6:
        return True                            # already straight: no undo step
    _note_before()
    with ov:
        _rotate_op(delta)
    return True


def _move_mesh(ctx, delta):
    """Move the unmasked mesh (and the pivot with it) by a world-space vector. Gizmo locked only."""
    ov = _override(ctx)
    if ov is None or _pivot_only(ctx):
        return False
    if Vector(delta).length < 1e-7:
        return True                            # already there: no undo step
    _note_before()
    with ov:
        bpy.ops.transform.translate(value=tuple(delta), orient_type='GLOBAL', snap=False)
    return True


def _move_native_pivot(ctx, obj, target):
    """Place the native sculpt pivot at a world-space point without touching the mesh:
    snap it to the object origin, then translate only the pivot."""
    ov = _override(ctx)
    if ov is None:
        return False
    with ov, _PivotOnly(ctx) as pivot_only:
        if not pivot_only:          # never move the mesh
            return False
        bpy.ops.sculpt.set_pivot_position('EXEC_DEFAULT', mode='ORIGIN')
        delta = target - obj.matrix_world.translation
        bpy.ops.transform.translate(value=tuple(delta), orient_type='GLOBAL', snap=False)
    _set_pivot_pos(target)
    return True


def _set_native_pivot(ctx, mode):
    ov = _override(ctx)
    if ov is None:
        return False
    with ov:
        bpy.ops.sculpt.set_pivot_position('EXEC_DEFAULT', mode=mode)
    _set_pivot_pos(_expected_pivot_pos(ctx, mode))
    return True


# ----------------------------------------------------------------------------
# Gizmo orientation (PIVOT_ORIENTATION custom orientation)
# ----------------------------------------------------------------------------
# The tracked rotation of the native pivot. It lives in Python, not in the scene, because
# global undo steps restore scene data (including custom orientations) on Ctrl+Z.
_TRACKED = Matrix.Identity(3)
# Tracked world position of the native pivot (Blender does not expose it either).
# None = unknown (e.g. Multires mask pivots, which Python cannot compute): the buttons
# then wait at the top of the viewport instead of floating at a wrong place.
_PIVOT_POS = Vector((0.0, 0.0, 0.0))
# Transform orientation the user had before the add-on took over the gizmo in Sculpt Mode.
_USER_ORIENTATION = None


def _remember_user_orientation(slot):
    global _USER_ORIENTATION
    if _USER_ORIENTATION is None and slot.type != PIVOT_ORIENTATION:
        _USER_ORIENTATION = slot.type


def _set_pivot_pos(location):
    """Set the tracked pivot position; None marks it as unknown."""
    global _PIVOT_POS
    _PIVOT_POS = Vector(location) if location is not None else None
    _tag_redraw()


def _pivot_pos_copy():
    return _PIVOT_POS.copy() if _PIVOT_POS is not None else None


def _set_orientation(ctx, matrix):
    """Make the gizmo use PIVOT_ORIENTATION with the given matrix (created if needed)."""
    global _TRACKED
    _TRACKED = matrix.copy()
    slot = ctx.scene.transform_orientation_slots[0]
    _remember_user_orientation(slot)
    try:
        slot.type = PIVOT_ORIENTATION
    except TypeError:
        ov = _override(ctx)
        if ov is None:
            return False
        with ov:
            bpy.ops.transform.create_orientation(name=PIVOT_ORIENTATION, use=True, overwrite=True)
        slot.type = PIVOT_ORIENTATION
    if slot.custom_orientation is None:
        return False
    slot.custom_orientation.matrix = matrix
    # Re-assigning the type sends the update notifier, so the gizmo redraws immediately.
    slot.type = 'GLOBAL'
    slot.type = PIVOT_ORIENTATION
    _tag_redraw()
    return True


def _object_rotation(ctx):
    """Rotation of the native pivot when Sculpt Mode starts: the object's own axes."""
    obj = ctx.active_object
    if obj is None:
        return Matrix.Identity(3)
    m = obj.matrix_world.to_3x3()
    m.normalize()
    return m


def _set_entry_orientation(ctx):
    """Gizmo matching the native pivot right after entering Sculpt Mode."""
    _apply_matrix(ctx, _object_rotation(ctx))


def _set_global(ctx, force=False):
    """Gizmo in Global orientation. `force` (in Sculpt Mode) also replaces an orientation the
    user picked, which would not match the pivot; it is given back when leaving Sculpt Mode."""
    global _TRACKED
    _TRACKED = Matrix.Identity(3)
    slot = ctx.scene.transform_orientation_slots[0]
    if force:
        _remember_user_orientation(slot)
    if force or slot.type == PIVOT_ORIENTATION:
        slot.type = 'GLOBAL'
    _tag_redraw()


def _restore_user_orientation(ctx):
    """Leaving Sculpt Mode: give the gizmo back the user's orientation and remove the
    add-on's custom orientation, so nothing is left in the scene or saved in the file."""
    global _USER_ORIENTATION
    slot = ctx.scene.transform_orientation_slots[0]
    try:
        slot.type = PIVOT_ORIENTATION
    except TypeError:
        pass                                   # not in this scene
    else:
        ov = _override(ctx)
        if ov is not None:
            with ov:
                bpy.ops.transform.delete_orientation()
    if slot.type in {PIVOT_ORIENTATION, 'GLOBAL'} and _USER_ORIENTATION is not None:
        try:
            slot.type = _USER_ORIENTATION
        except TypeError:
            slot.type = 'GLOBAL'
    elif slot.type == PIVOT_ORIENTATION:
        slot.type = 'GLOBAL'
    _USER_ORIENTATION = None
    _tag_redraw()


# Pivot Point is hidden in Sculpt Mode, but the Transform tool still follows it: with
# 3D Cursor or Active Element the gizmo leaves the sculpt pivot. In Sculpt Mode it is set
# to Median Point; the user's choice is kept on the scene (saved with the file) and given
# back when leaving Sculpt Mode.
_PIVOT_POINTS_OFF_PIVOT = {'CURSOR', 'ACTIVE_ELEMENT'}


def _use_sculpt_pivot_point(ctx):
    scene = ctx.scene
    ts = scene.tool_settings
    if ts.transform_pivot_point in _PIVOT_POINTS_OFF_PIVOT:
        if not scene.stp_user_pivot_point:
            scene.stp_user_pivot_point = ts.transform_pivot_point
        ts.transform_pivot_point = 'MEDIAN_POINT'


def _restore_user_pivot_point(ctx):
    scene = ctx.scene
    if scene is None or not scene.stp_user_pivot_point:
        return
    ts = scene.tool_settings
    if ts.transform_pivot_point == 'MEDIAN_POINT':
        try:
            ts.transform_pivot_point = scene.stp_user_pivot_point
        except TypeError:
            pass
    scene.stp_user_pivot_point = ""


def _rotate_orientation(ctx, delta):
    new = delta @ _TRACKED
    new.normalize()
    return _set_orientation(ctx, new)


def _surface_matrix(normal, rv3d):
    """Orientation with Z along the surface normal and X following the view's right."""
    z = Vector(normal).normalized()
    right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
    x = right - z * right.dot(z)
    if x.length < 1e-5:
        up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
        x = up - z * up.dot(z)
    x.normalize()
    m = Matrix.Identity(3)
    m.col[0] = x
    m.col[1] = z.cross(x).normalized()
    m.col[2] = z
    return m


def _align_to_surface(ctx, normal, rv3d):
    """Align gizmo AND native pivot to the surface normal."""
    new = _surface_matrix(normal, rv3d)
    _rotate_native_pivot(ctx, new @ _TRACKED.inverted())
    return _set_orientation(ctx, new)


def _reset_orientation(ctx):
    """Gizmo back to Global and undo the tracked rotation of the native pivot
    (the pivot keeps its position)."""
    _rotate_native_pivot(ctx, _TRACKED.inverted())
    _set_global(ctx, force=True)


# ----------------------------------------------------------------------------
# Tracking: keep the gizmo in sync with the native pivot rotation
# ----------------------------------------------------------------------------
# The native pivot rotates with every rotation done in Sculpt Mode (mesh or pivot only).
# Each finished rotate/trackball operator is read from the operator history and the
# same rotation is applied to PIVOT_ORIENTATION. Conventions measured in Blender 5.3.
_LAST_OP_PTR = None
_LAST_OP_SIG = None


def _op_sig(op):
    """Pointer + values: the redo panel (Adjust Last Operation) re-runs an operator in place,
    keeping its pointer but changing its values."""
    p = op.properties
    vals = [op.as_pointer(), op.bl_idname]
    for name in ("value", "orient_axis", "constraint_axis", "orient_matrix", "mode", "mouse_x", "mouse_y"):
        if hasattr(p, name):
            v = getattr(p, name)
            try:
                v = tuple(tuple(row) for row in v) if name == "orient_matrix" else (
                    tuple(v) if hasattr(v, "__len__") and not isinstance(v, str) else v)
            except Exception:
                v = str(v)
            vals.append(v)
    return tuple(vals)


def _mark_ops_seen():
    global _LAST_OP_PTR, _LAST_OP_SIG
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None or not len(wm.operators):
        _LAST_OP_PTR, _LAST_OP_SIG = None, None
        return
    _LAST_OP_PTR = wm.operators[-1].as_pointer()
    _LAST_OP_SIG = _op_sig(wm.operators[-1])


def _new_ops(ctx):
    """Every operator finished since the last check, oldest first (not only the last one)."""
    ops = list(ctx.window_manager.operators)
    if not ops:
        return []
    for i in range(len(ops) - 1, -1, -1):
        if ops[i].as_pointer() == _LAST_OP_PTR:
            if i == len(ops) - 1:
                return [] if _op_sig(ops[i]) == _LAST_OP_SIG else [ops[i]]   # redo panel
            return ops[i + 1:]
    return ops[-1:]                             # history rolled over: only trust the last one


def _track_rotation(op, ctx):
    p = op.properties
    if op.bl_idname == "TRANSFORM_OT_rotate":
        axis = Vector(Matrix(p.orient_matrix).col[_AXIS_INDEX.get(p.orient_axis, 2)])
        if axis.length < 1e-8:
            return
        # Colored rings (constrained): +value about the axis.
        # White view ring (unconstrained): Blender turns about the negated view axis.
        angle = p.value if any(p.constraint_axis) else -p.value
        _rotate_orientation(ctx, Matrix.Rotation(angle, 3, axis.normalized()))
    elif op.bl_idname == "TRANSFORM_OT_trackball":
        # Trackball: one rotation of |value| about (v0 * view X + v1 * view Y).
        rv3d = _view3d(ctx)[5]
        if rv3d is None:
            return
        view_inv = rv3d.view_matrix.inverted().to_3x3()
        axis = view_inv.col[0].normalized() * p.value[0] + view_inv.col[1].normalized() * p.value[1]
        if axis.length > 1e-9:
            _rotate_orientation(ctx, Matrix.Rotation(axis.length, 3, axis.normalized()))


def _raycast_active(ctx, region, rv3d, coord, want_index=False):
    """Hit (location, normal) on the active object only, in world space, or None.
    Other objects in the scene are ignored, like Blender's own Set Pivot > Surface.
    With want_index, also the index of the face hit (evaluated mesh)."""
    obj = ctx.active_object
    if obj is None or obj.type != 'MESH':
        return None
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    # Measured in Blender 5.3: Sculpt Mode transforms (gizmo, Home, Reset...) update the
    # mesh but not its evaluated copy, so a ray would hit the shape from before the
    # transform. Tag the mesh so the depsgraph gives the current shape.
    obj.update_tag(refresh={'DATA'})
    obj_eval = obj.evaluated_get(ctx.evaluated_depsgraph_get())
    mw = obj.matrix_world
    mw_inv = mw.inverted()
    local_origin = mw_inv @ origin
    local_dir = (mw_inv.to_3x3() @ direction).normalized()
    hit, loc, normal, index = obj_eval.ray_cast(local_origin, local_dir)
    if not hit:
        return None
    normal_world = (mw_inv.to_3x3().transposed() @ normal).normalized()
    if want_index:
        return mw @ loc, normal_world, index
    return mw @ loc, normal_world


def _mesh_world(obj):
    co = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def _symmetry_side(obj, co):
    """Vertices Blender uses for the Unmasked and Mask Border pivots: with mesh symmetry on,
    only the side of each mirror axis where the current pivot is. True for all when there is
    no symmetry; None when the side cannot be known (pivot unknown or on the mirror plane)."""
    axes = [i for i, on in enumerate((obj.use_mesh_mirror_x, obj.use_mesh_mirror_y,
                                       obj.use_mesh_mirror_z)) if on]
    keep = np.ones(len(co), dtype=bool)
    if not axes:
        return keep
    if _PIVOT_POS is None:
        return None
    pivot = obj.matrix_world.inverted_safe() @ _PIVOT_POS
    for i in axes:
        if abs(pivot[i]) < 1e-4:
            return None                        # on the mirror plane: the side is a float sign
        keep &= (co[:, i] <= 0.0) if pivot[i] < 0.0 else (co[:, i] >= 0.0)
    return keep


def _expected_pivot_pos(ctx, mode, mouse=None):
    """Where Blender's Set Pivot Position puts the pivot, computed from the mesh.
    None when it cannot be known from Python."""
    obj = ctx.active_object
    if obj is None or obj.type != 'MESH':
        return None
    mw = obj.matrix_world
    if mode == 'ORIGIN':
        return mw.translation.copy()
    if mode in {'UNMASKED', 'BORDER'}:
        if (any(m.type == 'MULTIRES' for m in obj.modifiers)
                or obj.use_dynamic_topology_sculpting):
            return None                        # mask not readable / mesh not up to date
        mask = _mask_values(obj)
        co = _mesh_world(obj)
        side = _symmetry_side(obj, co)
        if side is None:
            return None                        # symmetry side unknown
        if mode == 'BORDER' and mask is not None:
            border = _mask_border_world(obj, mask, side)
            if border is not None:
                return border
        if mask is not None and (mask < 0.5).any():
            side = side & (mask < 0.5)
        co = co[side]
        return mw @ Vector(co.mean(axis=0)) if len(co) else None
    if mode == 'SURFACE' and mouse is not None:
        _, _, area, region, _, rv3d = _view3d(ctx)
        if rv3d is None:
            return None
        hit = _raycast_active(ctx, region, rv3d, mouse)
        return hit[0] if hit else None
    return None                                # ACTIVE: not readable from Python


def _track_position(op, ctx):
    """Follow the native pivot position from finished operators (measured in Blender 5.3):
    moves shift the pivot (gizmo locked or not), rotations and scales keep it in place."""
    p = op.properties
    if op.bl_idname == "TRANSFORM_OT_translate":
        if _PIVOT_POS is not None:             # an unknown position stays unknown
            _set_pivot_pos(_PIVOT_POS + Matrix(p.orient_matrix) @ Vector(p.value))
    elif op.bl_idname == "SCULPT_OT_set_pivot_position":
        pos = _expected_pivot_pos(ctx, p.mode, (p.mouse_x, p.mouse_y))
        if pos is not None or p.mode != 'SURFACE':     # a surface miss leaves the pivot
            _set_pivot_pos(pos)
    elif op.bl_idname == "SCULPT_OT_expand" and p.use_reposition_pivot and p.target == 'MASK':
        _set_pivot_pos(_expected_pivot_pos(ctx, 'BORDER'))


def _apply_matrix(ctx, matrix):
    identity = Matrix.Identity(3)
    if all(abs(matrix[i][j] - identity[i][j]) < 1e-6 for i in range(3) for j in range(3)):
        _set_global(ctx, force=True)
    else:
        _set_orientation(ctx, matrix)


# Undo/redo, as measured in Blender 5.3:
# - Steps that edit the mesh (transforms with the gizmo locked, brush strokes, masks...)
#   store the pivot as it was BEFORE the step. Undoing or redoing such a step puts the
#   native pivot back to that stored state.
# - Pivot-only transforms do not store the pivot: undo/redo leaves it untouched.
# The add-on records, for every new undo step, its kind and the pivot rotation before it,
# and replays exactly that rule on undo/redo. Records are keyed by step pointer but are
# always overwritten when a step is pushed (Blender reuses freed step memory).
_STEP_INFO = {}             # step pointer -> (restores_pivot, rotation_before, position_before)
_STEP_BY_INDEX = {}         # same records by stack index (redo panel may reallocate a step)
_LAST_ACTIVE_PTR = None
_UNDO_EVENT = None          # 'UNDO' / 'REDO', set by the undo handlers
_MESH_OPS = {"STP_OT_mesh_to_world_origin", "STP_OT_straighten_mesh"}   # buttons that move the mesh


def _step_index(stack, ptr):
    for i, step in enumerate(stack.steps):
        if step.as_pointer() == ptr:
            return i
    return None


def _apply_undo_rule(ctx, direction):
    stack = ctx.window_manager.undo_stack
    new_index = stack.active_index
    old_index = _step_index(stack, _LAST_ACTIVE_PTR) if _LAST_ACTIVE_PTR else None
    if old_index is None or new_index == old_index:
        _apply_matrix(ctx, _TRACKED.copy())
        _tag_redraw()
        return
    lo, hi = sorted((old_index, new_index))
    touched = range(lo + 1, hi + 1)            # steps undone or redone
    order = touched if direction == 'REDO' else reversed(touched)
    result = None
    for i in order:                            # the last restoring step applied wins
        info = _STEP_INFO.get(stack.steps[i].as_pointer())
        if info and info[0]:
            result = info
    if result is not None:
        _set_pivot_pos(result[2])
        _apply_matrix(ctx, result[1].copy())
    else:
        _apply_matrix(ctx, _TRACKED.copy())    # scene data may have been restored
    _tag_redraw()


def _process(ctx, ops):
    """Track the given operators; returns False if one of them was a pivot-only edit."""
    restores_pivot = True
    for op in ops:
        if not _is_sculpt(ctx):
            continue
        _track_rotation(op, ctx)
        _track_position(op, ctx)
        if op.bl_idname.startswith("TRANSFORM_OT_") and _pivot_only(ctx):
            restores_pivot = False             # pivot-only transform
        if op.bl_idname.startswith("STP_OT_") and op.bl_idname not in _MESH_OPS:
            restores_pivot = False             # the add-on's pivot-only buttons
    return restores_pivot


def _store_step(stack, restores_pivot, before, before_pos):
    """Remember how undo/redo of the active step affects the native pivot."""
    info = (restores_pivot and stack.active.type == 'SCULPT', before, before_pos)
    _STEP_INFO[stack.active.as_pointer()] = info
    _STEP_BY_INDEX[stack.active_index] = info


def _record_step(ctx, restores_pivot, before, before_pos):
    global _LAST_ACTIVE_PTR
    stack = ctx.window_manager.undo_stack
    active_ptr = stack.active.as_pointer() if stack.active else None
    if active_ptr != _LAST_ACTIVE_PTR:         # a new step was pushed
        _LAST_ACTIVE_PTR = active_ptr
        if active_ptr is not None:
            _store_step(stack, restores_pivot, before, before_pos)
        alive = {step.as_pointer() for step in stack.steps}
        for key in list(_STEP_INFO):
            if key not in alive:
                del _STEP_INFO[key]


def _sync(ctx):
    """Called by the timer: track new operators and undo steps."""
    global _LAST_ACTIVE_PTR, _UNDO_EVENT
    stack = ctx.window_manager.undo_stack

    if _UNDO_EVENT is not None:
        new = _new_ops(ctx)
        # Adjust Last Operation = undo of the last step + the same operator run again with
        # new values (same operator pointer). Undo that step by its own rule, then re-track.
        redo_last = len(new) == 1 and new[0].as_pointer() == _LAST_OP_PTR
        if _is_sculpt(ctx):
            if redo_last:
                info = (_STEP_INFO.get(stack.active.as_pointer()) if stack.active else None) \
                    or _STEP_BY_INDEX.get(stack.active_index)
                if info and info[0]:           # mesh step: pivot back to its state before
                    _set_pivot_pos(info[2])
                    _apply_matrix(ctx, info[1].copy())
            else:
                _apply_undo_rule(ctx, _UNDO_EVENT)
        _UNDO_EVENT = None
        _LAST_ACTIVE_PTR = stack.active.as_pointer() if stack.active else None
        if new:
            before, before_pos = _TRACKED.copy(), _pivot_pos_copy()
            restores = _process(ctx, new)
            if stack.active is not None:
                _store_step(stack, restores, before, before_pos)
            _tag_redraw()
        _mark_ops_seen()
        return

    before = _TRACKED.copy()
    before_pos = _pivot_pos_copy()
    _assign_button_steps(ctx)
    new = _new_ops(ctx)
    restores_pivot = True
    if new:
        _mark_ops_seen()
        _tag_redraw()                      # buttons wait for this to reappear
        restores_pivot = _process(ctx, new)
    _record_step(ctx, restores_pivot, before, before_pos)


_LAST_UNDO_TIME = 0.0


@persistent
def _on_undo_post(*_args):
    global _UNDO_EVENT, _LAST_UNDO_TIME
    _UNDO_EVENT = 'UNDO'
    _LAST_UNDO_TIME = time.monotonic()


@persistent
def _on_redo_post(*_args):
    global _UNDO_EVENT, _LAST_UNDO_TIME
    _UNDO_EVENT = 'REDO'
    _LAST_UNDO_TIME = time.monotonic()


# ----------------------------------------------------------------------------
# Pivot memory per object
# ----------------------------------------------------------------------------
# Each object remembers where its pivot was left (position and rotation, in the object's
# own space so it follows the object). It is stored in the object, so it is saved in the
# .blend file. Registered properties stay out of the Custom Properties panel.
_MEM_NONE, _MEM_FULL, _MEM_ROTATION = 0, 1, 2    # stp_pivot_state values


def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _remember_enabled():
    p = _prefs()
    return True if p is None else p.remember_pivot


def _entry_center_enabled():
    p = _prefs()
    return True if p is None else p.entry_pivot == 'MESH_CENTER'


def _bbox_center_world(obj):
    corners = [Vector(c) for c in obj.bound_box]
    return obj.matrix_world @ (sum(corners, Vector()) / 8.0)


def _store_pivot_memory(ctx):
    """Write the tracked pivot of the active object into it (only when it changed)."""
    if not _is_sculpt(ctx) or not _remember_enabled() or _LAST_MODE_KEY != _mode_key(ctx):
        return
    obj = ctx.active_object
    if obj is None or obj.library is not None:
        return
    rot = (_object_rotation(ctx).inverted() @ _TRACKED).to_quaternion()
    if _PIVOT_POS is not None:
        pos, state = list(obj.matrix_world.inverted() @ _PIVOT_POS), _MEM_FULL
    else:                                      # position unknown (Multires/Dyntopo mask pivots)
        pos, state = list(obj.stp_pivot[:3]), _MEM_ROTATION
    data = pos + list(rot)
    if obj.stp_pivot_state == state and all(abs(a - b) < 1e-6 for a, b in zip(obj.stp_pivot, data)):
        return
    obj.stp_pivot = data
    obj.stp_pivot_state = state


def _restore_pivot_memory(ctx):
    """Right after entering Sculpt Mode (native pivot at the origin, object axes): move the
    pivot where it was left on this object, or to the mesh center the first time."""
    obj = ctx.active_object
    if obj is None or obj.type != 'MESH':
        return
    entry = _object_rotation(ctx)
    state = obj.stp_pivot_state if _remember_enabled() else _MEM_NONE
    target_pos = target_rot = None
    if state != _MEM_NONE:
        data = obj.stp_pivot
        target_rot = entry @ Quaternion(data[3:7]).normalized().to_matrix()
        if state == _MEM_FULL:
            target_pos = obj.matrix_world @ Vector(data[:3])
    if target_pos is None and _entry_center_enabled():
        target_pos = _bbox_center_world(obj)
    if target_pos is not None:
        _move_native_pivot(ctx, obj, target_pos)
    if target_rot is not None:
        _rotate_native_pivot(ctx, target_rot @ entry.inverted())
        _set_orientation(ctx, target_rot)


# Entering Sculpt Mode (Tab, switching objects, opening a file) resets the native pivot.
_LAST_MODE_KEY = None
_MSGBUS_OWNER = object()


def _mode_key(ctx):
    ob = getattr(ctx, "active_object", None)
    return (ob.as_pointer() if ob else None, getattr(ctx, "mode", None))


def _sync_mode(ctx, force=False):
    global _LAST_MODE_KEY
    key = _mode_key(ctx)
    if key == _LAST_MODE_KEY and not force:
        return
    was_sculpt = _LAST_MODE_KEY is not None and _LAST_MODE_KEY[1] == 'SCULPT'
    _LAST_MODE_KEY = key
    if was_sculpt and ctx.mode != 'SCULPT':
        _restore_user_orientation(ctx)
        _restore_user_pivot_point(ctx)
    if ctx.mode == 'SCULPT':
        _use_sculpt_pivot_point(ctx)
        _set_entry_orientation(ctx)      # native pivot: object axes...
        _set_pivot_pos(ctx.active_object.matrix_world.translation)   # ...at the object origin
        try:
            _restore_pivot_memory(ctx)   # then: where it was left, or the mesh center
        finally:
            _mark_ops_seen()    # never replay operators from before the change


def _on_mode_changed(*_args):
    # Undo/redo of global steps re-sends Object.mode without leaving Sculpt Mode; the
    # native pivot is not reset then. Real mode changes are still caught by the timer.
    now = time.monotonic()
    if now - _LAST_UNDO_TIME < 1.0:
        return
    # Only a real change (other object or mode) restores the pivot: Object.mode is also
    # re-sent while staying in Sculpt Mode (other objects changing, other add-ons), and a
    # second restore would rotate the native pivot twice.
    _sync_mode(bpy.context)


def _subscribe_mode():
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    bpy.msgbus.subscribe_rna(key=(bpy.types.Object, "mode"), owner=_MSGBUS_OWNER,
                             args=(), notify=_on_mode_changed)


@persistent
def _on_load_post(*_args):
    global _LAST_MODE_KEY, _LAST_ACTIVE_PTR, _UNDO_EVENT, _USER_ORIENTATION
    _STEP_INFO.clear()
    _STEP_BY_INDEX.clear()
    _LAST_ACTIVE_PTR = None
    _UNDO_EVENT = None
    _LAST_MODE_KEY = None
    _USER_ORIENTATION = None
    _mark_ops_seen()
    _subscribe_mode()
    _set_global(bpy.context)


_WAS_TRANSFORM_TOOL = False


def _sync_tool(ctx):
    """Leaving the transform tools (other tool, brush or mode) turns Pivot Only off, so the
    lock is always closed when coming back."""
    global _WAS_TRANSFORM_TOOL
    now = _is_transform_tool(ctx)
    if _WAS_TRANSFORM_TOOL and not now and ctx.scene.tool_settings.use_transform_data_pivot:
        ctx.scene.tool_settings.use_transform_data_pivot = False
        _tag_redraw()
    _WAS_TRANSFORM_TOOL = now


def _keep_sculpt_pivot_point(ctx):
    """Also covers enabling the add-on while already in Sculpt Mode."""
    if _is_sculpt(ctx):
        _use_sculpt_pivot_point(ctx)


def _watch_timer():
    ctx = bpy.context
    for step in (_sync_mode, _sync, _sync_tool, _keep_sculpt_pivot_point, _store_pivot_memory):
        try:
            step(ctx)
        except Exception:
            pass
    try:
        busy = any(len(w.modal_operators) for w in ctx.window_manager.windows)
    except Exception:
        busy = False
    return 0.02 if busy else 0.1         # react right after a drag ends


# ----------------------------------------------------------------------------
# Mask (read from the mesh; also used to follow Blender's own Mask Border pivots)
# ----------------------------------------------------------------------------
def _mask_values(obj):
    attr = obj.data.attributes.get(".sculpt_mask")
    if attr is None:
        return None
    values = np.empty(len(attr.data), dtype=np.float32)
    attr.data.foreach_get("value", values)
    return values


def _mask_border_world(obj, mask, side=None):
    """World-space center of the vertices on the masked/unmasked boundary.
    Works with hard masks (no smoothing needed)."""
    me = obj.data
    hard = mask >= 0.5
    if hard.all() or not hard.any():
        return None
    edges = np.empty(len(me.edges) * 2, dtype=np.int32)
    me.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    crossing = hard[edges[:, 0]] != hard[edges[:, 1]]
    if not crossing.any():
        return None
    border = np.unique(edges[crossing].ravel())
    if side is not None:
        border = border[side[border]]
        if not len(border):
            return None
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    return obj.matrix_world @ Vector(co.reshape(-1, 3)[border].mean(axis=0))


def _brush_uses_stencil(ctx):
    if _active_tool_id(ctx) in _TRANSFORM_TOOLS:
        return False
    brush = ctx.tool_settings.sculpt.brush
    if brush is None:
        return False
    tex = brush.texture_slot
    mask_tex = brush.mask_texture_slot
    return bool((tex and tex.texture and tex.map_mode == 'STENCIL') or
                (mask_tex and mask_tex.texture and mask_tex.mask_map_mode == 'STENCIL'))


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------
def _tooltip(name, text):
    """Button tooltip: the button name on the first line, then what it does."""
    return f"{name}\n{text}"


class STP_OT_pivot_to_surface(Operator):
    """Set the pivot on the surface under the cursor and align the gizmo to the surface normal"""
    bl_idname = "stp.pivot_to_surface"
    bl_label = "Pivot to Surface"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context)

    def invoke(self, context, event):
        if _brush_uses_stencil(context):
            return {'PASS_THROUGH'}          # keep Blender's stencil scaling
        _, _, area, region, _, rv3d = _view3d(context)
        if rv3d is None:
            return {'PASS_THROUGH'}
        coord = (event.mouse_region_x, event.mouse_region_y)
        hit = _raycast_active(context, region, rv3d, coord)
        if hit is None:
            return {'PASS_THROUGH'}             # missed the sculpted object: Blender does nothing either
        location, normal = hit
        with _override(context):
            bpy.ops.sculpt.set_pivot_position('EXEC_DEFAULT', mode='SURFACE',
                                              mouse_x=coord[0], mouse_y=coord[1])
        _set_pivot_pos(location)
        _align_to_surface(context, normal, rv3d)
        return {'FINISHED'}


class STP_OT_pivot_to_unmasked_center(Operator):
    """Pivot to the center of the unmasked area (the mesh center when nothing is masked)"""
    bl_idname = "stp.pivot_to_unmasked_center"
    bl_label = "Pivot to Unmasked Center"
    bl_options = {'REGISTER'}

    @classmethod
    def description(cls, context, properties):
        return _tooltip("Unmasked Center", cls.__doc__)

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context)

    def execute(self, context):
        obj = context.active_object
        readable = not (any(m.type == 'MULTIRES' for m in obj.modifiers)
                        or obj.use_dynamic_topology_sculpting)
        if readable:
            mask = _mask_values(obj)
            co = _mesh_world(obj)
            if mask is None or not (mask > 0.0).any():
                _reset_orientation(context)    # nothing masked: start over in Global
            elif (mask < 0.5).any():
                co = co[mask < 0.5]
            # Placed by the add-on instead of Blender's Unmasked mode, which with mesh
            # symmetry only averages the side of the current pivot (and picks that side
            # by a float sign when the pivot is on the mirror plane).
            if len(co) and _move_native_pivot(context, obj, obj.matrix_world @ Vector(co.mean(axis=0))):
                return {'FINISHED'}
        # Multires/Dyntopo: Python cannot read the mask, so the pivot position stays
        # unknown to the add-on and the buttons wait at the top of the viewport.
        _set_native_pivot(context, 'UNMASKED')
        return {'FINISHED'}


class STP_OT_pivot_to_world_origin(Operator):
    """Pivot to the world origin with Global orientation. The mesh, the object origin and the 3D Cursor are not changed"""
    bl_idname = "stp.pivot_to_world_origin"
    bl_label = "Pivot to World Origin"
    bl_options = {'REGISTER'}

    @classmethod
    def description(cls, context, properties):
        return _tooltip("World Origin", cls.__doc__ + ". Lock the gizmo to move the mesh instead")

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context)

    def execute(self, context):
        _reset_orientation(context)
        if not _move_native_pivot(context, context.active_object, Vector((0.0, 0.0, 0.0))):
            _set_native_pivot(context, 'ORIGIN')
        return {'FINISHED'}


class STP_OT_mesh_to_world_origin(Operator):
    """Straighten the unmasked mesh to Global axes and move it so the pivot lands on the world origin."""
    bl_idname = "stp.mesh_to_world_origin"
    bl_label = "Mesh to World Origin"
    bl_options = {'REGISTER', 'UNDO'}      # the mesh changes: needs its own undo step

    @classmethod
    def description(cls, context, properties):
        return _tooltip("World Origin", cls.__doc__ + ". Unlock the gizmo to move only the pivot")

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context) and not _pivot_only(context)

    def execute(self, context):
        if _PIVOT_POS is None:
            self.report({'WARNING'}, "Pivot position unknown here: place it first "
                                     "(Shift + right click or Unmasked Center)")
            return {'CANCELLED'}
        if _is_straight() and _PIVOT_POS.length < 1e-7:
            return {'CANCELLED'}               # already there: no empty undo step
        _begin_mesh_button(context)
        _rotate_mesh(context, _TRACKED.inverted())     # straighten about the pivot first
        _set_global(context, force=True)
        if not _move_mesh(context, -_PIVOT_POS):
            return {'CANCELLED'}
        _set_pivot_pos(Vector((0.0, 0.0, 0.0)))
        return {'FINISHED'}


class STP_OT_reset_orientation(Operator):
    """Pivot and gizmo back to Global axes, keeping the pivot position. The mesh is not changed"""
    bl_idname = "stp.reset_orientation"
    bl_label = "Reset Pivot Orientation"
    bl_options = {'REGISTER'}

    @classmethod
    def description(cls, context, properties):
        return _tooltip("Reset Orientation", cls.__doc__ + ". Lock the gizmo to turn the mesh instead")

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context)

    def execute(self, context):
        _reset_orientation(context)
        return {'FINISHED'}


class STP_OT_straighten_mesh(Operator):
    """Turn the unmasked mesh about the pivot until the gizmo is back on Global axes"""
    bl_idname = "stp.straighten_mesh"
    bl_label = "Straighten Mesh"
    bl_options = {'REGISTER', 'UNDO'}      # the mesh changes: needs its own undo step

    @classmethod
    def description(cls, context, properties):
        return _tooltip("Reset Orientation", cls.__doc__ + ". Unlock the gizmo to reset only the pivot")

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context) and not _pivot_only(context)

    def execute(self, context):
        if _is_straight():
            _set_global(context, force=True)
            return {'CANCELLED'}               # already straight: no empty undo step
        _begin_mesh_button(context)
        if not _rotate_mesh(context, _TRACKED.inverted()):
            return {'CANCELLED'}
        _set_global(context, force=True)
        return {'FINISHED'}


class STP_OT_toggle_pivot_only(Operator):
    """Lock or unlock the gizmo. Unlocked, the gizmo moves and rotates only the pivot, not the mesh"""
    bl_idname = "stp.toggle_pivot_only"
    bl_label = "Lock / Unlock Gizmo"

    @classmethod
    def description(cls, context, properties):
        if _pivot_only(context):
            return _tooltip("Lock Gizmo", "The gizmo moves and rotates the mesh again")
        return _tooltip("Unlock Gizmo", "The gizmo moves and rotates only the pivot, not the mesh")

    @classmethod
    def poll(cls, context):
        return _is_sculpt(context)

    def invoke(self, context, event):
        if not _is_transform_tool(context):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        _set_pivot_only(context, not _pivot_only(context))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
# Distance from the pivot to the outer ring of Blender's transform gizmo, as a factor of
# Preferences > Viewport > Gizmo Size.
GIZMO_RING_FACTOR = 1.3

# All button icons are the add-on's own (named as in BUTTONS), independent of Blender's icon
# set. The masters are SVG files in icons/src; icons/<name>_<size>.png are renderings of them
# (add-ons cannot load SVG). _draw_custom_buttons() paints them.


def _top_overlap(ctx):
    """Height of the headers drawn over the top of the viewport (region overlap)."""
    region = ctx.region
    top = region.y + region.height
    overlap = 0
    for r in ctx.area.regions:
        if r.type in {'HEADER', 'TOOL_HEADER'} and r.height > 1 and r.y < top and r.y >= region.y:
            overlap = max(overlap, top - r.y)
    return overlap


class STP_GT_hover_tracker(Gizmo):
    """Invisible gizmo that only records which 3D Viewport region the mouse is over, so
    view-dependent tracking (trackball) uses the right view with several viewports or
    Quad View. It never draws and never takes a click."""
    bl_idname = "STP_GT_hover_tracker"

    def draw(self, context):
        pass

    def test_select(self, context, location):
        global _HOVER_REGION
        _HOVER_REGION = context.region.as_pointer()
        return -1


class STP_GGT_pivot_buttons(GizmoGroup):
    """Pivot buttons drawn above the transform gizmo. They follow the
    pivot on screen but never rotate with it."""
    bl_idname = "STP_GGT_pivot_buttons"
    bl_label = "Sculpt Pivot Buttons"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    # (operator, icon, shown when): None = always, 'LOCKED' / 'UNLOCKED' = gizmo state.
    # Locked, Home and Reset act on the mesh; unlocked, only on the pivot.
    BUTTONS = (
        ("stp.pivot_to_unmasked_center", "unmasked_center", None),
        ("stp.mesh_to_world_origin", "home", 'LOCKED'),
        ("stp.pivot_to_world_origin", "home", 'UNLOCKED'),
        ("stp.straighten_mesh", "reset", 'LOCKED'),
        ("stp.reset_orientation", "reset", 'UNLOCKED'),
        ("stp.toggle_pivot_only", "lock_closed", 'LOCKED'),    # custom icons (last on the right)
        ("stp.toggle_pivot_only", "lock_open", 'UNLOCKED'),
    )

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (_is_transform_tool(context) and space is not None
                and space.show_gizmo and space.show_gizmo_tool)

    def setup(self, context):
        self.gizmos.new(STP_GT_hover_tracker.bl_idname)
        self.buttons = []
        for op, _icon, _state in self.BUTTONS:
            gz = self.gizmos.new("GIZMO_GT_button_2d")
            gz.target_set_operator(op)
            # Invisible click areas: Blender draws image icons dimmer than its own icons,
            # so _draw_custom_buttons() paints every button.
            gz.draw_options = set()
            gz.alpha = 0.0
            gz.alpha_highlight = 0.0
            self.buttons.append(gz)

    def draw_prepare(self, context):
        region = context.region
        rv3d = context.region_data
        p2d = None
        if rv3d is not None and _PIVOT_POS is not None:
            p2d = view3d_utils.location_3d_to_region_2d(region, rv3d, _PIVOT_POS)
            if p2d is not None and not (0 <= p2d.x <= region.width and 0 <= p2d.y <= region.height):
                p2d = None                     # pivot off screen
        ui = context.preferences.system.ui_scale
        size = 13.0 * ui
        unlocked = _pivot_only(context)
        _CUSTOM_DRAW.clear()
        other = 'LOCKED' if unlocked else 'UNLOCKED'
        visible = []
        for gz, (_op, _icon, state) in zip(self.buttons, self.BUTTONS):
            if state == other:
                gz.hide = True
            else:
                visible.append(gz)
        if not _is_transform_tool(context) or _transform_running(context):
            for gz in self.buttons:
                gz.hide = True
            return
        step = size * 2.0 + 4.0 * ui
        if p2d is not None:                    # above the gizmo
            radius = context.preferences.view.gizmo_size * ui * GIZMO_RING_FACTOR
            x, y = p2d.x, p2d.y + radius + size + 10.0 * ui
        else:                                  # pivot position unknown or off screen
            x, y = region.width / 2.0, region.height - _top_overlap(context) - size - 12.0 * ui
        x0 = x - step * (len(visible) - 1) / 2.0
        for i, gz in enumerate(visible):
            gz.hide = False
            gz.scale_basis = size
            gz.matrix_basis = Matrix.Translation((x0 + i * step, y, 0.0))
            name = self.BUTTONS[self.buttons.index(gz)][1]
            _CUSTOM_DRAW.append((gz, x0 + i * step, y, size, name, name == "lock_open"))


_DRAW_HANDLE = None
_CUSTOM_DRAW = []          # (gizmo, x, y, radius, icon name, blue backdrop)
_TEXTURES = {}
# Measured on Blender 5.3's default theme: backdrop and icon colors of its own buttons.
_ICON_GREY = 185.0 / 255.0   # shows as 183 after texture filtering


_ICON_SIZES = (32, 64, 128)   # PNGs rendered from the SVG masters in icons/src


def _texture(name, pixels):
    """Icon texture for an on-screen size: the smallest rendering that is at least as large,
    so icons are only ever scaled down and stay sharp at any interface scale."""
    size = next((s for s in _ICON_SIZES if s >= pixels), _ICON_SIZES[-1])
    tex = _TEXTURES.get((name, size))
    if tex is None:
        path = os.path.join(os.path.dirname(__file__), "icons", f"{name}_{size}.png")
        img = bpy.data.images.load(path, check_existing=False)
        try:
            w, h = img.size
            buf = gpu.types.Buffer('FLOAT', w * h * 4, list(img.pixels))
            tex = gpu.types.GPUTexture((w, h), format='RGBA16F', data=buf)
        finally:
            bpy.data.images.remove(img)
        _TEXTURES[(name, size)] = tex
    return tex


def _draw_disc(x, y, r, color):
    verts = [(x, y)] + [(x + r * math.cos(math.radians(a)), y + r * math.sin(math.radians(a)))
                        for a in range(0, 361, 10)]
    indices = [(0, i, i + 1) for i in range(1, len(verts) - 1)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=indices)
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _transform_running(ctx):
    """True while a move/rotate/scale is being dragged (Blender hides its gizmos then and
    the new pivot position is only known when the transform ends), and also right after
    it ends, until the add-on has read the result, so the buttons never flash at the old
    place."""
    try:
        if any(op.bl_idname.startswith("TRANSFORM_OT_")
               for w in ctx.window_manager.windows for op in w.modal_operators):
            return True
        ops = ctx.window_manager.operators
        return bool(len(ops)) and _op_sig(ops[-1]) != _LAST_OP_SIG
    except Exception:
        return False


def _draw_custom_buttons():
    """Paint the buttons with the same look as Blender's own icon buttons."""
    ctx = bpy.context
    if not _CUSTOM_DRAW or not _is_transform_tool(ctx) or _transform_running(ctx):
        return
    gpu.state.blend_set('ALPHA')
    for gz, x, y, r, name, blue in list(_CUSTOM_DRAW):
        try:
            hover = gz.is_highlight
        except ReferenceError:
            continue
        if blue:
            back = (0.25, 0.45, 0.8, 0.95)
        else:
            back = (0.3, 0.3, 0.3, 0.95) if hover else (0.12, 0.12, 0.12, 0.75)
        _draw_disc(x, y, r, back)
        half = r * 0.62
        shader = gpu.shader.from_builtin('IMAGE_COLOR')
        batch = batch_for_shader(shader, 'TRI_FAN', {
            "pos": ((x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)),
            "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        shader.bind()
        shader.uniform_sampler("image", _texture(name, 2.0 * half))
        grey = 1.0 if (hover or blue) else _ICON_GREY
        shader.uniform_float("color", (grey, grey, grey, 1.0))
        batch.draw(shader)
    gpu.state.blend_set('NONE')


_HINT_TEXT = "Pivot Only  \u00b7  Space to exit"     # the lock tooltip explains the mode


def _draw_rounded_rect(x, y, w, h, r, color):
    r = min(r, w / 2.0, h / 2.0)
    pts = []
    for cx, cy, a0 in ((x + w - r, y + h - r, 0.0), (x + r, y + h - r, 90.0),
                       (x + r, y + r, 180.0), (x + w - r, y + r, 270.0)):
        for k in range(7):
            a = math.radians(a0 + k * 15.0)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    center = (x + w / 2.0, y + h / 2.0)
    verts = [center] + pts + [pts[0]]
    indices = [(0, i, i + 1) for i in range(1, len(verts) - 1)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=indices)
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_overlay():
    _draw_custom_buttons()
    _draw_hint()


def _draw_hint():
    """Hint at the bottom of the 3D Viewport while Pivot Only is on (keeps the status bar free)."""
    ctx = bpy.context
    # Only with a transform tool: with a brush nothing moves and Space is not the add-on's.
    if not (_is_sculpt(ctx) and _pivot_only(ctx) and _is_transform_tool(ctx)):
        return
    p = _prefs()
    if p is not None and not p.show_pivot_only_hint:
        return
    region = ctx.region
    ui = ctx.preferences.system.ui_scale
    bottom = 0
    for r in ctx.area.regions:                 # stay above the asset shelf
        if r.type in {'ASSET_SHELF', 'ASSET_SHELF_HEADER'} and r.height > 1 and r.width > 1:
            bottom = max(bottom, r.y + r.height - region.y)
    font = 0
    blf.size(font, 11.0 * ui)
    width = blf.dimensions(font, _HINT_TEXT)[0]
    height = blf.dimensions(font, "Pivot Only")[1]    # cap height without descenders
    pad_x, pad_y = 8.0 * ui, 4.0 * ui
    x = (region.width - width) / 2.0
    y = bottom + 12.0 * ui
    _draw_rounded_rect(x - pad_x, y - pad_y, width + 2 * pad_x, height + 2 * pad_y,
                       5.0 * ui, (0.25, 0.45, 0.8, 0.9))
    blf.position(font, x, y, 0)
    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.draw(font, _HINT_TEXT)


# ----------------------------------------------------------------------------
# Keymap
# ----------------------------------------------------------------------------
_addon_keymaps = []


def _register_keymaps():
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return
    # Sculpt Mode: add-on items are evaluated before Blender's own Sculpt items.
    km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
    km.keymap_items.new(STP_OT_pivot_to_surface.bl_idname, 'RIGHTMOUSE', 'PRESS', shift=True)
    # Space only acts while a transform tool is active; otherwise it passes through
    # (animation playback or the user's Spacebar Action).
    km.keymap_items.new(STP_OT_toggle_pivot_only.bl_idname, 'SPACE', 'PRESS')
    kmi = km.keymap_items.new("wm.tool_set_by_id", 'T', 'PRESS', ctrl=True)
    kmi.properties.name = "builtin.transform"
    _addon_keymaps.append(km)
    # Transform tools: G/R/S transform instead of switching brushes. These keymaps belong to
    # the tools, so they are only active while a transform tool is the active tool.
    # Not with the Industry Compatible keymap, where R/S already have other meanings
    # (Scale tool, brush size) and tools are picked with W/E/R.
    active = bpy.context.window_manager.keyconfigs.active
    if active is not None and active.name.startswith("Industry"):
        return
    for name in ("3D View Tool: Transform", "3D View Tool: Move",
                 "3D View Tool: Rotate", "3D View Tool: Scale"):
        km = kc.keymaps.new(name=name, space_type='VIEW_3D')
        km.keymap_items.new("transform.translate", 'G', 'PRESS')
        km.keymap_items.new("transform.rotate", 'R', 'PRESS')
        km.keymap_items.new("transform.resize", 'S', 'PRESS')
        _addon_keymaps.append(km)


def _unregister_keymaps():
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is not None:
        for km in _addon_keymaps:
            kc.keymaps.remove(km)
    _addon_keymaps.clear()


# ----------------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------------
def _objects_with_memory():
    """Objects of this file with a remembered pivot (linked ones cannot be edited)."""
    return [ob for ob in bpy.data.objects
            if ob.library is None and getattr(ob, "stp_pivot_state", _MEM_NONE) != _MEM_NONE]


class STP_OT_clear_pivot_memory(Operator):
    """Forget the remembered pivot of every object in this file. Each object starts again like the first time (see Pivot on First Entry). Save the file to keep the change"""
    bl_idname = "stp.clear_pivot_memory"
    bl_label = "Clear Pivot Memory"
    bl_options = {'REGISTER', 'INTERNAL'}

    def invoke(self, context, event):
        count = len(_objects_with_memory())
        if not count:
            self.report({'INFO'}, "No object in this file has a remembered pivot")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event, title="Clear Pivot Memory",
            message=f"Forget the remembered pivot of {count} object{'s' if count != 1 else ''} in this file?",
            confirm_text="Clear")

    def execute(self, context):
        objects = _objects_with_memory()
        for ob in objects:
            ob.stp_pivot_state = _MEM_NONE
            ob.stp_pivot = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        # The object being sculpted starts over too, right now: otherwise its current pivot
        # would simply be remembered again. Same place as a first entry into Sculpt Mode.
        obj = context.active_object
        if _is_sculpt(context) and obj is not None and obj.type == 'MESH':
            entry = _object_rotation(context)
            target = (_bbox_center_world(obj) if _entry_center_enabled()
                      else obj.matrix_world.translation.copy())
            _move_native_pivot(context, obj, target)
            _rotate_native_pivot(context, entry @ _TRACKED.inverted())
            _apply_matrix(context, entry)
        self.report({'INFO'}, f"Pivot memory cleared on {len(objects)} object{'s' if len(objects) != 1 else ''}")
        return {'FINISHED'}


class STP_AddonPreferences(AddonPreferences):
    bl_idname = __package__

    show_pivot_only_hint: BoolProperty(
        name="Show Pivot Only Reminder",
        description="Show the blue \"Pivot Only\" reminder at the bottom of the 3D Viewport "
                    "while the gizmo is unlocked",
        default=True,
        update=lambda self, context: _tag_redraw(),
    )

    remember_pivot: BoolProperty(
        name="Remember Pivot per Object",
        description="Each object keeps the pivot where you left it (saved in the .blend file) "
                    "and gets it back when you sculpt it again",
        default=True,
    )
    entry_pivot: EnumProperty(
        name="Pivot on First Entry",
        description="Where the pivot starts on an object that has no remembered pivot",
        items=(
            ('MESH_CENTER', "Mesh Center", "Center of the object's bounding box"),
            ('ORIGIN', "Object Origin", "Blender's default: the object origin"),
        ),
        default='MESH_CENTER',
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = layout.column(heading="Pivot")
        col.prop(self, "entry_pivot")
        col.prop(self, "remember_pivot")
        split = col.split(factor=0.4)          # line the button up with the options
        split.label(text="")
        row = split.row()
        row.enabled = bool(_objects_with_memory())
        row.operator(STP_OT_clear_pivot_memory.bl_idname, icon='TRASH')

        col = layout.column(heading="Viewport")
        col.prop(self, "show_pivot_only_hint")


_CLASSES = (
    STP_OT_clear_pivot_memory,
    STP_AddonPreferences,
    STP_OT_pivot_to_surface,
    STP_OT_pivot_to_unmasked_center,
    STP_OT_pivot_to_world_origin,
    STP_OT_reset_orientation,
    STP_OT_mesh_to_world_origin,
    STP_OT_straighten_mesh,
    STP_OT_toggle_pivot_only,
    STP_GT_hover_tracker,
    STP_GGT_pivot_buttons,
)


def register():
    global _LAST_MODE_KEY, _DRAW_HANDLE
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    # Remembered pivot (see _store_pivot_memory): position + rotation in object space.
    bpy.types.Object.stp_pivot = FloatVectorProperty(
        size=7, default=(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0), options={'HIDDEN'})
    bpy.types.Object.stp_pivot_state = IntProperty(default=_MEM_NONE, options={'HIDDEN'})
    # The user's Pivot Point while Sculpt Mode uses Median Point ("" = nothing replaced).
    bpy.types.Scene.stp_user_pivot_point = StringProperty(default="", options={'HIDDEN'})
    _register_keymaps()
    _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), 'WINDOW', 'POST_PIXEL')
    # Startup runs with a restricted context; these become valid on the first timer tick.
    _mark_ops_seen()
    _LAST_MODE_KEY = _mode_key(bpy.context)   # enabling the add-on is not a mode change
    _subscribe_mode()
    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.handlers.undo_post.append(_on_undo_post)
    bpy.app.handlers.redo_post.append(_on_redo_post)
    bpy.app.timers.register(_watch_timer, first_interval=0.5, persistent=True)


def unregister():
    global _DRAW_HANDLE
    try:
        if _is_sculpt(bpy.context):
            _restore_user_orientation(bpy.context)
    except Exception:
        pass                                   # restricted context (Blender closing)
    try:
        for scene in bpy.data.scenes:
            if scene.stp_user_pivot_point:
                if scene.tool_settings.transform_pivot_point == 'MEDIAN_POINT':
                    scene.tool_settings.transform_pivot_point = scene.stp_user_pivot_point
                scene.stp_user_pivot_point = ""
    except Exception:
        pass
    if bpy.app.timers.is_registered(_watch_timer):
        bpy.app.timers.unregister(_watch_timer)
    for handlers, fn in ((bpy.app.handlers.load_post, _on_load_post),
                         (bpy.app.handlers.undo_post, _on_undo_post),
                         (bpy.app.handlers.redo_post, _on_redo_post)):
        if fn in handlers:
            handlers.remove(fn)
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    if _DRAW_HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, 'WINDOW')
        _DRAW_HANDLE = None
    _unregister_keymaps()
    del bpy.types.Scene.stp_user_pivot_point
    del bpy.types.Object.stp_pivot_state
    del bpy.types.Object.stp_pivot
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _TEXTURES.clear()
    _CUSTOM_DRAW.clear()


if __name__ == "__main__":
    register()
