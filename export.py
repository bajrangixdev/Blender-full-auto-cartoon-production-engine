"""Episode export: assemble shots, voice, music and subtitles into an MP4."""
import bpy
import os
import glob
import traceback
from bpy.props import BoolProperty
from bpy.types import Operator

from .helpers import (_S, _project_dir, _ensure, _safe_name, _log,
                      _seqs, _srt_time, _find_movie)
from .render_queue import _shot_done


class CARTOON_OT_export_episode(Operator):
    bl_idname = "cartoon.export_episode"
    bl_label = "Assemble Episode"
    bl_description = "Join rendered shots, voice, music and subtitles in a 'CS_Assembly' scene"

    render_now: BoolProperty(default=False)

    def execute(self, context):
        src = context.scene
        if src.name == "CS_Assembly":  # settings live on the main scene
            others = [sc for sc in bpy.data.scenes if sc.name != "CS_Assembly"]
            src = next((sc for sc in others if sc.cartoon_studio.project_dir),
                       others[0] if others else src)
        s = src.cartoon_studio
        root = _project_dir(s)
        if not root:
            self.report({"ERROR"}, "Set the Project Folder first")
            return {"CANCELLED"}
        ep = _safe_name(s.episode_name)
        fps = s.fps

        old = bpy.data.scenes.get("CS_Assembly")
        if old:
            if context.window and context.window.scene == old:
                context.window.scene = src
            bpy.data.scenes.remove(old)
        asm = bpy.data.scenes.new("CS_Assembly")
        asm.render.fps = fps
        asm.render.resolution_x, asm.render.resolution_y = s.res_x, s.res_y
        asm.render.resolution_percentage = 100
        asm.sequence_editor_create()
        seqs = _seqs(asm.sequence_editor)

        font = None
        if s.subtitle_font and os.path.isfile(bpy.path.abspath(s.subtitle_font)):
            try:
                font = bpy.data.fonts.load(bpy.path.abspath(s.subtitle_font))
            except Exception:
                font = None

        srt, missing, last = [], [], 1
        rendered = []
        for sh in s.shots:
            path = sh.final_path if _shot_done(sh, "final") else (sh.preview_path if _shot_done(sh, "preview") else "")
            if path:
                rendered.append((sh, path))
            else:
                missing.append(sh.name)

        cursor = 1
        previous_strip = None
        previous_shot = None
        for i, (sh, path) in enumerate(rendered):
            overlap = 0
            if previous_shot is not None and previous_shot.transition == "CROSS":
                overlap = min(previous_shot.transition_frames, max(1, previous_shot.duration - 1),
                              max(1, sh.duration - 1))
            start = max(1, cursor - overlap)
            video = seqs.new_movie(f"{sh.name}_video", path, channel=1 + (i % 2), frame_start=start)
            end = start + sh.duration
            if previous_strip is not None and overlap:
                seqs.new_effect(f"{previous_shot.name}_to_{sh.name}_crossfade", "CROSS", channel=3,
                                seq1=previous_strip, seq2=video, frame_start=start, frame_end=start + overlap)
            if sh.voice_file and os.path.isfile(bpy.path.abspath(sh.voice_file)):
                seqs.new_sound(f"{sh.name}_voice", bpy.path.abspath(sh.voice_file), channel=4,
                               frame_start=start)
            if sh.dialogue:
                srt.append(f"{len(srt) + 1}\n{_srt_time(start - 1, fps)} --> "
                           f"{_srt_time(end - 1, fps)}\n{sh.dialogue}\n")
                if s.burn_subtitles:
                    try:
                        t = seqs.new_effect(f"{sh.name}_sub", "TEXT", channel=5,
                                            frame_start=start, frame_end=end)
                        t.text = sh.dialogue
                        t.font_size = int(s.res_y * 0.045)
                        t.location = (0.5, 0.08)
                        t.use_box = True
                        t.box_color = (0, 0, 0, 0.5)
                        t.color = (1, 1, 1, 1)
                        try:
                            t.wrap_width = 0.9
                        except Exception:
                            pass
                        if font:
                            t.font = font
                    except Exception as e:
                        _log(s, f"Subtitle strip failed: {e}")
            cursor = end
            last = max(last, end - 1)
            previous_strip, previous_shot = video, sh

        if s.voice_file and os.path.isfile(bpy.path.abspath(s.voice_file)):
            seqs.new_sound("Narration", bpy.path.abspath(s.voice_file), channel=6, frame_start=1)
        if s.music_path and os.path.isfile(bpy.path.abspath(s.music_path)):
            m = seqs.new_sound("Music", bpy.path.abspath(s.music_path), channel=7, frame_start=1)
            m.volume = 0.25

        asm.frame_start, asm.frame_end = 1, max(1, last)
        final_dir = _ensure(os.path.join(root, "renders", "final"))
        if srt:
            with open(os.path.join(final_dir, ep + ".srt"), "w", encoding="utf-8") as f:
                f.write("\n".join(srt))

        if missing:
            self.report({"WARNING"}, "Shots without renders: " + ", ".join(missing))
        if not self.render_now:
            self.report({"INFO"}, "Assembly scene 'CS_Assembly' created - switch to it to review the edit")
            return {"FINISHED"}

        tmp = _ensure(os.path.join(final_dir, "_assembly_tmp"))
        for old_f in glob.glob(os.path.join(tmp, "*.mp4")):
            os.remove(old_f)
        r = asm.render
        r.filepath = os.path.join(tmp, "ep_")
        r.image_settings.file_format = "FFMPEG"
        r.ffmpeg.format = "MPEG4"
        r.ffmpeg.codec = "H264"
        r.ffmpeg.audio_codec = "AAC"
        r.ffmpeg.constant_rate_factor = "HIGH"
        try:
            bpy.ops.render.render(animation=True, scene=asm.name)
        except Exception as e:
            _log(s, traceback.format_exc())
            self.report({"ERROR"}, f"Export failed: {e}")
            return {"CANCELLED"}
        mp4 = _find_movie(tmp)
        if not mp4:
            self.report({"ERROR"}, "Export produced no file")
            return {"CANCELLED"}
        dest = os.path.join(final_dir, ep + ".mp4")
        os.replace(mp4, dest)
        _log(s, f"Episode exported: {dest}")
        self.report({"INFO"}, f"Exported {dest}")
        return {"FINISHED"}


classes = (
    CARTOON_OT_export_episode,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
