"""Character import (ANY model), auto rigging and the character library.

v1.2: nothing is refused any more.
  * rigged + skinned + any pose      -> accepted (a note says if it is not a T-pose)
  * skeleton but mesh not skinned    -> mesh is skinned to the skeleton automatically
  * mesh with NO skeleton            -> auto-rigged with the Rig Template if one is set,
                                        otherwise kept as a one-piece character (still placed,
                                        moved and filmed - it just has no limb animation)
  * wrong size / wrong direction     -> scaled to the Character height and turned to face the camera
"""
import bpy
import os
import re
import math
import json
import zipfile
import tempfile
import random
import traceback
from mathutils import Matrix, Vector
from bpy.props import StringProperty, BoolProperty, EnumProperty, FloatProperty
from bpy.props import CollectionProperty
from bpy.types import Operator, OperatorFileListElement

from .helpers import (_S, _safe_name, _log, _get_collection, _delete_objects, _new_objects,
                      _project_dir, _ensure, _char_missing, _char_objects, _world_bbox, _canon_bone)
from .director import build_episode
from . import rigfit as RF


IMPORT_EXTS = (".fbx", ".glb", ".gltf", ".obj", ".dae", ".stl", ".ply",
               ".usd", ".usda", ".usdc", ".blend", ".zip")
# when a .zip holds several files, the first type in this list is used
ZIP_PREFERENCE = (".fbx", ".glb", ".gltf", ".blend", ".obj", ".dae",
                  ".usd", ".usda", ".usdc", ".ply", ".stl")
FACING_ITEMS = [
    ("NEG_Y", "Toward camera (-Y)", "Model already faces the front camera"),
    ("POS_Y", "Facing +Y", "Rotate 180 degrees"),
    ("POS_X", "Facing +X", "Rotate to face the camera"),
    ("NEG_X", "Facing -X", "Rotate to face the camera"),
]


def _deselect_all(context):
    """Deselect everything. Safe right after objects were deleted or created (Blender can hand back an
    empty entry in view_layer.objects until the layer is refreshed)."""
    try:
        context.view_layer.update()
    except Exception:
        pass
    for o in list(context.view_layer.objects):
        if o is None:
            continue
        try:
            o.select_set(False)
        except Exception:
            pass


def _select(context, objs):
    for o in objs:
        try:
            o.select_set(True)
        except Exception:
            pass


def _extract_zip(context, path):
    """Unzip (inside the project folder when set, so textures survive a restart).
    Returns (best model file, extracted_inside_project)."""
    proj = _project_dir(_S(context))
    if proj:
        parent = _ensure(os.path.join(proj, "assets", "characters", "_unzipped"))
    else:
        parent = bpy.app.tempdir or None
    prefix = _safe_name(os.path.splitext(os.path.basename(path))[0]) + "_"
    dest = tempfile.mkdtemp(prefix=prefix, dir=parent)
    base = os.path.realpath(dest)
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():
            target = os.path.realpath(os.path.join(dest, member))
            if target != base and not target.startswith(base + os.sep):
                raise ValueError("Unsafe file path inside the zip")
        z.extractall(dest)
    found = []
    for root, _dirs, files in os.walk(dest):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ZIP_PREFERENCE:
                found.append((ZIP_PREFERENCE.index(ext), root.count(os.sep), os.path.join(root, f)))
    if not found:
        raise ValueError("No model file (.fbx .glb .obj ...) found inside the zip")
    found.sort()
    return found[0][2], bool(proj)


def _import_file(context, path):
    ext = os.path.splitext(path)[1].lower()
    pack_after = False
    if ext == ".zip":
        path, in_project = _extract_zip(context, path)
        pack_after = not in_project  # temp folder disappears -> pack textures into the .blend
        ext = os.path.splitext(path)[1].lower()
    images_before = set(bpy.data.images)
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".dae":
        bpy.ops.wm.collada_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext in (".usd", ".usda", ".usdc"):
        bpy.ops.wm.usd_import(filepath=path)
    elif ext == ".blend":
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for o in dst.objects:
            if o is not None:
                context.scene.collection.objects.link(o)
    else:
        raise ValueError("Unsupported file type: " + ext)
    # GLB imports may extract embedded textures to a temporary directory.
    # Pack newly imported images before that temp directory can disappear.
    if pack_after or ext in (".glb", ".gltf"):
        for image in (item for item in bpy.data.images if item not in images_before):
            try:
                if image.has_data and not image.packed_file:
                    image.pack()
            except Exception:
                pass


def _delete_by_names(names):
    _delete_objects([bpy.data.objects[n] for n in names if n in bpy.data.objects])


def _normalize(context, new, meshes, facing, target_h):
    """Rotate to face camera, scale to target height, put feet at the origin."""
    names = {o.name for o in new}
    top = [o for o in new if o.parent is None or o.parent.name not in names]
    ang = {"NEG_Y": 0.0, "POS_Y": math.pi, "POS_X": -math.pi / 2, "NEG_X": math.pi / 2}[facing]
    if ang:
        rot = Matrix.Rotation(ang, 4, "Z")
        for o in top:
            o.matrix_world = rot @ o.matrix_world
    context.view_layer.update()
    mn, mx = _world_bbox(meshes)
    h = mx[2] - mn[2]
    if target_h > 0 and h > 1e-6:
        sc = Matrix.Scale(target_h / h, 4)
        for o in top:
            o.matrix_world = sc @ o.matrix_world
        context.view_layer.update()
        mn, mx = _world_bbox(meshes)
    tr = Matrix.Translation((-(mn[0] + mx[0]) / 2, -(mn[1] + mx[1]) / 2, -mn[2]))
    for o in top:
        o.matrix_world = tr @ o.matrix_world
    context.view_layer.update()


def _weighted_fraction(m):
    """Share of the mesh's vertices that have at least one bone weight (0 if it is not attached)."""
    if m.parent is None or m.parent.type != "ARMATURE":
        return 0.0
    verts = m.data.vertices
    if not len(verts):
        return 1.0
    ok = sum(1 for v in verts if any(g.weight > 1e-4 for g in v.groups))
    return ok / float(len(verts))


def _has_weights(meshes):
    """True only when EVERY vertex of every mesh is weighted."""
    return all(_weighted_fraction(m) >= 0.9999 for m in meshes)


# Bones that should not take weight when skinning a cartoon mesh by distance: a mitten hand or a
# round foot must move as one piece with its hand/foot bone, not be split over 15 finger/toe bones.
_NO_WEIGHT = re.compile(r"(thumb|index|middle|ring|pinky|toe|headtop|eye|jaw)")


def _weight_bones(arm):
    mwa = arm.matrix_world
    every = [b for b in arm.data.bones if b.use_deform]
    main = [b for b in every if not _NO_WEIGHT.search(_canon_bone(b.name))]
    use = main if len(main) >= 6 else every
    return [(b.name, tuple(mwa @ b.head_local), tuple(mwa @ b.tail_local)) for b in use]


