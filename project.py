"""Project folders and saving/loading project data (JSON)."""
import bpy
import os
import json
from bpy.types import Operator

from .helpers import _S, _project_dir, _ensure, _log, _safe_name, _pg_to_dict, _dict_to_pg


class CARTOON_OT_project_create(Operator):
    bl_idname = "cartoon.project_create"
    bl_label = "Create Project Folders"

    def execute(self, context):
        s = _S(context)
        root = _project_dir(s)
        if not root:
            self.report({"ERROR"}, "Set the Project Folder first")
            return {"CANCELLED"}
        for sub in ("assets/characters", "assets/animations", "audio", "renders/preview",
                    "renders/final", "logs", "data"):
            _ensure(os.path.join(root, sub))
        _log(s, "Project folders created")
        self.report({"INFO"}, "Project folders ready")
        return {"FINISHED"}


class CARTOON_OT_save_json(Operator):
    bl_idname = "cartoon.save_json"
    bl_label = "Save Project Data"
    bl_description = "Write characters, animations and shots to data/episode.json"

    def execute(self, context):
        s = _S(context)
        root = _project_dir(s)
        if not root:
            self.report({"ERROR"}, "Set the Project Folder first")
            return {"CANCELLED"}
        data = {
            "characters": [_pg_to_dict(c) for c in s.characters],
            "animations": [_pg_to_dict(a) for a in s.animations],
            "shots": [_pg_to_dict(x) for x in s.shots],
        }
        path = os.path.join(_ensure(os.path.join(root, "data")), f"{_safe_name(s.episode_name)}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        _log(s, f"Saved {path}")
        self.report({"INFO"}, f"Saved {os.path.basename(path)}")
        return {"FINISHED"}


class CARTOON_OT_load_json(Operator):
    bl_idname = "cartoon.load_json"
    bl_label = "Load Project Data"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = _S(context)
        root = _project_dir(s)
        path = os.path.join(root, "data", f"{_safe_name(s.episode_name)}.json")
        if not os.path.isfile(path):
            self.report({"ERROR"}, "No saved data for this episode")
            return {"CANCELLED"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for coll, key in ((s.characters, "characters"), (s.animations, "animations"), (s.shots, "shots")):
            coll.clear()
            for d in data.get(key, []):
                _dict_to_pg(coll.add(), d)
        self.report({"INFO"}, "Project data loaded")
        return {"FINISHED"}


classes = (
    CARTOON_OT_project_create,
    CARTOON_OT_save_json,
    CARTOON_OT_load_json,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
