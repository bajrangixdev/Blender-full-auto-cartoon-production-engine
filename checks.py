"""Quality checks: find problems before rendering."""
import bpy
import os
from bpy.types import Operator
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

from .helpers import (_S, _project_dir, _cam_name, _action_bones, _safe_name, _render_current,
                      _shot_cast, _shot_ranges, _canon_bone)


def _cast_meshes(scene, character):
    settings = scene.cartoon_studio
    entry = settings.characters.get(character)
    arm = bpy.data.objects.get(entry.armature_name) if entry else None
    if arm is None or arm.name not in scene.objects:
        return []
    if arm.type == "MESH":
        return [] if arm.hide_render else [arm]
    return [obj for obj in arm.children_recursive if obj.type == "MESH" and not obj.hide_render]


def _camera_subject_status(scene, camera, characters, frame):
    """Check evaluated character bounds and ray visibility at a real scene frame."""
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    camera_eval = camera.evaluated_get(depsgraph)
    cast_objects = {obj.name for name in characters for obj in _cast_meshes(scene, name)}
    framed = False
    clear = False
    origin = camera_eval.matrix_world.translation
    for character in characters:
        for source in _cast_meshes(scene, character):
            obj = source.evaluated_get(depsgraph)
            try:
                corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
                projected = [world_to_camera_view(scene, camera_eval, point) for point in corners]
            except Exception:
                continue
            front = [p for p in projected if p.z > 0]
            if not front:
                continue
            min_x, max_x = min(p.x for p in front), max(p.x for p in front)
            min_y, max_y = min(p.y for p in front), max(p.y for p in front)
            if max_x >= 0.02 and min_x <= 0.98 and max_y >= 0.02 and min_y <= 0.98:
                framed = True
                target = sum(corners, Vector()) / max(1, len(corners))
                delta = target - origin
                distance = delta.length
                if distance <= 0.01:
                    clear = True
                    continue
                hit, _location, _normal, _face, hit_obj, _material = scene.ray_cast(
                    depsgraph, origin, delta.normalized(), distance=distance)
                if not hit or hit_obj is None or hit_obj.name in cast_objects:
                    clear = True
    if clear:
        return "CLEAR"
    return "BLOCKED" if framed else "MISS"


def _audit_camera_framing(scene, shot, f0, f1):
    """Return a warning if generated camera views miss every cast member."""
    characters = [entry["name"] for entry in _shot_cast(shot)]
    if not characters:
        return []
    marks = sorted((marker for marker in scene.timeline_markers
                    if f0 <= marker.frame <= f1 and marker.camera is not None),
                   key=lambda marker: marker.frame)
    if not marks:
        return [f"Shot {shot.name}: no camera marker is available for framing inspection"]
    issues = []
    for i, marker in enumerate(marks):
        seg_start = max(f0, marker.frame)
        seg_end = min(f1, marks[i + 1].frame - 1) if i + 1 < len(marks) else f1
        sample = (seg_start + seg_end) // 2
        status = _camera_subject_status(scene, marker.camera, characters, sample)
        if status == "MISS":
            issues.append(f"Shot {shot.name}: camera '{marker.camera.name}' may miss all characters near frame {sample}")
        elif status == "BLOCKED":
            issues.append(f"Shot {shot.name}: characters may be hidden behind scene geometry near frame {sample}")
    return issues


