"""Shot director: places characters, plays their animation, plans cameras and lights.

v1.2 fixes and changes
  * TWO OR MORE SHOTS NOW PLAY.  Before, every shot put its animation on a new NLA track and each
    strip held its first/last pose outside its own frames, so the newest track froze everything
    that came before it.  Now every character has ONE track, strips never overlap and never hold.
  * Shots follow each other on one timeline: shot 1 = frames 1-100, shot 2 = 101-200 ...
  * Any number of characters per shot (A, B and extras), with left/right AND depth position.
  * Characters stand on their own root empty, so position / turning never fights the rig.
  * Walk / run animations that stay in place now travel through the scene and turn to face the way
    they go; standing characters turn partly towards each other.
  * The camera is planned from the action (see camera_logic.py) and animated during the shot.
"""
import bpy
import math
import json
import re
import traceback
import urllib.request
from types import SimpleNamespace
from mathutils import Matrix, Quaternion, Vector
from bpy.types import Operator

from .helpers import (GEN_COLL, _S, _safe_name, _log, _get_collection, _cam_name,
                      _char_objects, _char_top, _key_visibility, _ensure_unique_shot_names,
                      _world_bbox, _fcurves, _action_bones, _canon_bone, _shot_cast,
                      _is_root)
from . import camera_logic as CL

LIGHT_PRESETS = {
    "DAY":    {"key": (3.5, (1.0, 0.96, 0.88)), "fill": (1.0, (0.75, 0.85, 1.0)), "rim": (2.0, (1.0, 1.0, 1.0))},
    "NIGHT":  {"key": (0.8, (0.45, 0.55, 1.0)), "fill": (0.3, (0.30, 0.40, 0.9)), "rim": (1.5, (0.5, 0.6, 1.0))},
    "WARM":   {"key": (2.5, (1.0, 0.72, 0.45)), "fill": (0.8, (1.0, 0.80, 0.6)), "rim": (1.2, (1.0, 0.85, 0.6))},
    "SUNSET": {"key": (3.0, (1.0, 0.55, 0.30)), "fill": (0.7, (0.60, 0.50, 0.8)), "rim": (2.5, (1.0, 0.60, 0.4))},
}
LIGHT_ROT = {"key": (50, 0, -35), "fill": (65, 0, 45), "rim": (-45, 0, 20)}

WALK_SPEED = 1.35   # metres per second for a 1.75 m character
RUN_SPEED = 3.4
MAX_AUTO_TRAVEL = 8.0
# category -> (bob height as a fraction of the character height, bob period in frames), used for
# characters that have no skeleton so they still feel alive
BOB = {"idle": (0.010, 48), "talk": (0.020, 18), "walk": (0.035, 14), "run": (0.070, 9),
       "jump": (0.300, 28), "dance": (0.050, 12), "fight": (0.040, 10), "laugh": (0.040, 10),
       "wave": (0.015, 24), "sad": (0.008, 60), "angry": (0.030, 12)}


# ------------------------------------------------------------------ clean-up
def _purge_actions():
    for a in list(bpy.data.actions):
        if a.name.startswith("CS_") and a.users == 0 and not a.use_fake_user:
            try:
                bpy.data.actions.remove(a)
            except Exception:
                pass


def _clear_generated(scene):
    coll = bpy.data.collections.get(GEN_COLL)
    if coll:
        for o in list(coll.objects):
            data = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if data is not None and data.users == 0:
                if isinstance(data, bpy.types.Camera):
                    bpy.data.cameras.remove(data)
                elif isinstance(data, bpy.types.Light):
                    bpy.data.lights.remove(data)
    for m in list(scene.timeline_markers):
        if m.name.startswith("CS_"):
            scene.timeline_markers.remove(m)


def _make_light(coll, name, ltype, energy, color, rot_deg):
    ld = bpy.data.lights.new(name, ltype)
    ld.energy = energy
    ld.color = color
    o = bpy.data.objects.new(name, ld)
    coll.objects.link(o)
    o.rotation_euler = [math.radians(a) for a in rot_deg]
    return o


# ------------------------------------------------------------------ characters
def _vec_prop(obj, key, default):
    try:
        v = json.loads(obj.get(key, "") or "")
        if len(v) == 3:
            return tuple(float(x) for x in v)
    except Exception:
        pass
    return tuple(float(x) for x in default)


def _set_vec_prop(obj, key, vec):
    obj[key] = json.dumps([float(x) for x in vec])


def _reset_root(context, root):
    """Old builds left the root at the world origin with generated keys on it. Wipe that so the
    character is back exactly where the artist placed it."""
    if root.animation_data:
        root.animation_data_clear()
    root.rotation_mode = "XYZ"
    root.location = (0.0, 0.0, 0.0)
    root.rotation_euler = (0.0, 0.0, 0.0)
    root.scale = (1.0, 1.0, 1.0)
    root.delta_location = (0.0, 0.0, 0.0)
    context.view_layer.update()


def _tag_root(root, cname, origin):
    root["cs_root"] = True
    root["cs_char"] = cname
    root["cs_joined"] = True
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.35
    root.show_in_front = True
    _set_vec_prop(root, "cs_anchor_loc", origin)
    _set_vec_prop(root, "cs_base_loc", origin)
    _set_vec_prop(root, "cs_base_rot", (0.0, 0.0, 0.0))


