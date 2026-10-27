bl_info = {
    "name": "Cartoon Studio Pro Advanced",
    "author": "Cartoon Studio",
    "version": (1, 7, 7),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Cartoon Studio",
    "description": "AI-assisted cartoon production, world building, cameras, animation and VFX",
    "category": "Animation",
}

import os as _os

# Clear message if a file was forgotten, misnamed (e.g. project.py.txt) or left empty.
_REQUIRED = ("helpers", "camera_logic", "data", "project", "characters", "animations", "shots",
             "rigfit", "director", "checks", "render_queue", "export", "auto_produce", "ui")
_folder = _os.path.dirname(__file__)
_problems = []
for _n in _REQUIRED:
    _p = _os.path.join(_folder, _n + ".py")
    if not _os.path.isfile(_p):
        _problems.append(_n + ".py is MISSING")
    elif _os.path.getsize(_p) == 0:
        _problems.append(_n + ".py is EMPTY")
if _problems:
    raise ImportError("Cartoon Studio folder problem: " + "; ".join(_problems) +
                      "  | folder: " + _folder)

from . import (data, project, characters, animations, shots, director,
               checks, render_queue, export, auto_produce, ui, advanced_tools, rbc_vehicle)

# data FIRST (settings must exist), ui LAST
modules = (data, project, characters, animations, shots, director,
           checks, render_queue, export, auto_produce, ui, advanced_tools)


_rbc_registered = False


def register():
    global _rbc_registered
    for m in modules:
        m.register()
    import bpy
    if not hasattr(bpy.types.Scene, "sna_rbc_rig_collection"):
        try:
            rbc_vehicle.register()
            _rbc_registered = True
        except Exception as exc:
            print("[CartoonStudio] Bundled RBC registration failed:", exc)


def unregister():
    global _rbc_registered
    if _rbc_registered:
        try:
            rbc_vehicle.unregister()
        except Exception as exc:
            print("[CartoonStudio] Bundled RBC cleanup failed:", exc)
        _rbc_registered = False
    for m in reversed(modules):
        m.unregister()