def _topology_smooth_weights(mesh, weights, passes=2, keep=4):
    """Soften fallback weights across connected surface edges without crossing mesh gaps.

    Distance-only weights can change abruptly from one vertex to the next around joints.
    A small, anchored neighborhood blend makes those transitions smoother while retaining
    each vertex's own bone-distance estimate and limiting the number of influences.
    """
    vertex_count = len(mesh.data.vertices)
    if vertex_count < 2 or len(mesh.data.edges) == 0:
        return weights
    neighbors = [[] for _ in range(vertex_count)]
    for edge in mesh.data.edges:
        a, b = edge.vertices
        neighbors[a].append(b)
        neighbors[b].append(a)
    current = weights
    for _ in range(passes):
        updated = []
        for i, own in enumerate(weights):
            linked = neighbors[i]
            if not linked:
                updated.append(own)
                continue
            blend = {name: 0.75 * value for name, value in own.items()}
            factor = 0.25 / len(linked)
            for j in linked:
                for name, value in current[j].items():
                    blend[name] = blend.get(name, 0.0) + factor * value
            strongest = sorted(blend.items(), key=lambda item: item[1], reverse=True)[:keep]
            total = sum(value for _name, value in strongest)
            updated.append({name: value / total for name, value in strongest} if total > 0 else own)
        current = updated
    return current


def _blended_weights(meshes, arm):
    """Skin every vertex to nearby bones, then smooth transitions over mesh topology.
    Works without Blender's heat-map solver and keeps disconnected mesh parts separate."""
    prepared = RF.prepare_bones(_weight_bones(arm))
    if not prepared:
        raise RuntimeError("the skeleton has no deforming bones")
    mn, mx = _world_bbox(meshes)
    scale = max(mx[2] - mn[2], 1e-3)
    for m in meshes:
        for mod in [x for x in m.modifiers if x.type == "ARMATURE"]:
            m.modifiers.remove(mod)
        m.vertex_groups.clear()
        mw = m.matrix_world.copy()
        weights = [RF.blend_weights(tuple(mw @ v.co), prepared, scale) for v in m.data.vertices]
        weights = _topology_smooth_weights(m, weights)
        groups = {}
        for v, vertex_weights in zip(m.data.vertices, weights):
            for name, w in vertex_weights.items():
                vg = groups.get(name)
                if vg is None:
                    vg = groups[name] = m.vertex_groups.new(name=name)
                vg.add([v.index], w, "REPLACE")
        m.parent = arm
        m.matrix_parent_inverse = arm.matrix_world.inverted()
        m.matrix_world = mw
        mod = m.modifiers.new("Armature", "ARMATURE")
        mod.object = arm


def _fill_unweighted(meshes, arm):
    """The heat solver sometimes leaves a few vertices without weight (they stay behind when the
    character moves). Give just those vertices blended weights. Returns how many were fixed."""
    prepared = RF.prepare_bones(_weight_bones(arm))
    if not prepared:
        return 0
    mn, mx = _world_bbox(meshes)
    scale = max(mx[2] - mn[2], 1e-3)
    fixed = 0
    for m in meshes:
        mw = m.matrix_world
        for v in m.data.vertices:
            if any(g.weight > 1e-4 for g in v.groups):
                continue
            for name, w in RF.blend_weights(tuple(mw @ v.co), prepared, scale).items():
                vg = m.vertex_groups.get(name) or m.vertex_groups.new(name=name)
                vg.add([v.index], w, "REPLACE")
            fixed += 1
    return fixed


def _auto_rig(context, meshes, template_path):
    """Fit a Mixamo template skeleton to the meshes and skin them. Returns (armature, method)."""
    view_layer = context.view_layer
    mn, mx = _world_bbox(meshes)
    h = mx[2] - mn[2]
    if h < 1e-6:
        raise RuntimeError("Mesh has no height")

    before = {o.name for o in bpy.data.objects}
    _import_file(context, template_path)
    tnew = _new_objects(before)
    tnames = [o.name for o in tnew]
    arm = next((o for o in tnew if o.type == "ARMATURE"), None)
    if arm is None:
        _delete_by_names(tnames)
        raise RuntimeError("Rig template has no armature - use a Mixamo T-pose .fbx")
    view_layer.update()
    tmeshes = [o for o in tnew if o.type == "MESH"]
    if tmeshes:
        tmn, tmx = _world_bbox(tmeshes)
    else:
        pts = [arm.matrix_world @ b.head_local for b in arm.data.bones]
        pts += [arm.matrix_world @ b.tail_local for b in arm.data.bones]
        tmn = [min(p[i] for p in pts) for i in range(3)]
        tmx = [max(p[i] for p in pts) for i in range(3)]
    th = tmx[2] - tmn[2]
    if th < 1e-6:
        _delete_by_names(tnames)
        raise RuntimeError("Could not measure the rig template")

    mwt = arm.matrix_world.copy()
    arm.parent = None
    arm.matrix_world = mwt
    _delete_by_names([n for n in tnames if n != arm.name])
    arm.animation_data_clear()
    for pb in arm.pose.bones:
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
        pb.scale = (1, 1, 1)

    k = h / th
    origin = arm.location.copy()
    arm.scale = [v * k for v in arm.scale]
    ref = Vector(((tmn[0] + tmx[0]) / 2, (tmn[1] + tmx[1]) / 2, tmn[2]))
    ref_after = origin + (ref - origin) * k
    want = Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2]))
    arm.location = origin + (want - ref_after)
    view_layer.update()

    method = _skin_to(context, meshes, arm)
    return arm, method

# ---------------------------------------------------------------- built-in skeleton (T-pose or A-pose)
POSE_ITEMS = [
    ("T", "T-pose", "The model stands with its arms straight out to the sides"),
    ("A", "A-pose", "The model stands with its arms angled down (about 40 degrees)"),
]
TEMPLATE_TEXT = "CS_RigTemplate"          # the skeleton shape is kept inside the .blend in this text block
_ARM_ROOTS = {"leftarm", "rightarm", "leftupperarm", "rightupperarm"}
_LEG_ROOTS = {"leftupleg", "rightupleg", "leftupperleg", "rightupperleg", "leftthigh", "rightthigh"}
_template_cache = {}


def _extract_template(arm):
    """Describe a skeleton as plain numbers: feet at the origin, height = 1, every bone's exact axes copied.
    (Mixamo animations only look right on bones that have the same axes as Mixamo's own.)"""
    mw = arm.matrix_world
    raw, zs = [], []
    for b in arm.data.bones:
        h, t = mw @ b.head_local, mw @ b.tail_local
        raw.append((b, h, t, (mw @ b.matrix_local).to_3x3().normalized()))
        zs += [h.z, t.z]
    if not raw:
        raise RuntimeError("the skeleton has no bones")
    zmin, zmax = min(zs), max(zs)
    height = zmax - zmin
    if height < 1e-6:
        raise RuntimeError("the skeleton has no height")
    root = next((x for x in raw if x[0].parent is None), raw[0])
    ox, oy = root[1].x, root[1].y
    bones = []
    for b, h, t, rot in raw:
        bones.append({
            "n": b.name, "p": b.parent.name if b.parent else "",
            "h": [(h.x - ox) / height, (h.y - oy) / height, (h.z - zmin) / height],
            "t": [(t.x - ox) / height, (t.y - oy) / height, (t.z - zmin) / height],
            "r": [rot[i][j] for i in range(3) for j in range(3)],
        })
    return {"v": 1, "bones": bones}