def _ensure_root(context, scene, cname, arm):
    """One empty per character, sitting exactly ON the character (at its origin).

    It carries the build's position and turning. The character keeps its own location / rotation /
    scale, so what the artist set is what you get. Returns (root, fresh); fresh=True means the root
    was just created or re-anchored, so there are no later hand edits to read from it."""
    top = _char_top(arm)
    root = top.parent if _is_root(top.parent) else None
    if root is not None:
        shared = any(ch is not top for ch in root.children)
        if not root.get("cs_joined"):
            # root from an older version: it sits at the world origin, away from the character
            _reset_root(context, root)
            if not shared:
                origin = top.matrix_basis.translation.copy()
                root.location = origin
                top.matrix_parent_inverse = Matrix.Translation(-origin)
                _tag_root(root, cname, origin)
                return root, True
        elif root.get("cs_char") == cname:
            return root, False
        # a root shared with another character (e.g. a duplicated rig): give this one its own
        mw = top.matrix_world.copy()
        top.parent = None
        top.matrix_world = mw
        if not root.children:
            try:
                bpy.data.objects.remove(root, do_unlink=True)
            except Exception:
                pass
    else:
        mw = top.matrix_world.copy()
    origin = mw.translation.copy()
    root = bpy.data.objects.new(f"CS_{_safe_name(cname)}_Root", None)
    coll = top.users_collection[0] if top.users_collection else scene.collection
    coll.objects.link(root)
    root.rotation_mode = "XYZ"
    root.location = origin
    top.parent = root
    top.matrix_parent_inverse = Matrix.Translation(-origin)
    _tag_root(root, cname, origin)
    return root, True


def _animated_value(root, path, index, frame):
    """What the last build keyed on the root at this frame, or None if it keyed nothing there."""
    ad = root.animation_data
    act = ad.action if ad else None
    if act is None:
        return None
    for fc in _fcurves(act):
        if fc.data_path == path and fc.array_index == index:
            try:
                return float(fc.evaluate(frame))
            except Exception:
                return None
    return None


def _read_placement(root, frame, fresh):
    """The artist's base location / rotation for this character.
    Hand edits made after the last build are found by comparing the root with what the build keyed
    AT THE SAME FRAME (so it does not matter which frame you press Build on)."""
    base_loc = list(_vec_prop(root, "cs_base_loc", root.location))
    base_rot = list(_vec_prop(root, "cs_base_rot", (0.0, 0.0, 0.0)))
    anchor = _vec_prop(root, "cs_anchor_loc", base_loc)
    if not fresh:
        live_loc, live_rot = tuple(root.location), tuple(root.rotation_euler)
        for i in range(3):
            g = _animated_value(root, "location", i, frame)
            d = live_loc[i] - (g if g is not None else base_loc[i])
            if abs(d) > 1e-5:
                base_loc[i] += d
            g = _animated_value(root, "rotation_euler", i, frame)
            d = live_rot[i] - (g if g is not None else base_rot[i])
            if abs(d) > 1e-5:
                base_rot[i] += d
    return {"base_loc": tuple(base_loc), "base_rot": tuple(base_rot), "anchor": tuple(anchor)}


def _reset_pose(arm):
    if arm.type != "ARMATURE":
        return
    for pb in arm.pose.bones:
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
        pb.scale = (1, 1, 1)


def _char_height(arm):
    meshes = [o for o in _char_objects(arm) if o.type == "MESH"]
    if not meshes:
        return 1.75
    mn, mx = _world_bbox(meshes)
    # Some imported/auto-rigged characters have stale object-space bound_box
    # values even though their visible mesh dimensions are larger. Use both so
    # the camera planner does not place a full-body camera inside the character.
    h = max(mx[2] - mn[2], max((float(o.dimensions.z) for o in meshes), default=0.0))
    return h if h > 0.2 else 1.75


def _evaluated_char_bounds(arm):
    """Measure the posed mesh after armature modifiers and current animation evaluate."""
    meshes = [o for o in _char_objects(arm) if o.type == "MESH"]
    if not meshes:
        return None
    depsgraph = bpy.context.evaluated_depsgraph_get()
    low, high = [float("inf")] * 3, [float("-inf")] * 3
    for source in meshes:
        obj = source.evaluated_get(depsgraph)
        for corner in obj.bound_box:
            point = obj.matrix_world @ Vector(corner)
            for axis in range(3):
                low[axis] = min(low[axis], point[axis])
                high[axis] = max(high[axis], point[axis])
    if low[0] == float("inf"):
        return None
    return low, high


def _apply_pose_correction(act, corr):
    """A-pose skeletons rest with the arms angled down, but Mixamo animations assume arms straight out.
    Multiplying the upper-arm rotation keys by one fixed rotation makes the arms end up in the same
    place in the world as they would on a T-pose skeleton."""
    groups = {}
    for fc in _fcurves(act):
        m = re.match(r'pose\.bones\["(.+?)"\]\.rotation_quaternion$', fc.data_path)
        if m and m.group(1) in corr:
            groups.setdefault(m.group(1), {})[fc.array_index] = fc
    for bone, fcs in groups.items():
        if len(fcs) != 4:
            continue
        n = len(fcs[0].keyframe_points)
        if any(len(fcs[i].keyframe_points) != n for i in range(4)):
            continue
        d = Quaternion(corr[bone])
        prev = None
        for k in range(n):
            q = Quaternion([fcs[i].keyframe_points[k].co[1] for i in range(4)])
            r = d @ q
            if prev is not None and (r.w * prev.w + r.x * prev.x + r.y * prev.y + r.z * prev.z) < 0:
                r = Quaternion((-r.w, -r.x, -r.y, -r.z))      # keep the shortest way round between keys
            prev = r
            for i in range(4):
                kp = fcs[i].keyframe_points[k]
                dy = r[i] - kp.co[1]
                kp.co[1] = r[i]
                kp.handle_left[1] += dy
                kp.handle_right[1] += dy
        for fc in fcs.values():
            fc.update()


