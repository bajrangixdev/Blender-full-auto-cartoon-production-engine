"""Animation library: import Mixamo actions and register existing ones."""
import bpy
import os
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator, OperatorFileListElement

from .helpers import _S, _safe_name, _log, _new_objects, _delete_objects, _fcurves


class CARTOON_OT_anim_add(Operator):
    bl_idname = "cartoon.anim_add"
    bl_label = "Add Animations"
    bl_description = ("Import one or more animation files (Mixamo .fbx, or .glb/.gltf) and store their actions. "
                      "For walking and running download from Mixamo with 'In Place' ticked")

    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=OperatorFileListElement)
    filter_glob: StringProperty(default="*.fbx;*.glb;*.gltf", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        s = _S(context)
        added = 0
        for f in self.files:
            path = os.path.join(self.directory, f.name)
            name = _safe_name(os.path.splitext(f.name)[0])
            if s.animations.get(name):
                continue
            before = {o.name for o in bpy.data.objects}
            # FBX animation exports often include an unused preview mesh and its
            # textures. Keep a snapshot so those temporary datablocks can be
            # removed after we extract the action.
            data_before = {
                kind: {block.as_pointer() for block in getattr(bpy.data, kind)}
                for kind in ("meshes", "armatures", "cameras", "lights", "curves", "materials", "images")
            }
            try:
                if os.path.splitext(path)[1].lower() in (".glb", ".gltf"):
                    bpy.ops.import_scene.gltf(filepath=path)
                else:
                    bpy.ops.import_scene.fbx(filepath=path)
            except Exception as e:
                _log(s, f"Animation import failed {f.name}: {e}")
                continue
            new = _new_objects(before)
            act = None
            for o in new:
                if o.type == "ARMATURE" and o.animation_data and o.animation_data.action:
                    act = o.animation_data.action
                    break
            if act is None:
                _delete_objects(new)
                _log(s, f"No animation found in {f.name}")
                continue
            # ---- NEW (step 6): remember the skeleton shape for characters that have none
            if bpy.data.texts.get("CS_RigTemplate") is None:
                skel = next((o for o in new if o.type == "ARMATURE"), None)
                if skel is not None:
                    try:
                        from .characters import store_rig_template
                        store_rig_template(skel)
                    except Exception as e:
                        _log(s, f"Could not remember skeleton from {f.name}: {e}")
            # ---- end of new part
            act.name = name
            act.use_fake_user = True
            for o in new:
                if o.animation_data:
                    o.animation_data.action = None
            _delete_objects(new)
            for kind in ("meshes", "armatures", "cameras", "lights", "curves", "materials", "images"):
                datablocks = getattr(bpy.data, kind)
                for block in list(datablocks):
                    if block.as_pointer() not in data_before[kind] and block.users == 0:
                        datablocks.remove(block)
            item = s.animations.add()
            item.name, item.filepath, item.action_name = name, path, act.name
            added += 1
        self.report({"INFO"}, f"Added {added} animation(s)")
        return {"FINISHED"}


class CARTOON_OT_anim_register_existing(Operator):
    bl_idname = "cartoon.anim_register_existing"
    bl_label = "Register Actions in This File"

    def execute(self, context):
        s = _S(context)
        known = {a.action_name for a in s.animations}
        n = 0
        for act in bpy.data.actions:
            if act.name.startswith("CS_") or act.name in known or ".blend" in act.name:
                continue
            if any(fc.data_path.startswith("pose.bones") for fc in _fcurves(act)):
                act.use_fake_user = True
                item = s.animations.add()
                item.name = _safe_name(act.name)
                item.action_name = act.name
                n += 1
        # remember the skeleton shape (needed to give rig-less characters a skeleton)
        if bpy.data.texts.get("CS_RigTemplate") is None:
            skels = [o for o in bpy.data.objects if o.type == "ARMATURE" and len(o.data.bones) >= 15]
            if skels:
                try:
                    from .characters import store_rig_template
                    store_rig_template(max(skels, key=lambda o: len(o.data.bones)))
                except Exception as e:
                    _log(s, f"Could not remember skeleton: {e}")
        self.report({"INFO"}, f"Registered {n} action(s)")
        return {"FINISHED"}


class CARTOON_OT_anim_remove(Operator):
    bl_idname = "cartoon.anim_remove"
    bl_label = "Remove Animation"

    def execute(self, context):
        s = _S(context)
        if 0 <= s.anim_index < len(s.animations):
            s.animations.remove(s.anim_index)
            s.anim_index = max(0, s.anim_index - 1)
        return {"FINISHED"}


class CARTOON_OT_anim_clear_selection(Operator):
    bl_idname = "cartoon.anim_clear_selection"
    bl_label = "Clear Animation Selection"
    bl_description = "Clear the active Animation Library row"

    def execute(self, context):
        _S(context).anim_index = -1
        return {"FINISHED"}


classes = (
    CARTOON_OT_anim_add,
    CARTOON_OT_anim_register_existing,
    CARTOON_OT_anim_remove,
    CARTOON_OT_anim_clear_selection,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
