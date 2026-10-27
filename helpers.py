"""Shared helper functions used by every module."""
import bpy
import os
import re
import glob
import time
import unicodedata
from mathutils import Vector

GEN_COLL = "CS_Generated"


def _S(context):
    return context.scene.cartoon_studio


def _project_dir(s):
    return bpy.path.abspath(s.project_dir) if s.project_dir else ""


def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name):
    """Keep letters/digits of ANY script (Hindi included, with its vowel signs).
    Blender names are limited to 63 bytes, so cap at 45 bytes to leave room for prefixes."""
    out = []
    for ch in (name or ""):
        if ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M"):
            out.append(ch)
        else:
            out.append("_")
    s = re.sub(r"_+", "_", "".join(out)).strip("_")
    while len(s.encode("utf-8")) > 45:
        s = s[:-1]
    return s.strip("_") or "Item"


def _next_shot_name(s):
    """First S001, S002... that no existing shot uses."""
    used = {_safe_name(x.name) for x in s.shots}
    n = 1
    while _safe_name(f"S{n:03d}") in used:
        n += 1
    return f"S{n:03d}"


def _ensure_unique_shot_names(s):
    """Rename shots whose (safe) names collide. Returns [(old, new)]."""
    changed = []
    keys = [_safe_name(x.name) for x in s.shots]
    seen = set()
    for i, sh in enumerate(s.shots):
        if keys[i] in seen:
            later = set(keys[i + 1:])
            n = 2
            while True:
                cand = f"{sh.name}_{n}"
                ck = _safe_name(cand)
                if ck not in seen and ck not in later:
                    break
                n += 1
            changed.append((sh.name, cand))
            sh.name = cand
            keys[i] = ck
        seen.add(keys[i])
    return changed


_SIG_FIELDS = ("duration", "camera_type", "light_type", "auto_move", "move_dist", "face_each_other")


def _shot_index(s, sh):
    ptr = sh.as_pointer()
    for i, x in enumerate(s.shots):
        if x.as_pointer() == ptr:
            return i
    return -1


def _shot_cast(sh):
    """Everybody in a shot, in order: Character A, Character B, then any extra characters."""
    cast = []
    if sh.char_a:
        cast.append({"slot": "a", "name": sh.char_a, "action": sh.action_a, "cues": sh.actions_a,
                     "x": sh.char_a_x, "y": sh.char_a_y})
    if sh.char_b:
        cast.append({"slot": "b", "name": sh.char_b, "action": sh.action_b, "cues": sh.actions_b,
                     "x": sh.char_b_x, "y": sh.char_b_y})
    for i, e in enumerate(sh.extras):
        if e.char:
            cast.append({"slot": f"x{i}", "name": e.char, "action": e.action, "cues": e.actions,
                         "x": e.x, "y": e.y})
    return cast


def _spread_x(n, gap=3.0):
    """X positions for n characters standing in a row, centred on 0 (two characters = -1.5 and +1.5)."""
    return [round((i - (n - 1) / 2.0) * gap, 3) for i in range(n)]


def _shot_ranges(s):
    """[(start, end)] for every shot, computed live from the durations: shot 1 = 1-100, shot 2 = 101-200 ..."""
    out, frame = [], 1
    for sh in s.shots:
        end = frame + max(1, sh.duration) - 1
        out.append((frame, end))
        frame = end + 1
    return out


def _shot_sig(s, sh, mode):
    """Fingerprint of everything that changes how a shot looks when rendered."""
    # Bump the camera planner tag when its framing logic changes, so a completed
    # render from an older camera plan cannot be mistaken for a current render.
    parts = ["camera-plan-v4"] + [str(getattr(sh, f)) for f in _SIG_FIELDS]
    parts += [f"{c['name']}:{c['action']}:{c['x']:.3f}:{c['y']:.3f}:" + ",".join(
        f"{q.action}@{q.start_frame}-{q.end_frame}" for q in c.get("cues", [])) for c in _shot_cast(sh)]
    idx = _shot_index(s, sh)
    parts += [str(len(sh.dialogue.split())), str(idx), str(idx == len(s.shots) - 1)]
    parts += [str(s.res_x), str(s.res_y), str(s.fps)]
    parts += [str(s.preview_scale), str(s.preview_samples)] if mode == "preview" else [str(s.final_samples)]
    return "|".join(parts)


def _render_current(s, sh, mode):
    """True only if the shot is rendered AND nothing about it changed since."""
    done = getattr(sh, "status_" + mode) == "DONE" and os.path.isfile(getattr(sh, mode + "_path"))
    return done and getattr(sh, "sig_" + mode) == _shot_sig(s, sh, mode)