def _retarget(act, arm):
    """Make an action fit this rig. Two things can be needed:
      1. the action's bones are named differently (mixamorig: / mixamorig1: / no prefix, Arm.L / LeftArm ...)
      2. the rig was built with an A-pose skeleton (arm keys need the fixed correction)
    A changed copy is returned in those cases, otherwise the action itself. Returns (action, fit 0..1)."""
    if arm.type != "ARMATURE":
        return act, 1.0
    rig_bones = {b.name for b in arm.pose.bones}
    used = _action_bones(act)
    if not used:
        return act, 1.0
    canon = {}
    for b in rig_bones:
        canon.setdefault(_canon_bone(b), b)
    mapping = {u: canon[_canon_bone(u)] for u in used if u not in rig_bones and _canon_bone(u) in canon}
    fit = (len(used & rig_bones) + len(mapping)) / float(len(used))
    corr = {}
    raw = arm.get("cs_apose")
    if raw:
        try:
            corr = json.loads(raw)
        except Exception:
            corr = {}
    if not mapping and not corr:
        return act, fit
    key = f"CS_map_{_safe_name(act.name)[:16]}_{_safe_name(arm.name)[:16]}{'_A' if corr else ''}"
    new = bpy.data.actions.get(key)
    if new is None:
        new = act.copy()
        new.name = key
        for fc in _fcurves(new):
            m = re.match(r'pose\.bones\["(.+?)"\](.*)', fc.data_path)
            if m and m.group(1) in mapping:
                fc.data_path = f'pose.bones["{mapping[m.group(1)]}"]{m.group(2)}'
        if corr:
            _apply_pose_correction(new, corr)
    return new, fit


def _analyze_action(act, arm, height, cache):
    """Measure an action: does it travel (root motion), how high does it lift, how energetic is it."""
    key = (act.name, arm.name)
    if key in cache:
        return cache[key]
    st = {"travel_m": 0.0, "dir": (0.0, 0.0), "lift_m": 0.0, "energy": 0.0}
    try:
        rng, loc = [], {}
        for fc in _fcurves(act):
            dp = fc.data_path
            if dp.endswith("rotation_quaternion") or dp.endswith("rotation_euler"):
                if len(fc.keyframe_points) > 1:
                    vals = [kp.co[1] for kp in fc.keyframe_points]
                    rng.append(max(vals) - min(vals))
            else:
                m = re.match(r'pose\.bones\["(.+?)"\]\.location$', dp)
                if m:
                    loc.setdefault(m.group(1), {})[fc.array_index] = fc
        if rng:
            st["energy"] = sum(rng) / len(rng)
        if arm.type == "ARMATURE" and loc:
            canon = {_canon_bone(b.name): b for b in arm.data.bones}
            cands = []
            for n in loc:
                b = arm.data.bones.get(n) or canon.get(_canon_bone(n))
                if b is not None:
                    cands.append((n, b))
            if cands:
                cands.sort(key=lambda nb: (nb[1].parent is not None,
                                           not re.search(r"hips|pelvis|root", _canon_bone(nb[0]))))
                n, bone = cands[0]
                mat = arm.matrix_world.to_3x3() @ bone.matrix_local.to_3x3()
                f0, f1 = act.frame_range
                curves = loc[n]

                def val(f):
                    return Vector([curves[i].evaluate(f) if i in curves else 0.0 for i in range(3)])

                v0 = val(f0)
                best, bdir, zs = 0.0, (0.0, 0.0), []
                for i in range(13):
                    w = mat @ (val(f0 + (f1 - f0) * i / 12.0) - v0)
                    h = math.hypot(w.x, w.y)
                    zs.append(w.z)
                    if h > best:
                        best, bdir = h, (w.x / h, w.y / h)
                st["travel_m"], st["dir"], st["lift_m"] = best, bdir, max(zs) - min(zs)
    except Exception:
        pass
    cache[key] = st
    return st


def _add_strip(track, name, start, act, duration):
    """One animation strip that covers exactly its own shot and holds nothing outside it."""
    strip = track.strips.new(name, int(start), act)
    strip.extrapolation = "NOTHING"
    strip.blend_type = "REPLACE"
    alen = max(1.0, act.frame_range[1] - act.frame_range[0])
    strip.scale = 1.0
    strip.repeat = max(0.05, (duration - 1) / alen)
    try:
        if getattr(strip, "action_slot", None) is None and len(act.slots):
            strip.action_slot = act.slots[0]
    except Exception:
        pass
    return strip


def _bob(root, start, end, cat, height):
    """Rig-less characters: a gentle up-down motion that matches the action."""
    amp, per = BOB.get(cat, (0.015, 36))
    half = max(2.0, per / 2.0)
    t, i = float(start), 0
    while t < end:
        root.delta_location[2] = amp * height if i % 2 else 0.0
        root.keyframe_insert("delta_location", index=2, frame=t)
        t += half
        i += 1
    root.delta_location[2] = 0.0
    root.keyframe_insert("delta_location", index=2, frame=end)