class CARTOON_OT_run_checks(Operator):
    bl_idname = "cartoon.run_checks"
    bl_label = "Run Quality Check"

    def execute(self, context):
        s = _S(context)
        out = []

        def add(level, msg):
            out.append((level, msg))

        root = _project_dir(s)
        if not root or not os.path.isdir(root):
            add("ERROR", "Project folder missing or not created")
        if not len(s.characters):
            add("ERROR", "Character library is empty")
        for c in s.characters:
            arm = bpy.data.objects.get(c.armature_name)
            if arm is None:
                add("ERROR", f"Character {c.name}: rig '{c.armature_name}' is not in the layout - press Restore in Character Library")
            if c.filepath and not os.path.isfile(c.filepath):
                add("WARNING", f"Character {c.name}: source file missing")
        for a in s.animations:
            if bpy.data.actions.get(a.action_name) is None:
                add("ERROR", f"Animation {a.name}: action '{a.action_name}' missing")
        for asset in s.assets:
            if not bpy.data.objects.get(asset.root_object):
                add("ERROR", f"Asset '{asset.name}': source object '{asset.root_object}' is missing")
        for fx in s.vfx_items:
            if not bpy.data.objects.get(fx.root_object):
                add("ERROR", f"VFX '{fx.name}': source object '{fx.root_object}' is missing")
        for library in bpy.data.libraries:
            path = bpy.path.abspath(library.filepath)
            if path and not os.path.isfile(path):
                add("ERROR", f"Linked Blender file missing: {library.filepath}")
        missing_images = {}
        for image in bpy.data.images:
            if image.users == 0:
                continue
            if image.source in {"GENERATED", "VIEWER"} or image.packed_file or getattr(image, "packed_files", None):
                continue
            path = bpy.path.abspath(image.filepath) if image.filepath else ""
            if path and not os.path.isfile(path):
                missing_images.setdefault(path, []).append(image.name)
        for path, names in missing_images.items():
            label = ", ".join(names[:3])
            if len(names) > 3:
                label += f" and {len(names) - 3} duplicate image datablock(s)"
            # A stale texture must be visible to the artist, but it should not
            # prevent creating a preview shot. The source remains actionable.
            add("WARNING", f"Texture source unavailable: {label} ({path})")
        if s.subtitle_font and not os.path.isfile(bpy.path.abspath(s.subtitle_font)):
            add("ERROR", "Subtitle font file missing")
        if not len(s.shots):
            add("ERROR", "No shots")
        _seen = set()
        for sh in s.shots:
            k = _safe_name(sh.name)
            if k in _seen:
                add("WARNING", f"Shot name '{sh.name}' is used twice (Build Episode will rename it)")
            _seen.add(k)
        ranges = _shot_ranges(s)
        old_frame = context.scene.frame_current
        camera_issues = []
        for sh, (f0, f1) in zip(s.shots, ranges):
            tag = f"Shot {sh.name} (frames {f0}-{f1})"
            if sh.duration < 6:
                add("ERROR", f"{tag}: duration too short")
            cast = _shot_cast(sh)
            if not cast:
                add("WARNING", f"{tag}: no character")
            names = [c["name"] for c in cast]
            for dup in {n for n in names if names.count(n) > 1}:
                add("WARNING", f"{tag}: '{dup}' appears twice - only the first is used")
            for c in cast:
                cname, aname = c["name"], c["action"]
                cd = s.characters.get(cname)
                if cd is None:
                    add("ERROR", f"{tag}: character '{cname}' not in library")
                    continue
                if not aname:
                    add("INFO", f"{tag}: {cname} has no action (static pose)")
                    continue
                lib = s.animations.get(aname)
                act = bpy.data.actions.get(lib.action_name if lib else aname)
                if act is None:
                    add("ERROR", f"{tag}: action '{aname}' missing")
                    continue
                arm = bpy.data.objects.get(cd.armature_name)
                if arm and arm.type == "ARMATURE":
                    used = _action_bones(act)
                    have = {b.name for b in arm.pose.bones}
                    canon = {_canon_bone(b) for b in have}
                    missing = {u for u in used if u not in have and _canon_bone(u) not in canon}
                    if missing:
                        total = len(used) or 1
                        lvl = "ERROR" if len(missing) / total > 0.25 else "WARNING"
                        add(lvl, f"{tag}: '{aname}' uses {len(missing)} bones not on {cname} (rig mismatch)")
                elif arm is not None:
                    add("INFO", f"{tag}: {cname} has no skeleton - it bobs as one piece")
            has_camera = bpy.data.objects.get(_cam_name(sh)) is not None or any(
                f0 <= marker.frame <= f1 and marker.camera is not None
                for marker in context.scene.timeline_markers)
            if not has_camera:
                add("WARNING", f"{tag}: no camera yet - press Build Episode")
            if sh.voice_file and not os.path.isfile(bpy.path.abspath(sh.voice_file)):
                add("ERROR", f"{tag}: voice file missing")
            # Catch contradictory per-character ranges before the builder has to trim them.
            for member in cast:
                cues = list(member.get("cues", []))
                previous_end = 0
                for cue in sorted(cues, key=lambda item: (item.start_frame, item.end_frame)):
                    if cue.start_frame <= previous_end:
                        add("WARNING", f"{tag}: overlapping action ranges for {member['name']}")
                    if cue.start_frame > sh.duration or cue.end_frame > sh.duration:
                        add("WARNING", f"{tag}: action range for {member['name']} is outside the shot")
                    previous_end = max(previous_end, cue.end_frame)
            for event in sh.vfx_events:
                if not event.effect or not any(fx.name == event.effect for fx in s.vfx_items):
                    add("ERROR", f"{tag}: timed VFX effect is missing")
                if event.frame > sh.duration:
                    add("WARNING", f"{tag}: VFX event '{event.effect}' is outside the shot")
            camera_issues.extend(_audit_camera_framing(context.scene, sh, f0, f1))
            for mode in ("preview", "final"):
                if getattr(sh, "status_" + mode) == "DONE" and not os.path.isfile(getattr(sh, mode + "_path")):
                    add("ERROR", f"{tag}: {mode} marked done but file is missing")
                elif getattr(sh, "status_" + mode) == "DONE" and not _render_current(s, sh, mode):
                    add("INFO", f"{tag}: {mode} render is out of date and will be re-rendered")
        if s.voice_file and not os.path.isfile(bpy.path.abspath(s.voice_file)):
            add("ERROR", "Narration file missing")
        if s.music_path and not os.path.isfile(bpy.path.abspath(s.music_path)):
            add("ERROR", "Music file missing")
        context.scene.frame_set(old_frame)
        for issue in camera_issues:
            add("WARNING", issue)
        expected_frames = sum(max(1, sh.duration) for sh in s.shots)
        if s.shots and context.scene.frame_end >= expected_frames:
            add("INFO", f"Timeline range covers the planned {expected_frames} frame(s)")
        elif s.shots:
            add("WARNING", f"Timeline ends at frame {context.scene.frame_end}; planned episode needs {expected_frames} frames")
        if not any(lv == "ERROR" for lv, _ in out):
            add("OK", "No blocking problems found")

        s.checks.clear()
        for level, msg in out:
            it = s.checks.add()
            it.name, it.level = msg, level
        errs = sum(1 for lv, _ in out if lv == "ERROR")
        self.report({"ERROR" if errs else "INFO"}, f"{errs} error(s)")
        return {"FINISHED"}


classes = (
    CARTOON_OT_run_checks,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