def store_rig_template(arm):
    """Remember a skeleton (called automatically when a Mixamo animation file is imported)."""
    tpl = _extract_template(arm)
    txt = bpy.data.texts.get(TEMPLATE_TEXT) or bpy.data.texts.new(TEMPLATE_TEXT)
    txt.clear()
    txt.write(json.dumps(tpl))
    return tpl


def _get_template(context):
    """Skeleton shape to build from: the Rig Template file if one is set, else the one remembered
    from an imported Mixamo animation. None if there is neither."""
    s = _S(context)
    path = bpy.path.abspath(s.rig_template) if s.rig_template else ""
    if path and os.path.isfile(path):
        key = (path, os.path.getmtime(path))
        if key in _template_cache:
            return _template_cache[key]
        before = {o.name for o in bpy.data.objects}
        tpl = None
        try:
            _import_file(context, path)
            arm = next((o for o in _new_objects(before) if o.type == "ARMATURE"), None)
            tpl = _extract_template(arm) if arm is not None else None
        finally:
            _delete_by_names([o.name for o in _new_objects(before)])
        if tpl:
            _template_cache[key] = tpl
            return tpl
    txt = bpy.data.texts.get(TEMPLATE_TEXT)
    if txt is not None:
        try:
            return json.loads(txt.as_string())
        except Exception:
            pass
    return None


def _descendants(children, name):
    out, stack = [], [name]
    while stack:
        n = stack.pop()
        out.append(n)
        stack += children.get(n, [])
    return out


def _surface_points(meshes, n=24000):
    """Random points on the mesh SURFACE (not just its corner vertices), so low-poly cartoon models
    measure correctly. Fixed seed -> same result every time."""
    rnd = random.Random(7)
    sets, total = [], 0.0
    for m in meshes:
        me = m.data
        try:
            me.calc_loop_triangles()
        except Exception:
            pass
        tris = me.loop_triangles
        if not len(tris):
            continue
        areas = [t.area for t in tris]
        sa = sum(areas)
        if sa > 0:
            sets.append((m, tris, areas, sa))
            total += sa
    pts = []
    for m, tris, areas, sa in sets:
        mw, co = m.matrix_world, m.data.vertices
        k = max(300, int(n * sa / total))
        for i in rnd.choices(range(len(tris)), weights=areas, k=k):
            a, b, c = tris[i].vertices
            u, v = rnd.random(), rnd.random()
            if u + v > 1.0:
                u, v = 1.0 - u, 1.0 - v
            p = co[a].co + (co[b].co - co[a].co) * u + (co[c].co - co[a].co) * v
            pts.append(tuple(mw @ p))
    return pts


def _fit_proportions(P, bones, children, meshes, base, height):
    """Move the template joints so they sit where THIS body's crotch, neck and legs are
    (a big-headed cartoon has hips/shoulders in very different places than a human).
    Changes P in place. Returns a list of what was fitted; [] means 'average human proportions'."""
    pts = _surface_points(meshes)
    if len(pts) < 300:
        return []
    norm = [((x - base.x) / height, (y - base.y) / height, (z - base.z) / height) for x, y, z in pts]
    lm = RF.detect_landmarks(norm)
    if not lm:
        return []
    cn = {_canon_bone(n): n for n in bones}
    leg_roots = [n for n in bones if _canon_bone(n) in _LEG_ROOTS]
    tpl_crotch = None
    if leg_roots:
        tpl_crotch = sum(bones[n]["h"][2] for n in leg_roots) / len(leg_roots) - RF.CROTCH_BELOW_HIP
    nz = bones[cn["neck"]]["h"][2] if "neck" in cn else None
    hz = bones[cn["head"]]["h"][2] if "head" in cn else None
    tpl_neck = (nz + hz) / 2.0 if (nz is not None and hz is not None) else None

    done = []
    knots = RF.fit_knots(lm, tpl_crotch, tpl_neck)
    if knots:
        for n in P:
            for k in (0, 1):
                z = (P[n][k].z - base.z) / height
                P[n][k].z = base.z + RF.remap_z(z, knots) * height
        if any(tpl_crotch is not None and abs(a - tpl_crotch) < 1e-9 for a, _b in knots):
            done.append("hips height")
        if any(tpl_neck is not None and abs(a - tpl_neck) < 1e-9 for a, _b in knots):
            done.append("neck height")
    hs, cr = lm.get("leg_half_sep"), lm.get("crotch")
    if hs and cr is not None and 0.2 <= cr <= 0.62 and leg_roots:
        ts = sum(abs(bones[n]["h"][0]) for n in leg_roots) / len(leg_roots)
        if ts > 0.01:
            sx = max(0.5, min(2.0, hs / ts))
            if abs(sx - 1.0) > 0.05:
                for r in leg_roots:
                    for c in _descendants(children, r):
                        for k in (0, 1):
                            P[c][k].x = base.x + (P[c][k].x - base.x) * sx
                done.append("leg spacing")
    return done


# ---------------------------------------------------------------- rig markers (place circles, then build)
MARKER_COLL = "CS_RigMarkers"
_MK_BONE_KEYS = {
    "shoulder": ("arm", "upperarm"),
    "elbow": ("forearm", "lowerarm"),
    "wrist": ("hand",),
    "knee": ("leg", "lowerleg", "shin", "calf"),
    "ankle": ("foot",),
    "hip": ("upleg", "upperleg", "thigh"),
}
# (id, label shown next to the circle, colour)
_MARKER_DEFS = [
    ("head_top", "Head Top", (0.3, 0.9, 0.9, 1.0)),
    ("chin", "Chin", (0.0, 0.8, 1.0, 1.0)),
    ("neck", "Neck Base", (0.7, 0.5, 1.0, 1.0)),
    ("shoulder_L", "Shoulder L", (0.4, 0.6, 1.0, 1.0)),
    ("shoulder_R", "Shoulder R", (0.4, 0.6, 1.0, 1.0)),
    ("elbow_L", "Elbow L", (1.0, 0.9, 0.2, 1.0)),
    ("elbow_R", "Elbow R", (1.0, 0.9, 0.2, 1.0)),
    ("wrist_L", "Wrist L", (0.6, 1.0, 0.2, 1.0)),
    ("wrist_R", "Wrist R", (0.6, 1.0, 0.2, 1.0)),
    ("pelvis", "Pelvis", (0.9, 0.9, 0.9, 1.0)),
    ("groin", "Groin", (1.0, 0.4, 0.6, 1.0)),
    ("knee_L", "Knee L", (1.0, 0.6, 0.2, 1.0)),
    ("knee_R", "Knee R", (1.0, 0.6, 0.2, 1.0)),
    ("ankle_L", "Ankle L", (1.0, 0.3, 0.2, 1.0)),
    ("ankle_R", "Ankle R", (1.0, 0.3, 0.2, 1.0)),
]