# ------------------------------------------------------------------ movement
def _wrap(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def _travel_end(sh, e, others, secs):
    """Where an in-place walk/run goes during the shot. Returns (x1, y1) or None."""
    heightk = e["H"] / 1.75
    speed = (RUN_SPEED if e["cat"] == "run" else WALK_SPEED) * heightk
    dist = sh.move_dist if sh.move_dist > 0 else min(speed * secs, MAX_AUTO_TRAVEL)
    direction = None
    if others and sh.move_dist <= 0:
        o = min(others, key=lambda q: math.hypot(q["x"] - e["x"], q["y"] - e["y"]))
        vx, vy = o["x"] - e["x"], o["y"] - e["y"]
        gap = math.hypot(vx, vy)
        if gap > 2.4:
            direction = (vx / gap, vy / gap)
            dist = min(dist, gap - 1.5)      # stop in front of them
    if direction is None:
        fd = e.get("face_dir")
        direction = fd if fd else ((1.0, 0.0) if e["x"] <= 0 else (-1.0, 0.0))
    if dist < 0.5:
        return None
    return e["x"] + direction[0] * dist, e["y"] + direction[1] * dist


def _facing_yaw(e, others):
    """Turn a standing character partly towards the nearest other one (0 = facing the audience)."""
    if not others:
        return 0.0
    o = min(others, key=lambda q: math.hypot(q["x"] - e["x"], q["y"] - e["y"]))
    vx, vy = o["x"] - e["x"], o["y"] - e["y"]
    if math.hypot(vx, vy) < 0.2:
        return 0.0
    delta = _wrap(math.atan2(vy, vx) + math.pi / 2)
    return max(-math.radians(60), min(math.radians(60), delta * 0.55))


# ------------------------------------------------------------------ cameras
def _setup_camera(coll, sh, plan, start, end, suffix=""):
    name = _cam_name(sh) + suffix
    cd = bpy.data.cameras.new(name)
    cd.lens = plan.lens
    cd.clip_start = 0.05
    cd.clip_end = 300.0
    cam = bpy.data.objects.new(name, cd)
    coll.objects.link(cam)
    tgt = bpy.data.objects.new(f"CS_{_safe_name(sh.name)}_Target", None)
    tgt.empty_display_size = 0.15
    coll.objects.link(tgt)
    try:
        tgt.hide_set(True)      # only an aim point for the camera; keeps the viewport clean
    except Exception:
        pass
    for t, cpos, tpos in plan.waypoints:
        f = start + t * (end - start)
        cam.location = cpos
        cam.keyframe_insert("location", frame=f)
        tgt.location = tpos
        tgt.keyframe_insert("location", frame=f)
    for o in (cam, tgt):
        if o.animation_data and o.animation_data.action:
            for fc in _fcurves(o.animation_data.action):
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR" if plan.linear else "BEZIER"
    con = cam.constraints.new("TRACK_TO")
    con.target = tgt
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return cam


def _ollama_camera_sequence(settings, sh, entries):
    """Ask an optional local Ollama model for a bounded list of camera setups."""
    allowed = {"WIDE", "FULL", "MEDIUM", "CLOSE", "OTS", "TWO_SHOT", "LOW", "HIGH", "TRACK", "ORBIT"}
    actions = sorted({q["action"] for e in entries for q in e["segments"] if q.get("action")})
    prompt = ("You are a film camera planner for a simple 3D cartoon. Return JSON only as "
              '{"camera_sequence":["...", ...]}. Pick exactly ' + str(sh.camera_angles) +
              " values from this list: " + ", ".join(sorted(allowed)) +
              ". Use a varied, readable sequence with wide/full shots for movement, medium/close for "
              "dialogue, and OTS for two-person conversation. Avoid close shots when the action needs "
              "full-body visibility. Character count: " + str(len(entries)) +
              ". Actions: " + (", ".join(actions) or "none") +
              ". Visual beat: " + (sh.shot_summary or "unspecified") +
              ". Emotion: " + (sh.emotion_note or "unspecified") +
              ". Location: " + (sh.location_note or "unspecified") +
              ". Required props: " + (sh.props_note or "none") +
              ". Dialogue: " + (sh.dialogue or "none") + ".")
    url = settings.ollama_url.rstrip("/") + "/api/generate"
    request = urllib.request.Request(url, data=json.dumps({
        "model": settings.ollama_model or "qwen3-vl:4b", "prompt": prompt,
        "stream": False, "format": "json", "think": False,
        "options": {"num_predict": 128, "temperature": 0.2},
    }).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=max(30, int(settings.ollama_timeout))) as response:
        payload = json.loads(response.read().decode("utf-8"))
    answer = json.loads(payload.get("response", "{}"))
    values = answer.get("camera_sequence", [])
    sequence = [str(value).strip().upper() for value in values if str(value).strip().upper() in allowed]
    return sequence[:sh.camera_angles] if sequence else None


def _vfx_anchor(rig, entry, action_name):
    arm = rig["arm"]
    anchor = entry.anchor
    text = (action_name or "").lower()
    if anchor == "AUTO":
        anchor = "HAND" if any(word in text for word in ("cast", "magic", "punch", "attack", "sword", "throw")) else "ROOT"
    if anchor in {"HAND", "HEAD"} and arm.type == "ARMATURE":
        words = ("hand", "wrist") if anchor == "HAND" else ("head", "neck")
        bone = next((b for b in arm.pose.bones if any(word in b.name.lower() for word in words)), None)
        if bone:
            return arm, bone.name, arm.matrix_world @ bone.matrix
    return rig["root"], "", rig["root"].matrix_world.copy()


def _spawn_vfx(scene, gen, entry, rig, action_name, start, end, serial):
    source = bpy.data.objects.get(entry.root_object)
    if source is None:
        raise RuntimeError(f"VFX source is missing: {entry.root_object}")
    sources, stack = [], [source]
    while stack:
        obj = stack.pop()
        if obj in sources:
            continue
        sources.append(obj)
        stack.extend(reversed(list(obj.children)))
    copies = {}
    for src in sources:
        obj = src.copy()
        if src.data is not None:
            obj.data = src.data
        obj.name = f"CS_VFX_{serial:03d}_{src.name}"
        gen.objects.link(obj)
        copies[src] = obj
    parent, bone_name, anchor_world = _vfx_anchor(rig, entry, action_name)
    local = (Matrix.Translation(Vector((entry.offset_x, entry.offset_y, entry.offset_z))) @
             Matrix.Rotation(entry.rotation_z, 4, "Z") @ Matrix.Scale(entry.scale, 4))
    source_basis = source.matrix_world.copy()
    source_basis.translation = (0.0, 0.0, 0.0)
    target_world = anchor_world @ local @ source_basis
    source_inverse = source.matrix_world.inverted_safe()
    for src in sources:
        obj = copies[src]
        if src in copies and src is not source and src.parent in copies:
            obj.parent = copies[src.parent]
        elif src is source:
            obj.parent = parent
            if bone_name:
                obj.parent_type = "BONE"
                obj.parent_bone = bone_name
        obj.matrix_world = target_world @ source_inverse @ src.matrix_world
        obj.hide_render = False
        obj.hide_set(False)
        obj["cs_vfx_name"] = entry.name
        obj["cs_vfx_action"] = action_name
    _key_visibility(list(copies.values()), {0: False, start: True, end + 1: False})
    return list(copies.values())


# ------------------------------------------------------------------ the episode
def build_episode(context):
    scene = context.scene
    s = _S(context)
    warns = []
    for old, new in _ensure_unique_shot_names(s):
        warns.append(f"Shot name '{old}' was used twice - renamed to '{new}'")
    # Every character gets ONE root empty that sits on the character. Its transform is the artist's
    # base placement; the character keeps the location / rotation / scale it was given.
    # Hand edits made on the root since the last build are read first (see _read_placement).
    keep = bool(s.keep_placement)
    edit_frame = max(1, int(scene.frame_current))
    roots, placements = {}, {}
    for c in s.characters:
        arm = bpy.data.objects.get(c.armature_name)
        if arm is None or arm.name not in scene.objects:
            continue
        root, fresh = _ensure_root(context, scene, c.name, arm)
        roots[c.name] = root
        placements[c.name] = _read_placement(root, edit_frame, fresh)

    gen = _get_collection(scene, GEN_COLL)
    _clear_generated(scene)

    # --- prepare every character: clean animation, rest pose, measured height
    rigs = {}
    for c in s.characters:
        arm = bpy.data.objects.get(c.armature_name)
        if arm is None or arm.name not in scene.objects:
            warns.append(f"Character '{c.name}': rig object '{c.armature_name}' not in scene")
            continue
        root = roots[c.name]
        place = placements[c.name]
        # Root animation is generated data; clear it before writing the new build.
        if root.animation_data:
            root.animation_data_clear()
        for o in _char_objects(arm):
            if o.animation_data:
                o.animation_data_clear()
        root.rotation_mode = "XYZ"
        root.location = place["base_loc"]
        root.rotation_euler = place["base_rot"]
        root.delta_location = (0.0, 0.0, 0.0)
        # root.scale is never touched: whatever size the artist gave the root or the character stays
        _set_vec_prop(root, "cs_base_loc", place["base_loc"])
        _set_vec_prop(root, "cs_base_rot", place["base_rot"])
        _reset_pose(arm)
        context.view_layer.update()
        top = _char_top(arm)
        try:
            heading = _wrap(top.matrix_world.to_3x3().normalized().to_euler("XYZ").z)
        except Exception:
            heading = 0.0
        rigs[c.name] = {"arm": arm, "root": root, "H": _char_height(arm), "track": None,
                        "vis": {}, "linear": set(), "base_loc": place["base_loc"],
                        "base_rot": place["base_rot"], "anchor": place["anchor"],
                        "heading": heading}

    cache = {}
    carry = {}                      # character name -> where / how turned it was at the end of its last shot
    cont = bool(s.continue_shots)
    frame = 1
    prev_kind, rep = "", 0
    total = len(s.shots)
    aspect = s.res_x / float(max(1, s.res_y))
    first_cam = None
    vfx_serial = 0
    ollama_failed = False

    for idx, sh in enumerate(s.shots):
        start = frame
        end = start + max(1, sh.duration) - 1
        sh.start_frame, sh.end_frame = start, end
        frame = end + 1
        secs = max(0.1, sh.duration / float(max(1, s.fps)))

        # ---- who is in this shot
        words = len(sh.dialogue.split())
        entries, seen = [], set()
        for c in _shot_cast(sh):
            rig = rigs.get(c["name"])
            if rig is None:
                warns.append(f"{sh.name}: character '{c['name']}' unavailable")
                continue
            if c["name"] in seen:
                warns.append(f"{sh.name}: '{c['name']}' is in the shot twice - second one ignored")
                continue
            seen.add(c["name"])
            segments = []
            raw_cues = list(c.get("cues", []))
            if raw_cues:
                cursor = 1
                for cue in sorted(raw_cues, key=lambda q: (q.start_frame, q.end_frame)):
                    a = max(1, int(cue.start_frame))
                    b = min(sh.duration, int(cue.end_frame))
                    if a <= cursor - 1:
                        warns.append(f"{sh.name}: overlapping action range for {c['name']} was trimmed")
                        a = cursor
                    if b < a:
                        warns.append(f"{sh.name}: action range for {c['name']} is outside the shot")
                        continue
                    lib = s.animations.get(cue.action) if cue.action else None
                    act = bpy.data.actions.get(lib.action_name if lib else cue.action) if cue.action else None
                    if cue.action and act is None:
                        warns.append(f"{sh.name}: action '{cue.action}' not found")
                    stats = _analyze_action(act, rig["arm"], rig["H"], cache) if act else None
                    cat = CL.classify(cue.action or "", {
                        "travel": stats["travel_m"] / rig["H"] if stats else 0.0,
                        "lift": stats["lift_m"] / rig["H"] if stats else 0.0,
                        "energy": stats["energy"] if stats else 0.0},
                        max(0.1, (b - a + 1) / float(max(1, s.fps)))) if act else ("talk" if words else "idle")
                    position = ((float(cue.position_x), float(cue.position_y), float(cue.position_z))
                                if cue.use_position else None)
                    segments.append({"start": a, "end": b, "action": cue.action, "act": act,
                                     "stats": stats, "cat": cat, "position": position})
                    cursor = b + 1
            else:
                act = None
                if c["action"]:
                    lib = s.animations.get(c["action"])
                    act = bpy.data.actions.get(lib.action_name if lib else c["action"])
                    if act is None:
                        warns.append(f"{sh.name}: action '{c['action']}' not found")
                stats = _analyze_action(act, rig["arm"], rig["H"], cache) if act else None
                cat = CL.classify(c["action"], {
                    "travel": stats["travel_m"] / rig["H"] if stats else 0.0,
                    "lift": stats["lift_m"] / rig["H"] if stats else 0.0,
                    "energy": stats["energy"] if stats else 0.0}, secs) if act else ("talk" if words else "idle")
                segments = [{"start": 1, "end": sh.duration, "action": c["action"], "act": act,
                             "stats": stats, "cat": cat, "position": None}]
            e = dict(c)
            first = next((q for q in segments if q["act"] is not None), segments[0] if segments else {})
            e.update(rig=rig, segments=segments, act=first.get("act"), stats=first.get("stats"),
                     action=first.get("action", c["action"]), H=rig["H"], cat=first.get("cat", "idle"),
                     x1=None, y1=None, yaw=0.0, turn=False, rootmotion=False,
                     cue_timeline=bool(raw_cues))
            base, anchor, heading = rig["base_loc"], rig["anchor"], rig["heading"]
            if keep:
                # start exactly where the artist put the character; walk the way it is facing
                e["x"], e["y"], e["z"] = base
                e["face_dir"] = (math.sin(heading), -math.cos(heading))
            else:
                # X / Depth of the shot, plus any hand move made on the root since the first build
                e["x"] = float(c["x"]) + base[0] - anchor[0]
                e["y"] = float(c["y"]) + base[1] - anchor[1]
                e["z"] = base[2]
                e["face_dir"] = None
            e["start_yaw"] = 0.0
            last = carry.get(c["name"]) if cont else None
            if last is not None:
                # continue from where this character ended in its previous shot
                e["x"], e["y"], e["z"] = last["pos"]
                e["start_yaw"] = last["yaw"]
                if keep:
                    face = heading + last["yaw"]
                    e["face_dir"] = (math.sin(face), -math.cos(face))
            entries.append(e)

        # ---- movement and turning
        for e in entries:
            others = [o for o in entries if o is not e]
            st = e["stats"]
            if st and st["travel_m"] > 0.25 * e["H"]:
                # the animation itself moves the body (root motion)
                e["rootmotion"] = True
                e["x1"] = e["x"] + st["dir"][0] * st["travel_m"]
                e["y1"] = e["y"] + st["dir"][1] * st["travel_m"]
                warns.append(f"{sh.name}: '{e['action']}' has root motion - it jumps back each time it loops. "
                             "For best results download it from Mixamo with 'In Place' ticked")
            elif sh.auto_move and e["cat"] in CL.LOCOMOTION and not e["cue_timeline"]:
                end_pos = _travel_end(sh, e, others, secs)
                if end_pos:
                    e["x1"], e["y1"] = end_pos
                    e["yaw"] = _wrap(math.atan2(e["y1"] - e["y"], e["x1"] - e["x"]) + math.pi / 2)
                    e["turn"] = True
            if (e["x1"] is None and sh.face_each_other and len(entries) > 1
                    and e["cat"] not in CL.LOCOMOTION and not keep):
                e["yaw"] = _facing_yaw(e, others)
                e["turn"] = True

            # Plan each action's position in order. A walk/run advances the rig
            # during its own range; the next action starts at that destination.
            if e["cue_timeline"]:
                current = (float(e["x"]), float(e["y"]), float(e["z"]))
                for seg in e["segments"]:
                    seg["from_position"] = current
                    target = seg.get("position")
                    stats = seg.get("stats") or {}
                    travel = float(stats.get("travel_m", 0.0))
                    if target is None and travel > 0.25 * e["H"]:
                        direction = stats.get("dir", (0.0, 0.0))
                        target = (current[0] + direction[0] * travel,
                                  current[1] + direction[1] * travel, current[2])
                    elif target is None and sh.auto_move and seg["cat"] in CL.LOCOMOTION:
                        mover = dict(e, x=current[0], y=current[1], cat=seg["cat"])
                        duration = (seg["end"] - seg["start"] + 1) / float(max(1, s.fps))
                        endpoint = _travel_end(sh, mover, others, duration)
                        if endpoint:
                            target = (endpoint[0], endpoint[1], current[2])
                    if target is None:
                        target = current
                    seg["to_position"] = tuple(target)
                    current = tuple(target)
                if math.hypot(current[0] - e["x"], current[1] - e["y"]) > 0.05:
                    e["x1"], e["y1"] = current[0], current[1]

        # ---- write placement, turning, animation
        present = set()
        for e in entries:
            rig, root, arm = e["rig"], e["rig"]["root"], e["rig"]["arm"]
            present.add(e["name"])
            base_rot = rig["base_rot"]
            if keep:
                # walkers turn to face where they go; everybody else keeps the rotation the artist set
                # (or the way they were facing when the previous shot ended)
                yaw = _wrap(e["yaw"] - rig["heading"]) if e["turn"] else e["start_yaw"]
            else:
                yaw = e["yaw"] if e["turn"] else e["start_yaw"]
            root.location = (e["x"], e["y"], e["z"])
            root.keyframe_insert("location", frame=start)
            root.rotation_euler = (base_rot[0], base_rot[1], base_rot[2] + yaw)
            root.keyframe_insert("rotation_euler", frame=start)
            if e["x1"] is not None and not e["rootmotion"] and not e["cue_timeline"]:
                root.location = (e["x1"], e["y1"], e["z"])
                root.keyframe_insert("location", frame=end)
                rig["linear"].add(start)
            # Per-action positions are world-space destinations. Keyframe at the
            # action boundaries so the character travels during that action and
            # remains at the destination for the following action.
            for seg_i, seg in enumerate(e["segments"]):
                target = seg.get("position")
                if e["cue_timeline"]:
                    target = seg.get("to_position")
                    from_pos = seg.get("from_position", (e["x"], e["y"], e["z"]))
                elif target is None:
                    continue
                seg_start = start + seg["start"] - 1
                seg_end = start + seg["end"] - 1
                if not e["cue_timeline"]:
                    previous = next((q.get("position") for q in reversed(e["segments"][:seg_i])
                                     if q.get("position") is not None), None)
                    from_pos = previous or (e["x"], e["y"], e["z"])
                root.location = tuple(from_pos)
                root.keyframe_insert("location", frame=seg_start)
                root.location = tuple(target)
                root.keyframe_insert("location", frame=seg_end)
                rig["linear"].add(seg_start)
                # A walk / run that goes somewhere turns the character towards where it is going.
                if e["cue_timeline"] and seg["cat"] in CL.LOCOMOTION:
                    seg_stats = seg.get("stats") or {}
                    if float(seg_stats.get("travel_m", 0.0)) <= 0.25 * e["H"]:
                        fx0, fx1 = seg["from_position"], seg["to_position"]
                        if math.hypot(fx1[0] - fx0[0], fx1[1] - fx0[1]) > 0.3:
                            face = _wrap(math.atan2(fx1[1] - fx0[1], fx1[0] - fx0[0]) + math.pi / 2)
                            new_yaw = _wrap(face - rig["heading"]) if keep else face
                            if abs(_wrap(new_yaw - yaw)) > 1e-4:
                                yaw = new_yaw
                                root.rotation_euler = (base_rot[0], base_rot[1], base_rot[2] + yaw)
                                root.keyframe_insert("rotation_euler", frame=seg_start)
            if cont:
                if e["cue_timeline"] and e["segments"]:
                    end_pos = tuple(e["segments"][-1].get("to_position", (e["x"], e["y"], e["z"])))
                elif e["x1"] is not None and not e["rootmotion"] and not e["cue_timeline"]:
                    end_pos = (e["x1"], e["y1"], e["z"])
                else:
                    end_pos = (e["x"], e["y"], e["z"])
                carry[e["name"]] = {"pos": end_pos, "yaw": yaw}
            if arm.type == "ARMATURE":
                for seg_i, seg in enumerate(e["segments"]):
                    action_label = seg.get("action", "") or ""
                    for fx in s.vfx_items:
                        trigger = (fx.action_match or "").strip().lower()
                        if trigger and trigger in action_label.lower():
                            vfx_serial += 1
                            try:
                                _spawn_vfx(scene, gen, fx, rig, action_label,
                                           start + seg["start"] - 1, start + seg["end"] - 1, vfx_serial)
                            except Exception as ex:
                                warns.append(f"{sh.name}: VFX '{fx.name}' could not attach ({ex})")
                    if seg["act"] is None:
                        continue
                    act, fit = _retarget(seg["act"], arm)
                    if fit < 0.75:
                        warns.append(f"{sh.name}: '{seg['action']}' fits only {int(fit * 100)}% of {e['name']}'s bones "
                                     "(different skeleton) - the motion may look wrong")
                    ad = arm.animation_data or arm.animation_data_create()
                    if rig["track"] is None:
                        rig["track"] = ad.nla_tracks.new()
                        rig["track"].name = "CS_Animation"
                    seg_start = start + seg["start"] - 1
                    seg_duration = seg["end"] - seg["start"] + 1
                    try:
                        _add_strip(rig["track"],
                                   f"{_safe_name(sh.name)}_{_safe_name(e['name'])}_{seg_i + 1}",
                                   seg_start, act, seg_duration)
                    except Exception as ex:
                        warns.append(f"{sh.name}: could not place '{seg['action']}' for {e['name']} ({ex})")
            else:
                for seg in e["segments"]:
                    _bob(root, start + seg["start"] - 1, start + seg["end"] - 1, seg["cat"], e["H"])
                    action_label = seg.get("action", "") or ""
                    for fx in s.vfx_items:
                        trigger = (fx.action_match or "").strip().lower()
                        if trigger and trigger in action_label.lower():
                            vfx_serial += 1
                            try:
                                _spawn_vfx(scene, gen, fx, rig, action_label,
                                           start + seg["start"] - 1, start + seg["end"] - 1, vfx_serial)
                            except Exception as ex:
                                warns.append(f"{sh.name}: VFX '{fx.name}' could not attach ({ex})")

        # Hand-authored impact / gesture events take precedence over text-only VFX matching.
        # Their frame and offsets are editable per shot and the effect follows the selected rig.
        for event in sh.vfx_events:
            fx = next((item for item in s.vfx_items if item.name == event.effect), None)
            if fx is None:
                warns.append(f"{sh.name}: timed VFX event refers to missing effect '{event.effect}'")
                continue
            actor = next((item for item in entries if item["name"] == event.character), None)
            if actor is None and entries:
                actor = entries[0]
            if actor is None:
                warns.append(f"{sh.name}: timed VFX '{event.effect}' has no character to attach to")
                continue
            local_frame = max(1, min(sh.duration, int(event.frame)))
            event_start = start + local_frame - 1
            event_end = min(end, event_start + max(1, int(event.duration)) - 1)
            active = next((seg for seg in actor["segments"]
                           if seg["start"] <= local_frame <= seg["end"]), None)
            event_action = active.get("action", "") if active else actor.get("action", "")
            event_entry = SimpleNamespace(
                name=event.effect,
                root_object=fx.root_object,
                anchor=fx.anchor,
                offset_x=fx.offset_x + event.offset_x,
                offset_y=fx.offset_y + event.offset_y,
                offset_z=fx.offset_z + event.offset_z,
                rotation_z=fx.rotation_z,
                scale=fx.scale * event.scale)
            vfx_serial += 1
            try:
                _spawn_vfx(scene, gen, event_entry, actor["rig"], event_action,
                           event_start, event_end, vfx_serial)
            except Exception as ex:
                warns.append(f"{sh.name}: timed VFX '{event.effect}' could not attach ({ex})")

        for cname, rig in rigs.items():
            rig["vis"][start] = cname in present

        # ---- camera
        # Reframe at each action boundary. A separate camera marker gives Blender a clean cut
        # while each camera is planned from the action currently playing in that range.
        boundaries = {1, sh.duration + 1}
        if sh.camera_type == "AUTO":
            for angle_i in range(1, max(1, sh.camera_angles)):
                angle_frame = 1 + round(angle_i * sh.duration / max(1, sh.camera_angles))
                if 1 < angle_frame <= sh.duration:
                    boundaries.add(angle_frame)
        for e in entries:
            for seg in e["segments"]:
                boundaries.add(seg["start"])
                boundaries.add(seg["end"] + 1)
        bounds = sorted(boundaries)
        ai_camera_sequence = None
        if s.ollama_enabled and not ollama_failed and sh.camera_type == "AUTO":
            try:
                ai_camera_sequence = _ollama_camera_sequence(s, sh, entries)
                if not ai_camera_sequence:
                    raise RuntimeError("model returned no usable camera sequence")
            except Exception as ex:
                warns.append(f"Ollama camera planning unavailable; using built-in camera planner ({ex})")
                ollama_failed = True
        for seg_i, (local_start, local_after) in enumerate(zip(bounds, bounds[1:])):
            local_end = local_after - 1
            members = []
            for e in entries:
                active = next((q for q in e["segments"] if q["start"] <= local_start <= q["end"]), None)
                cat = active["cat"] if active else ("talk" if words else "idle")
                stats = active["stats"] if active else None
                x1 = e["x"] if e["x1"] is None else e["x1"]
                y1 = e["y"] if e["y1"] is None else e["y1"]
                lift = stats["lift_m"] if stats else 0.0
                members.append(CL.Member(e["name"], e["x"], e["y"], x1, y1, e["H"], cat, lift))
            if not members:
                continue
            manual_camera = sh.camera_type
            if ai_camera_sequence:
                angle_i = min(len(ai_camera_sequence) - 1,
                              int((local_start - 1) * len(ai_camera_sequence) / max(1, sh.duration)))
                manual_camera = ai_camera_sequence[angle_i]
            camera_index = idx + seg_i
            camera_total = total + max(0, len(bounds) - 2)
            seg_start = start + local_start - 1
            seg_end = start + local_end - 1
            # Include the real animated silhouette, not only rest-pose bounds.
            # Imported rigs can grow substantially in poses such as falling or jumping.
            sample_frames = sorted({seg_start, (seg_start + seg_end) // 2, seg_end})
            sample_centers = {}
            for member in members:
                rig = rigs.get(member.name)
                if rig is None:
                    continue
                measured = member.height
                centers = []
                for sample_frame in sample_frames:
                    scene.frame_set(sample_frame)
                    context.view_layer.update()
                    bounds3d = _evaluated_char_bounds(rig["arm"])
                    if bounds3d:
                        low, high = bounds3d
                        measured = max(measured, high[2] - low[2])
                        centers.append(tuple((low[axis] + high[axis]) / 2.0 for axis in range(3)))
                member.height = measured
                sample_centers[member.name] = centers
            plan = CL.plan_shot(members, camera_index, camera_total, words,
                                manual_camera, prev_kind, rep, aspect)
            # The action can translate or fold the posed mesh away from its rest
            # pose. Move both camera and target by the measured center offset so
            # the actual character silhouette stays inside the planned frame.
            shifted = []
            for waypoint_i, (t, camera_pos, target_pos) in enumerate(plan.waypoints):
                sample_i = round(t * (len(sample_frames) - 1))
                centers = [values[sample_i] for values in sample_centers.values()
                           if sample_i < len(values)]
                if centers:
                    center = tuple(sum(value[axis] for value in centers) / len(centers)
                                   for axis in range(3))
                    offset = tuple(center[axis] - target_pos[axis] for axis in range(3))
                    camera_pos = tuple(camera_pos[axis] + offset[axis] for axis in range(3))
                    target_pos = tuple(target_pos[axis] + offset[axis] for axis in range(3))
                shifted.append((t, camera_pos, target_pos))
            plan.waypoints = shifted
            rep = rep + 1 if plan.kind == prev_kind else 0
            prev_kind = plan.kind
            sh.cam_note = plan.note
            suffix = f"_{seg_i + 1:02d}" if len(bounds) > 2 else ""
            cam = _setup_camera(gen, sh, plan, seg_start, seg_end, suffix)
            if first_cam is None:
                first_cam = cam
            mk = scene.timeline_markers.new(f"CS_{_safe_name(sh.name)}_{seg_i + 1:02d}", frame=seg_start)
            mk.camera = cam

        # ---- lights (only on during their own shot)
        preset = LIGHT_PRESETS[sh.light_type]
        lights = []
        for kind in ("key", "fill", "rim"):
            energy, color = preset[kind]
            lights.append(_make_light(gen, f"CS_{_safe_name(sh.name)}_{kind}", "SUN",
                                      energy, color, LIGHT_ROT[kind]))
        _key_visibility(lights, {0: False, start: True, end + 1: False}, viewport=True)

    # --- finish every character: visibility keys, hold-position interpolation
    for cname, rig in rigs.items():
        _key_visibility(_char_objects(rig["arm"]), rig["vis"])
        root = rig["root"]
        if root.animation_data and root.animation_data.action:
            for fc in _fcurves(root.animation_data.action):
                if fc.data_path == "delta_location":
                    continue
                for kp in fc.keyframe_points:
                    glide = fc.data_path == "location" and int(round(kp.co[0])) in rig["linear"]
                    kp.interpolation = "LINEAR" if glide else "CONSTANT"

    scene.frame_start = 1
    scene.frame_end = max(1, frame - 1)
    scene.render.fps = s.fps
    scene.render.resolution_x = s.res_x
    scene.render.resolution_y = s.res_y
    if scene.world is None:
        scene.world = bpy.data.worlds.new("CS_World")
    if first_cam is not None:
        scene.camera = first_cam
    _purge_actions()
    scene.frame_set(1)
    return warns


class CARTOON_OT_build_episode(Operator):
    bl_idname = "cartoon.build_episode"
    bl_label = "Build Episode"
    bl_description = ("Place characters, play their animations, plan the camera and lights for every shot. "
                      "Shots follow each other on one timeline (shot 1 = frames 1-100, shot 2 = 101-200 ...)")

    def execute(self, context):
        s = _S(context)
        if not len(s.shots):
            self.report({"ERROR"}, "Add shots first")
            return {"CANCELLED"}
        try:
            warns = build_episode(context)
        except Exception as e:
            _log(s, traceback.format_exc())
            self.report({"ERROR"}, f"Build failed: {e}")
            return {"CANCELLED"}
        for w in warns:
            _log(s, "WARN " + w)
        msg = f"Built {len(s.shots)} shots, frames 1-{context.scene.frame_end}"
        if warns:
            msg += f" ({len(warns)} warnings - run Quality Check)"
        self.report({"WARNING" if warns else "INFO"}, msg)
        return {"FINISHED"}


classes = (
    CARTOON_OT_build_episode,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
