"""Render queue: render shots one by one, resume after failure."""
import bpy
import os
import glob
import traceback
from bpy.props import EnumProperty, BoolProperty
from bpy.types import Operator

from .helpers import (_S, _project_dir, _ensure, _safe_name, _log,
                      _cam_name, _set_engine, _find_movie, _shot_sig, _render_current)


def _shot_done(sh, mode):
    return getattr(sh, "status_" + mode) == "DONE" and os.path.isfile(getattr(sh, mode + "_path"))


def _render_shot(context, sh, mode):
    scene = context.scene
    s = _S(context)
    r = scene.render
    preview = mode == "preview"
    root = _project_dir(s)
    out_dir = _ensure(os.path.join(root, "renders", mode, _safe_name(sh.name)))
    for old in glob.glob(os.path.join(out_dir, "*.mp4")):
        try:
            os.remove(old)
        except OSError:
            pass
    backup = (scene.frame_start, scene.frame_end, r.filepath, r.resolution_percentage,
              r.image_settings.file_format, scene.camera, r.engine)
    ok = False
    try:
        cam = bpy.data.objects.get(_cam_name(sh))
        if cam is None:
            # Multi-angle shots create suffixed cameras and bind them to timeline
            # markers. Start the render with the first camera in this shot so
            # Blender's marker cuts can switch angles during the animation.
            cam = next((marker.camera for marker in sorted(
                scene.timeline_markers,
                key=lambda marker: marker.frame)
                if sh.start_frame <= marker.frame <= sh.end_frame and marker.camera), None)
        if cam is None:
            raise RuntimeError("camera missing - build the episode first")
        scene.camera = cam
        scene.frame_start, scene.frame_end = sh.start_frame, sh.end_frame
        r.filepath = os.path.join(out_dir, "shot_")
        r.image_settings.file_format = "FFMPEG"
        r.ffmpeg.format = "MPEG4"
        r.ffmpeg.codec = "H264"
        r.ffmpeg.constant_rate_factor = "MEDIUM" if preview else "HIGH"
        r.ffmpeg.audio_codec = "NONE"
        r.resolution_percentage = s.preview_scale if preview else 100
        _set_engine(scene, s.preview_samples if preview else s.final_samples)
        bpy.ops.render.render(animation=True)
        mp4 = _find_movie(out_dir)
        if not mp4:
            raise RuntimeError("renderer produced no movie file")
        dest = os.path.join(root, "renders", mode, _safe_name(sh.name) + ".mp4")
        try:
            os.replace(mp4, dest)
        except PermissionError:
            # Windows may keep an older preview open in a player. Keep the new
            # render and point the shot at it instead of marking the render failed.
            stem, ext = os.path.splitext(dest)
            dest = stem + "_latest" + ext
            os.replace(mp4, dest)
        setattr(sh, mode + "_path", dest)
        setattr(sh, "status_" + mode, "DONE")
        setattr(sh, "sig_" + mode, _shot_sig(s, sh, mode))
        _log(s, f"Rendered {mode} {sh.name} -> {dest}")
        ok = True
    except Exception:
        setattr(sh, "status_" + mode, "FAILED")
        _log(s, f"FAILED {mode} {sh.name}\n{traceback.format_exc()}")
    finally:
        (scene.frame_start, scene.frame_end, r.filepath, r.resolution_percentage,
         r.image_settings.file_format, scene.camera, r.engine) = backup
    return ok


class CARTOON_OT_render_queue(Operator):
    bl_idname = "cartoon.render_queue"
    bl_label = "Render Queue"
    bl_description = "Render pending shots one by one (ESC cancels between shots; finished shots are skipped on resume)"

    mode: EnumProperty(items=[("preview", "Preview", ""), ("final", "Final", "")], default="preview")
    redo: BoolProperty(name="Re-render finished shots", default=False)

    _timer = None
    _queue = None
    _total = 0

    def invoke(self, context, event):
        s = _S(context)
        if not _project_dir(s):
            self.report({"ERROR"}, "Set the Project Folder first")
            return {"CANCELLED"}
        if not len(s.shots) or s.shots[0].end_frame <= 0:
            self.report({"ERROR"}, "Build the episode first")
            return {"CANCELLED"}
        self._queue = [i for i, sh in enumerate(s.shots) if self.redo or not _render_current(s, sh, self.mode)]
        self._total = len(self._queue)
        if not self._queue:
            self.report({"INFO"}, "Nothing to render - all shots are done")
            return {"FINISHED"}
        wm = context.window_manager
        wm.progress_begin(0, self._total)
        self._timer = wm.event_timer_add(0.3, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()

    def modal(self, context, event):
        if event.type == "ESC":
            self._finish(context)
            self.report({"WARNING"}, "Render queue cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        s = _S(context)
        if not self._queue:
            self._finish(context)
            failed = sum(1 for sh in s.shots if getattr(sh, "status_" + self.mode) == "FAILED")
            self.report({"WARNING" if failed else "INFO"}, f"Queue finished, {failed} failed shot(s)")
            return {"FINISHED"}
        idx = self._queue.pop(0)
        _render_shot(context, s.shots[idx], self.mode)
        context.window_manager.progress_update(self._total - len(self._queue))
        if bpy.data.is_saved:
            try:
                bpy.ops.wm.save_mainfile()
            except Exception:
                pass
        return {"RUNNING_MODAL"}


classes = (
    CARTOON_OT_render_queue,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