def _template_geometry(tpl):
    bones = {b["n"]: b for b in tpl["bones"]}
    children = {}
    for b in tpl["bones"]:
        children.setdefault(b["p"], []).append(b["n"])
    cn = {}
    for n in bones:
        cn.setdefault(_canon_bone(n), n)
    return bones, children, cn


def _find_bone(cn, side, kind):
    for k in _MK_BONE_KEYS[kind]:
        n = cn.get(side + k)
        if n:
            return n
    return None


def _marker_points(cn, H, T, unit):
    """Where each marker belongs on a skeleton. H / T = {bone: head / tail position}; unit = body height
    of those positions (the chin and groin sit a fixed fraction of the body away from a joint)."""
    out = {}
    head, neck, hips = cn.get("head"), cn.get("neck"), cn.get("hips")
    if head in H:
        top = cn.get("headtopend")
        out["head_top"] = H[top].copy() if top in H else T[head].copy()
        out["chin"] = H[head] - Vector((0.0, 0.0, 0.025 * unit))
    if neck in H:
        out["neck"] = H[neck].copy()
    if hips in H:
        out["pelvis"] = H[hips].copy()
    hj = [H[n] for n in (_find_bone(cn, sd, "hip") for sd in ("left", "right")) if n in H]
    if hj:
        out["groin"] = Vector((sum(v.x for v in hj) / len(hj), sum(v.y for v in hj) / len(hj),
                               sum(v.z for v in hj) / len(hj) - 0.045 * unit))
    for side, tag in (("left", "L"), ("right", "R")):
        for kind in ("shoulder", "elbow", "wrist", "knee", "ankle"):
            n = _find_bone(cn, side, kind)
            if n in H:
                out[f"{kind}_{tag}"] = H[n].copy()
    return out


def _fit_arm_reach(P, bones, children, base, mn, mx):
    """T-pose: stretch/shrink the template arms so they are as long as the mesh arms."""
    half_span = (mx[0] - mn[0]) / 2.0
    for n in [n for n in bones if _canon_bone(n) in _ARM_ROOTS]:
        chain = [c for c in _descendants(children, n) if c in P]
        anchor = P[n][0].copy()
        reach = max(abs(P[c][k].x - base.x) for c in chain for k in (0, 1))
        ax = abs(anchor.x - base.x)
        if reach - ax > 1e-6:
            r = max(0.5, min(2.0, (half_span - ax) / (reach - ax)))
            for c in chain:
                for k in (0, 1):
                    P[c][k].x = anchor.x + (P[c][k].x - anchor.x) * r


def _default_markers(meshes, tpl):
    """Starting positions for the circles: the template skeleton already fitted to the body.
    Returns {marker id: world position}."""
    mn, mx = _world_bbox(meshes)
    height = mx[2] - mn[2]
    if height < 1e-6:
        raise RuntimeError("the mesh has no height")
    base = Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2]))
    bones, children, cn = _template_geometry(tpl)
    P = {n: [Vector(b["h"]) * height + base, Vector(b["t"]) * height + base] for n, b in bones.items()}
    try:
        _fit_proportions(P, bones, children, meshes, base, height)
        _fit_arm_reach(P, bones, children, base, mn, mx)
    except Exception:
        pass
    pts = _marker_points(cn, {n: v[0] for n, v in P.items()}, {n: v[1] for n, v in P.items()}, height)
    return pts, height


def _scale_chain(P, children, root, center, s, axes):
    """Scale a bone and everything below it around 'center' (axes = which of x,y,z to scale)."""
    def sc(v):
        return Vector(tuple(center[i] + (v[i] - center[i]) * (s if axes[i] else 1.0) for i in range(3)))
    P[root][1] = sc(P[root][1])
    for c in _descendants(children, root):
        if c == root or c not in P:
            continue
        for k in (0, 1):
            P[c][k] = sc(P[c][k])


def _warp_to_markers(P, bones, children, markers, base, height):
    """Move every template joint so the skeleton's joints sit on the user's markers.
    P = {bone: [head, tail]} in world space, changed in place. Returns a list of notes."""
    _b, _c, cn = _template_geometry({"bones": list(bones.values())})
    Hn = {n: Vector(b["h"]) for n, b in bones.items()}
    Tn = {n: Vector(b["t"]) for n, b in bones.items()}
    src_pts = _marker_points(cn, Hn, Tn, 1.0)
    ids = [i for i in src_pts if i in markers]
    if len(ids) < 8:
        raise RuntimeError("only %d marker(s) match this skeleton - press 'Add Rig Markers' again" % len(ids))
    inv = 1.0 / height
    dst_pts = {i: (markers[i] - base) * inv for i in ids}
    src = [tuple(src_pts[i]) for i in ids]
    dst = [tuple(dst_pts[i]) for i in ids]
    for side, tag in (("left", "L"), ("right", "R")):       # floor anchors: the whole foot stays on the ground
        a, foot = f"ankle_{tag}", _find_bone(cn, side, "ankle")
        if a in ids and foot:
            sp, dp = src_pts[a], dst_pts[a]
            dx = dp.x - sp.x
            src.append((sp.x, sp.y, 0.0))
            dst.append((dp.x, sp.y, 0.0))
            for c in _descendants(children, foot):
                for v in (Hn[c], Tn[c]):
                    if v.z <= 0.04:
                        src.append(tuple(v))
                        dst.append((v.x + dx, v.y, v.z))
    keys = [(n, k) for n in P for k in (0, 1)]
    pts = [tuple((P[n][k] - base) * inv) for n, k in keys]
    new = RF.warp_points(pts, src, dst)
    for (n, k), q in zip(keys, new):
        P[n][k] = Vector(q) * height + base
    # hands and feet follow the length of the arm / leg the user marked
    for side, tag in (("left", "L"), ("right", "R")):
        for lower, upper, root_kind, axes in (("wrist", "elbow", "wrist", (1, 1, 1)),
                                              ("ankle", "knee", "ankle", (1, 1, 0))):
            lo, up = f"{lower}_{tag}", f"{upper}_{tag}"
            root = _find_bone(cn, side, root_kind)
            if root in P and lo in ids and up in ids:
                lt = (src_pts[lo] - src_pts[up]).length
                lm = (dst_pts[lo] - dst_pts[up]).length
                if lt > 1e-6 and lm > 1e-6:
                    _scale_chain(P, children, root, P[root][0].copy(), max(0.5, min(2.0, lm / lt)), axes)
    return ["your markers"]