def _log(s, msg):
    print("[CartoonStudio]", msg)
    root = _project_dir(s)
    if root and os.path.isdir(root):
        try:
            logdir = _ensure(os.path.join(root, "logs"))
            with open(os.path.join(logdir, "studio.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except OSError:
            pass


def _split_story(text):
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    sentences = re.split(r'(?<=[\u0964\u0965])\s*|(?<=[.!?])\s+(?=[A-Z0-9"\u201c])', text)
    sentences = [x.strip() for x in sentences if x.strip()]
    if len(sentences) > 1:
        return [" ".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2)]
    words = text.split()
    return [" ".join(words[i:i + 35]) for i in range(0, len(words), 35)]


def _get_collection(scene, name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if name not in scene.collection.children:
        scene.collection.children.link(coll)
    return coll


def _cam_name(shot):
    return f"CS_{_safe_name(shot.name)}_Cam"


def _is_root(o):
    """True for the Studio root empty that carries a character's position and turning."""
    return o is not None and o.type == "EMPTY" and bool(o.get("cs_root"))


def _char_top(arm):
    """Highest parent of the rig, NOT counting the Studio root empty above it."""
    top, guard = arm, 0
    while top.parent is not None and not _is_root(top.parent) and guard < 32:
        top, guard = top.parent, guard + 1
    return top


def _char_root(arm):
    """The Studio root empty above this character, or None."""
    top = _char_top(arm)
    return top.parent if _is_root(top.parent) else None


def _char_objects(arm):
    """Every object that belongs to the character (root, rig, meshes ...)."""
    top = _char_top(arm)
    objs = [top] + list(top.children_recursive)
    root = _char_root(arm)
    # include the root only when it carries this character alone
    if root is not None and all(ch is top for ch in root.children):
        objs.insert(0, root)
    if arm not in objs:
        objs.append(arm)
    return objs


def _world_bbox(objs):
    mn, mx = [1e18] * 3, [-1e18] * 3
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            for i in range(3):
                mn[i] = min(mn[i], w[i])
                mx[i] = max(mx[i], w[i])
    return mn, mx


def _fcurves(act):
    """F-curves of an action on every Blender version (old API and the 4.4+ layered actions)."""
    try:
        return list(act.fcurves)
    except Exception:
        pass
    out = []
    try:
        for layer in act.layers:
            for strip in layer.strips:
                for slot in act.slots:
                    bag = strip.channelbag(slot)
                    if bag is not None:
                        out.extend(bag.fcurves)
    except Exception:
        pass
    return out


def _canon_bone(name):
    """Bone name -> comparable form, so 'mixamorig:LeftArm', 'mixamorig1:LeftArm', 'LeftArm',
    'Arm.L' and 'arm_l' all meet in the middle."""
    n = (name or "").split(":")[-1]
    side = ""
    m = re.search(r"[._\- ]([lr])$", n, re.I)
    if m:
        side = "left" if m.group(1).lower() == "l" else "right"
        n = n[:m.start()]
    n = re.sub(r"[^a-z0-9]", "", n.lower())
    n = re.sub(r"^(mixamorig\d*|bip\d*|jbip[clr]?)", "", n)
    if n.startswith("left") or n.startswith("right"):
        return n
    return side + n


def _remove_data(d):
    try:
        if isinstance(d, bpy.types.Mesh):
            bpy.data.meshes.remove(d)
        elif isinstance(d, bpy.types.Armature):
            bpy.data.armatures.remove(d)
    except Exception:
        pass


def _delete_objects(objs):
    datas = [o.data for o in objs if o.data is not None]
    for o in objs:
        bpy.data.objects.remove(o, do_unlink=True)
    for d in datas:
        if d.users == 0:
            _remove_data(d)


def _new_objects(before):
    return [o for o in bpy.data.objects if o.name not in before]


def _key_visibility(objs, keys, viewport=True):
    """keys: {frame: visible_bool}"""
    for o in objs:
        for f, vis in sorted(keys.items()):
            o.hide_render = not vis
            o.keyframe_insert("hide_render", frame=f)
            if viewport:
                try:
                    o.hide_viewport = not vis
                    o.keyframe_insert("hide_viewport", frame=f)
                except Exception:
                    pass


def _action_bones(act):
    names = set()
    for fc in _fcurves(act):
        m = re.match(r'pose\.bones\["(.+?)"\]', fc.data_path)
        if m:
            names.add(m.group(1))
    return names


def _seqs(se):
    return getattr(se, "strips", None) or se.sequences


def _set_engine(scene, samples):
    for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = eng
            break
        except TypeError:
            continue
    try:
        scene.eevee.taa_render_samples = samples
    except Exception:
        pass


def _find_movie(folder):
    files = glob.glob(os.path.join(folder, "*.mp4"))
    return max(files, key=os.path.getmtime) if files else None


def _srt_time(frames, fps):
    ms = int(round(frames / fps * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _pg_to_dict(pg):
    out = {}
    for p in pg.bl_rna.properties:
        if p.identifier == "rna_type":
            continue
        val = getattr(pg, p.identifier)
        if p.type == "COLLECTION":
            val = [_pg_to_dict(x) for x in val]
        elif p.type == "POINTER":
            continue
        out[p.identifier] = val
    return out


def _dict_to_pg(pg, data):
    for k, v in data.items():
        try:
            cur = getattr(pg, k, None)
            if isinstance(v, list) and hasattr(cur, "add"):
                cur.clear()
                for d in v:
                    _dict_to_pg(cur.add(), d)
            else:
                setattr(pg, k, v)
        except Exception:
            pass


def _char_missing(item, scene):
    """True when the character's rig was deleted from the layout (it is still in the panel list)."""
    arm = bpy.data.objects.get(item.armature_name)
    return arm is None or arm.name not in scene.objects