def _build_skeleton(context, meshes, tpl, pose="T", angle=40.0, markers=None):
    """Create a humanoid skeleton that fits the mesh. pose 'T' = arms straight out, 'A' = arms angled down.
    Returns (armature_object, pose)."""
    mn, mx = _world_bbox(meshes)
    height = mx[2] - mn[2]
    if height < 1e-6:
        raise RuntimeError("the mesh has no height")
    base = Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2]))
    bones = {b["n"]: b for b in tpl["bones"]}
    children = {}
    for b in tpl["bones"]:
        children.setdefault(b["p"], []).append(b["n"])
    P = {n: [Vector(b["h"]) * height + base, Vector(b["t"]) * height + base] for n, b in bones.items()}
    R = {n: Matrix(((b["r"][0], b["r"][1], b["r"][2]),
                    (b["r"][3], b["r"][4], b["r"][5]),
                    (b["r"][6], b["r"][7], b["r"][8]))) for n, b in bones.items()}

    fitted = []
    if markers:
        fitted = _warp_to_markers(P, bones, children, markers, base, height)   # errors go to the user
    else:
        try:
            fitted = _fit_proportions(P, bones, children, meshes, base, height)
        except Exception:
            _log(_S(context), "Proportion fit skipped:\n" + traceback.format_exc())
            fitted = []

    arm_roots = [] if markers else [n for n in bones if _canon_bone(n) in _ARM_ROOTS]
    if pose == "A" and not arm_roots:
        raise RuntimeError("the skeleton template has no arm bones")
    corr = {}
    half_span = (mx[0] - mn[0]) / 2.0
    for n in arm_roots:
        chain = _descendants(children, n)
        anchor = P[n][0].copy()
        side = 1.0 if (anchor.x - base.x) >= 0 else -1.0
        if pose == "A":
            rc = Matrix.Rotation(math.radians(angle) * side, 3, "Y")
            frame = R[n].copy()
            # Mixamo animations expect T-pose arms. Because this arm rests rotated, its keys are
            # multiplied by this fixed rotation when the animation is placed (see director._retarget).
            corr[n] = list((frame.transposed() @ rc.transposed() @ frame).to_quaternion())
            for c in chain:
                for k in (0, 1):
                    P[c][k] = anchor + rc @ (P[c][k] - anchor)
                R[c] = rc @ R[c]
        else:
            reach = max(abs(P[c][k].x - base.x) for c in chain for k in (0, 1))
            ax = abs(anchor.x - base.x)
            if reach - ax > 1e-6:
                r = max(0.5, min(2.0, (half_span - ax) / (reach - ax)))   # arms as long as the mesh arms
                for c in chain:
                    for k in (0, 1):
                        P[c][k].x = anchor.x + (P[c][k].x - anchor.x) * r

    arm_data = bpy.data.armatures.new("CS_Skeleton")
    arm_obj = bpy.data.objects.new("CS_Skeleton", arm_data)
    context.scene.collection.objects.link(arm_obj)
    vl = context.view_layer
    _deselect_all(context)
    _select(context, [arm_obj])
    vl.objects.active = arm_obj
    order, seen = [], set()
    for n in bones:                       # parents before children
        chain, cur = [], n
        while cur and cur in bones and cur not in seen:
            chain.append(cur)
            cur = bones[cur]["p"]
        for c in reversed(chain):
            seen.add(c)
            order.append(c)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        ebs = {}
        for n in order:
            h, t = P[n]
            length = max((t - h).length, 1e-4)
            eb = arm_data.edit_bones.new(n)
            eb.head = h
            eb.tail = h + R[n].col[1] * length          # the bone's own Y axis points along the bone
            m = R[n].to_4x4()
            m.translation = h
            eb.matrix = m                                # copies the exact bone axes (roll)
            eb.length = length
            ebs[n] = eb
        for n in order:
            p = bones[n]["p"]
            if p in ebs:
                ebs[n].parent = ebs[p]
                ebs[n].use_connect = False
    finally:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    arm_obj["cs_rest_pose"] = pose
    arm_obj["cs_fit"] = ", ".join(fitted)
    if corr:
        arm_obj["cs_apose"] = json.dumps(corr)
    return arm_obj, pose


def _rig_meshes(context, meshes, pose="T", angle=40.0):
    """Give mesh object(s) a skeleton in the chosen pose and skin them. Returns (armature, weights method)."""
    tpl = _get_template(context)
    if tpl is None:
        raise RuntimeError("no skeleton shape known yet - import one Mixamo animation (.fbx) in the "
                           "Animation Library first, or set a Rig Template in Character Library")
    arm, _ = _build_skeleton(context, meshes, tpl, pose, angle)
    how = _skin_to(context, meshes, arm)
    return arm, how
# ---------------------------------------------------------------- pose notes (never blocks)
_ARM_NAMES = {
    "L": ("leftarm", "leftupperarm", "upperarml", "armleft", "arml", "upperarmleft"),
    "R": ("rightarm", "rightupperarm", "upperarmr", "armright", "armr", "upperarmright"),
}
_HAND_NAMES = {
    "L": ("lefthand", "handl", "handleft"),
    "R": ("righthand", "handr", "handright"),
}
TPOSE_MAX_ANGLE = 20.0  # degrees an arm may be away from horizontal before we mention it


def _norm_bone(name):
    return re.sub(r"[^a-z0-9]", "", name.split(":")[-1].lower())


def _check_tpose(context, arm):
    """Information only: is the rest pose a T-pose? Returns (ok, message). Never rejects a model."""
    context.view_layer.update()
    bones = {}
    for b in arm.data.bones:
        bones.setdefault(_norm_bone(b.name), b)
    mw = arm.matrix_world
    vecs = {}
    for side, label in (("L", "left"), ("R", "right")):
        upper = next((bones[n] for n in _ARM_NAMES[side] if n in bones), None)
        hand = next((bones[n] for n in _HAND_NAMES[side] if n in bones), None)
        if upper is None or hand is None:
            return True, ""   # non-standard bone names: nothing we can say about the pose
        d = (mw @ hand.head_local) - (mw @ upper.head_local)
        ang = math.degrees(math.atan2(abs(d.z), math.hypot(d.x, d.y)))
        if ang > TPOSE_MAX_ANGLE:
            return False, (f"rest pose is not a T-pose (the {label} arm is {ang:.0f} degrees off horizontal) - "
                           "animations still play, arms may look slightly different")
        vecs[side] = (d.x, d.y)
    if vecs["L"][0] * vecs["R"][0] + vecs["L"][1] * vecs["R"][1] >= 0:
        return False, "rest pose is not a T-pose (both arms point the same way) - animations still play"
    return True, ""


def _is_skinned(meshes):
    for m in meshes:
        has_mod = any(x.type == "ARMATURE" and x.object for x in m.modifiers)
        is_child = m.parent is not None and m.parent.type == "ARMATURE"
        if (has_mod or is_child) and len(m.vertex_groups):
            return True
    return False


def _unskinned(meshes):
    return [m for m in meshes if not _is_skinned([m])]


def _skin_to(context, meshes, arm):
    """Attach meshes to an existing skeleton. Blender's automatic weights first (best quality);
    any mesh where that failed gets blended distance weights; stray unweighted vertices are fixed.
    Returns 'smooth', 'smooth (N stray vertices fixed)' or 'blended'."""
    vl = context.view_layer
    try:
        _deselect_all(context)
        _select(context, list(meshes) + [arm])
        vl.objects.active = arm
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception:
        pass
    poor = [m for m in meshes if _weighted_fraction(m) < 0.8]
    good = [m for m in meshes if m not in poor]
    if poor:
        _blended_weights(poor, arm)
    fixed = _fill_unweighted(good, arm) if good else 0
    if poor:
        return "blended"
    return f"smooth ({fixed} stray vertices fixed)" if fixed else "smooth"


def _find_rig(objs):
    """The object that stands for a character: its armature, or the one-piece root of a rig-less model."""
    arms = [o for o in objs if o.type == "ARMATURE"]
    if arms:
        return max(arms, key=lambda a: len(a.data.bones))
    return next((o for o in objs if o.get("cs_static")), None)


def _make_static_root(context, new, meshes):
    """Rig-less model: put it under one root so it can be placed, moved and filmed as a single piece."""
    mn, mx = _world_bbox(meshes)
    root = bpy.data.objects.new("Static_Root", None)
    root.empty_display_type = "ARROWS"
    root.empty_display_size = 0.3
    root["cs_static"] = True
    root.location = ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2])
    context.scene.collection.objects.link(root)
    context.view_layer.update()
    names = {o.name for o in new}
    for o in new:
        if o.parent is None or o.parent.name not in names:
            mw = o.matrix_world.copy()
            o.parent = root
            o.matrix_parent_inverse = root.matrix_world.inverted()
            o.matrix_world = mw
    return root


def _prepare_character(context, new, facing, target_h, pose="T", a_angle=40.0):
    """Turn whatever was imported into something the Studio can animate. Never refuses a model.
    pose = "T" or "A": the pose the mesh is in, used when a skeleton has to be created.
    Returns (rig_object, [notes])."""
    notes = []
    meshes = [o for o in new if o.type == "MESH"]
    if not meshes:
        raise ValueError("the file contains no mesh (only %s)" %
                         (", ".join(sorted({o.type.lower() for o in new})) or "nothing"))
    arms = [o for o in new if o.type == "ARMATURE"]
    arm = max(arms, key=lambda a: len(a.data.bones)) if arms else None

    try:
        _normalize(context, new, meshes, facing, target_h)
    except Exception:
        notes.append("size/position was not adjusted")

    if arm is not None:
        if not _is_skinned(meshes):
            how = _skin_to(context, meshes, arm)
            notes.append(f"mesh was not attached to the skeleton - skinned automatically ({how} weights)")
        else:
            loose = _unskinned(meshes)
            if loose:
                _blended_weights(loose, arm)
                notes.append(f"{len(loose)} loose mesh part(s) attached to the nearest bones")
        ok, msg = _check_tpose(context, arm)
        if not ok:
            notes.append(msg)
        return arm, notes

    try:
        arm, how = _rig_meshes(context, meshes, pose, a_angle)
        notes.append(f"no skeleton - built a {pose}-pose skeleton and skinned the mesh ({how} weights)")
        fit = arm.get("cs_fit")
        if fit:
            notes.append(f"skeleton fitted to the body ({fit})")
        else:
            notes.append("body proportions not detected - average human proportions used "
                         "(if joints look off, rig the model in Mixamo and import it with skin)")
        return arm, notes
    except Exception as e:
        _log(_S(context), traceback.format_exc())
        notes.append(f"could not build a skeleton ({e})")
    root = _make_static_root(context, new, meshes)
    notes.append("kept as a one-piece character (placed, moved and filmed, but no limb animation)")
    return root, notes


# ---------------------------------------------------------------- registering / restoring
def _adopt(context, name, arm, names):
    """Put the character's objects into its own collection and name the rig."""
    coll = _get_collection(context.scene, "CS_Char_" + name)
    for n in names:
        o = bpy.data.objects.get(n)
        if o is None:
            continue
        for c in list(o.users_collection):
            if c != coll:
                c.objects.unlink(o)
        if o.name not in coll.objects:
            coll.objects.link(o)
    arm.name = f"CS_{name}_Rig"


def _backup_character(context, name):
    """Save the character to project/assets/characters/<name>_backup.blend so it can be restored."""
    root = _project_dir(_S(context))
    coll = bpy.data.collections.get("CS_Char_" + name)
    if not root or coll is None:
        return ""
    path = os.path.join(_ensure(os.path.join(root, "assets", "characters")), f"{name}_backup.blend")
    try:
        bpy.data.libraries.write(path, set(coll.objects), path_remap="ABSOLUTE")
    except Exception:
        _log(_S(context), "Backup failed for " + name + "\n" + traceback.format_exc())
        return ""
    return path


def _unique_char_name(s, base):
    name, n = base, 2
    while s.characters.get(name):
        name = _safe_name(f"{base}_{n}")
        n += 1
    return name


def _register_character(context, name, path, arm, names):
    s = _S(context)
    _adopt(context, name, arm, names)
    item = s.characters.add()
    item.name, item.filepath, item.armature_name = name, path, arm.name
    item.backup_path = _backup_character(context, name)
    s.char_index = len(s.characters) - 1
    _log(s, f"Character added: {name} ({arm.name})")


def restore_character(context, item):
    """Re-create a character that was deleted from the layout. Returns (ok, message)."""
    src = ""
    for cand in (item.backup_path, item.filepath):
        if cand and os.path.isfile(bpy.path.abspath(cand)):
            src = bpy.path.abspath(cand)
            break
    if not src:
        return False, f"{item.name}: no backup or source file found"
    coll = bpy.data.collections.get("CS_Char_" + item.name)
    if coll:
        _delete_objects(list(coll.objects))  # leftovers of a half-deleted character
    old = bpy.data.objects.get(item.armature_name)
    if old:
        _delete_objects(_char_objects(old))
    before = {o.name for o in bpy.data.objects}
    try:
        _import_file(context, src)
    except Exception as e:
        _delete_by_names([o.name for o in _new_objects(before)])
        return False, f"{item.name}: import failed ({e})"
    _delete_by_names([o.name for o in _new_objects(before) if o.type in {"CAMERA", "LIGHT"}])
    new = _new_objects(before)
    arm = _find_rig(new)
    if arm is None:
        _delete_by_names([o.name for o in new])
        return False, f"{item.name}: the saved file has no character in it"
    _adopt(context, item.name, arm, [o.name for o in new])
    item.armature_name = arm.name
    _log(_S(context), f"Character restored: {item.name} from {src}")
    return True, ""


def _rebuild_if_built(context):
    """Restored rigs have no animation yet - rebuild the episode if it was built before."""
    s = _S(context)
    if len(s.shots) and s.shots[0].end_frame > 0:
        try:
            build_episode(context)
            return True
        except Exception:
            _log(s, traceback.format_exc())
    return False


def add_character_file(context, path, name, facing="NEG_Y", pose="T", a_angle=40.0):
    """Import one file as a character. Returns (ok, message). Accepts ANY model."""
    s = _S(context)
    before = {o.name for o in bpy.data.objects}
    try:
        _import_file(context, path)
    except Exception as e:
        _delete_by_names([o.name for o in _new_objects(before)])
        return False, f"{os.path.basename(path)}: import failed ({e})"
    new = _new_objects(before)
    _delete_by_names([o.name for o in new if o.type in {"CAMERA", "LIGHT"}])
    new = _new_objects(before)
    try:
        arm, notes = _prepare_character(context, new, facing, s.char_height, pose, a_angle)
    except Exception as e:
        _log(s, traceback.format_exc())
        _delete_by_names([o.name for o in _new_objects(before)])
        return False, f"{os.path.basename(path)}: {e}"
    keep = [o.name for o in _new_objects(before)]
    name = _unique_char_name(s, name)
    _register_character(context, name, path, arm, keep)
    for n in notes:
        _log(s, f"Character {name}: {n}")
    return True, name + (" - " + "; ".join(notes) if notes else "")


class CARTOON_OT_char_add(Operator):
    bl_idname = "cartoon.char_add"
    bl_label = "Add Characters"
    bl_description = ("Import one or many characters at once: fbx, glb, gltf, obj, dae, usd, blend or a zip of these. "
                      "Any model is accepted - with or without skeleton, in any pose")

    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=OperatorFileListElement)
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(
        default="*.fbx;*.glb;*.gltf;*.obj;*.dae;*.stl;*.ply;*.usd;*.usda;*.usdc;*.blend;*.zip",
        options={"HIDDEN"})
    char_name: StringProperty(name="Name", description="Only used when a single file is chosen. Empty = file name")
    facing: EnumProperty(name="Model faces", items=FACING_ITEMS, default="NEG_Y",
                         description="Which way the model looks when imported, so it can be turned to face the camera")
    rig_pose: EnumProperty(name="Pose (if no skeleton)", items=POSE_ITEMS, default="T",
                           description="Only used for models WITHOUT a skeleton: the pose the model is in, "
                                       "so the new skeleton fits it")
    a_angle: FloatProperty(name="A-pose arm angle", default=40.0, min=10.0, max=70.0,
                           description="How far the arms are angled down in an A-pose model (degrees)")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        paths = []
        if len(self.files) and self.directory:
            paths = [os.path.join(self.directory, f.name) for f in self.files]
        elif self.filepath:
            paths = [bpy.path.abspath(self.filepath)]
        paths = [p for p in paths if os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMPORT_EXTS]
        if not paths:
            self.report({"ERROR"}, "Choose a model file: " + ", ".join(IMPORT_EXTS))
            return {"CANCELLED"}
        added, problems, notes = [], [], []
        for path in paths:
            base = self.char_name if (len(paths) == 1 and self.char_name) else os.path.splitext(os.path.basename(path))[0]
            ok, msg = add_character_file(context, path, _safe_name(base), self.facing, self.rig_pose, self.a_angle)
            if ok:
                added.append(msg.split(" - ")[0])
                if " - " in msg:
                    notes.append(msg)
            else:
                problems.append(msg)
        if problems:
            self.report({"WARNING" if added else "ERROR"}, "; ".join(problems))
        if added:
            text = f"Added {len(added)} character(s): {', '.join(added)}"
            if notes:
                text += "  |  " + "  |  ".join(notes)
            self.report({"INFO"}, text)
            return {"FINISHED"}
        return {"CANCELLED"}


class CARTOON_OT_auto_rig(Operator):
    bl_idname = "cartoon.auto_rig"
    bl_label = "Add Skeleton To Selected Mesh"
    bl_description = ("Give the selected mesh object(s) a skeleton and add them as a character. "
                      "Choose whether the model is in a T-pose or an A-pose")

    char_name: StringProperty(name="Name", default="Hero")
    rig_pose: EnumProperty(name="Model pose", items=POSE_ITEMS, default="T")
    a_angle: FloatProperty(name="A-pose arm angle", default=40.0, min=10.0, max=70.0)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        s = _S(context)
        meshes = [o for o in context.selected_objects if o.type == "MESH"]
        name = _safe_name(self.char_name)
        if not meshes:
            self.report({"ERROR"}, "Select the character mesh object(s) first")
            return {"CANCELLED"}
        name = _unique_char_name(s, name)
        try:
            arm, method = _rig_meshes(context, meshes, self.rig_pose, self.a_angle)
        except Exception as e:
            _log(s, traceback.format_exc())
            self.report({"ERROR"}, f"Add skeleton failed: {e}")
            return {"CANCELLED"}
        _register_character(context, name, "", arm, [o.name for o in meshes] + [arm.name])
        self.report({"INFO"}, f"Skeleton ({self.rig_pose}-pose) added to '{name}' "
                              f"({method} weights)")
        return {"FINISHED"}



class CARTOON_OT_char_save_blend(Operator):
    bl_idname = "cartoon.char_save_blend"
    bl_label = "Save Rigged Character to Computer"
    bl_description = "Save the selected character, mesh, armature and weights as a standalone .blend file"

    filepath: StringProperty(subtype="FILE_PATH", default="character.blend")
    filter_glob: StringProperty(default="*.blend", options={"HIDDEN"})

    def invoke(self, context, event):
        s = _S(context)
        if not (0 <= s.char_index < len(s.characters)):
            self.report({"ERROR"}, "Select a character in the Character Library first")
            return {"CANCELLED"}
        item = s.characters[s.char_index]
        self.filepath = bpy.path.abspath("//") + _safe_name(item.name) + "_rigged.blend"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.char_index < len(s.characters)):
            self.report({"ERROR"}, "Select a character in the Character Library first")
            return {"CANCELLED"}
        item = s.characters[s.char_index]
        arm = bpy.data.objects.get(item.armature_name)
        if arm is None:
            self.report({"ERROR"}, "Character rig is missing. Restore it before saving.")
            return {"CANCELLED"}
        objs = _char_objects(arm)
        if arm not in objs:
            objs.append(arm)
        try:
            folder = os.path.dirname(bpy.path.abspath(self.filepath))
            if folder:
                os.makedirs(folder, exist_ok=True)
            bpy.data.libraries.write(bpy.path.abspath(self.filepath), set(objs), path_remap="RELATIVE")
        except Exception as e:
            self.report({"ERROR"}, "Could not save character: " + str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, "Saved rigged character: " + bpy.path.abspath(self.filepath))
        return {"FINISHED"}


class CARTOON_OT_char_clear_selection(Operator):
    bl_idname = "cartoon.char_clear_selection"
    bl_label = "Clear Character Selection"
    bl_description = "Clear the active Character Library row"

    def execute(self, context):
        _S(context).char_index = -1
        return {"FINISHED"}


class CARTOON_OT_char_remove(Operator):
    bl_idname = "cartoon.char_remove"
    bl_label = "Remove Character"
    bl_description = "Remove from the library (scene objects are kept)"

    def execute(self, context):
        s = _S(context)
        if 0 <= s.char_index < len(s.characters):
            s.characters.remove(s.char_index)
            s.char_index = max(0, s.char_index - 1)
        return {"FINISHED"}


class CARTOON_OT_char_restore(Operator):
    bl_idname = "cartoon.char_restore"
    bl_label = "Restore Selected"
    bl_description = "Put the selected character back in the layout if it was deleted from there"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.char_index < len(s.characters)):
            self.report({"ERROR"}, "Select a character in the list first")
            return {"CANCELLED"}
        item = s.characters[s.char_index]
        if not _char_missing(item, context.scene):
            self.report({"INFO"}, f"'{item.name}' is already in the layout")
            return {"FINISHED"}
        ok, msg = restore_character(context, item)
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        rebuilt = _rebuild_if_built(context)
        self.report({"INFO"}, f"Restored '{item.name}'" + (" and rebuilt the episode" if rebuilt else ""))
        return {"FINISHED"}


class CARTOON_OT_char_restore_all(Operator):
    bl_idname = "cartoon.char_restore_all"
    bl_label = "Restore All Missing"
    bl_description = "Put every character that was deleted from the layout back"

    def execute(self, context):
        s = _S(context)
        done, failed = [], []
        for item in s.characters:
            if _char_missing(item, context.scene):
                ok, msg = restore_character(context, item)
                (done if ok else failed).append(item.name if ok else msg)
        if not done and not failed:
            self.report({"INFO"}, "All characters are in the layout")
            return {"FINISHED"}
        rebuilt = _rebuild_if_built(context) if done else False
        text = f"Restored {len(done)} character(s)" + (" and rebuilt the episode" if rebuilt else "")
        if failed:
            self.report({"WARNING"}, text + "; problems: " + " | ".join(failed))
        else:
            self.report({"INFO"}, text)
        return {"FINISHED"}


def _read_markers():
    coll = bpy.data.collections.get(MARKER_COLL)
    found = {}
    if coll is not None:
        for o in coll.objects:
            mid = o.get("cs_marker")
            if mid:
                found[mid] = o
    return coll, found


class CARTOON_OT_marker_add(Operator):
    bl_idname = "cartoon.marker_add"
    bl_label = "1. Add Rig Markers"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = ("Select the character's mesh (arms out in a T-pose, facing the front), then press this. "
                      "Small circles appear on the body: drag each one onto its joint in the FRONT view, "
                      "then press 'Build Skeleton From Markers'")

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "Select the character mesh object(s) first")
            return {"CANCELLED"}
        tpl = _get_template(context)
        if tpl is None:
            self.report({"ERROR"}, "No skeleton shape known yet - import one Mixamo animation (.fbx) in the "
                                   "Animation Library first")
            return {"CANCELLED"}
        try:
            pts, height = _default_markers(meshes, tpl)
        except Exception as e:
            _log(_S(context), traceback.format_exc())
            self.report({"ERROR"}, f"Could not place markers: {e}")
            return {"CANCELLED"}
        coll = _get_collection(context.scene, MARKER_COLL)
        coll.hide_viewport = False
        _delete_objects(list(coll.objects))
        coll["cs_meshes"] = json.dumps([m.name for m in meshes])
        size = max(0.004, 0.018 * height)
        made = []
        for mid, label, color in _MARKER_DEFS:
            if mid not in pts:
                continue
            o = bpy.data.objects.new(label, None)
            o.empty_display_type = "SPHERE"
            o.empty_display_size = size
            o.show_name = True
            o.show_in_front = True
            o.color = color
            o.lock_location = (False, True, False)      # front view: left/right and up/down only
            o["cs_marker"] = mid
            o.location = pts[mid]
            coll.objects.link(o)
            made.append(o)
        _deselect_all(context)
        _select(context, made)
        try:
            bpy.ops.view3d.view_axis(type="FRONT")
        except Exception:
            pass
        self.report({"INFO"}, f"{len(made)} markers added. In the FRONT view drag each circle onto its joint "
                              "(G key or the move tool), then press 'Build Skeleton From Markers'")
        return {"FINISHED"}


class CARTOON_OT_marker_mirror(Operator):
    bl_idname = "cartoon.marker_mirror"
    bl_label = "Mirror Markers"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = "Copy the markers of one side onto the other side (mirrored across the body's middle)"

    source: EnumProperty(name="From", default="L",
                         items=[("L", "Left to right", ""), ("R", "Right to left", "")])

    def execute(self, context):
        context.view_layer.update()
        _coll, mk = _read_markers()
        centre = [mk[i].matrix_world.translation.x for i in ("chin", "neck", "pelvis", "groin") if i in mk]
        if not centre:
            self.report({"ERROR"}, "No markers found - press 'Add Rig Markers' first")
            return {"CANCELLED"}
        cx = sum(centre) / len(centre)
        other = "R" if self.source == "L" else "L"
        n = 0
        for kind in ("shoulder", "elbow", "wrist", "knee", "ankle"):
            a, b = mk.get(f"{kind}_{self.source}"), mk.get(f"{kind}_{other}")
            if a is None or b is None:
                continue
            pa = a.matrix_world.translation
            b.location = (2 * cx - pa.x, b.location.y, pa.z)
            n += 1
        self.report({"INFO"}, f"Mirrored {n} markers")
        return {"FINISHED"}


class CARTOON_OT_rig_from_markers(Operator):
    bl_idname = "cartoon.rig_from_markers"
    bl_label = "2. Build Skeleton From Markers"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = ("Build the skeleton so its joints sit exactly on your markers, attach the mesh to it "
                      "and add the character to the library")

    char_name: StringProperty(name="Name", default="Hero")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        s = _S(context)
        context.view_layer.update()
        coll, mk = _read_markers()
        if coll is None or not mk:
            self.report({"ERROR"}, "No markers found - press 'Add Rig Markers' first")
            return {"CANCELLED"}
        try:
            names = json.loads(coll.get("cs_meshes", "[]"))
        except Exception:
            names = []
        meshes = [bpy.data.objects[n] for n in names if n in bpy.data.objects and bpy.data.objects[n].type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "The character mesh is gone - delete the markers and start again")
            return {"CANCELLED"}
        tpl = _get_template(context)
        if tpl is None:
            self.report({"ERROR"}, "No skeleton shape known yet - import one Mixamo animation (.fbx) first")
            return {"CANCELLED"}
        positions = {i: o.matrix_world.translation.copy() for i, o in mk.items()}
        for m in meshes:                       # clear any older skin so it cannot fight the new one
            for mod in [x for x in m.modifiers if x.type == "ARMATURE"]:
                m.modifiers.remove(mod)
            m.vertex_groups.clear()
            if m.parent is not None and m.parent.type == "ARMATURE":
                mw = m.matrix_world.copy()
                m.parent = None
                m.matrix_world = mw
        name = _unique_char_name(s, _safe_name(self.char_name))
        try:
            arm, _pose = _build_skeleton(context, meshes, tpl, "T", 40.0, positions)
            how = _skin_to(context, meshes, arm)
        except Exception as e:
            _log(s, traceback.format_exc())
            self.report({"ERROR"}, f"Build failed: {e}")
            return {"CANCELLED"}
        _register_character(context, name, "", arm, [o.name for o in meshes] + [arm.name])
        coll.hide_viewport = True
        self.report({"INFO"}, f"Skeleton built on your markers for '{name}' ({how} weights). "
                              "Markers are hidden - press 'Add Rig Markers' again to redo it")
        return {"FINISHED"}


classes = (
    CARTOON_OT_char_add,
    CARTOON_OT_auto_rig,
    CARTOON_OT_marker_add,
    CARTOON_OT_marker_mirror,
    CARTOON_OT_rig_from_markers,
    CARTOON_OT_char_remove,
    CARTOON_OT_char_save_blend,
    CARTOON_OT_char_clear_selection,
    CARTOON_OT_char_restore,
    CARTOON_OT_char_restore_all,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
