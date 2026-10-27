"""Auto production: story to finished video in one click."""
import bpy
import traceback
from bpy.props import BoolProperty
from bpy.types import Operator

from .helpers import _S, _project_dir, _log, _render_current, _shot_cast
from .director import build_episode
from .render_queue import _render_shot


ACTION_KEYWORDS = [
    (("walk", "went", "came", "go to", "चल", "गया", "गई", "आया", "आई"), ("walk",)),
    (("run", "ran", "chase", "hurry", "दौड़", "भाग"), ("run",)),
    (("hello", "hi ", "welcome", "goodbye", "wave", "नमस्ते", "नमस्कार", "हैलो", "अलविदा", "स्वागत"), ("wav", "greet", "hello")),
    (("dance", "party", "celebrate", "नाच", "नृत्य", "जश्न", "खुशी"), ("danc",)),
    (("jump", "leap", "कूद"), ("jump",)),
    (("laugh", "funny", "haha", "हंस", "हँस", "मज़ा", "मजा"), ("laugh",)),
    (("sad", "cry", "sorry", "उदास", "रोया", "रोई", "रोने", "दुख", "माफ़"), ("sad", "cry")),
    (("angry", "shout", "yell", "गुस्सा", "क्रोध", "चिल्ला"), ("angry", "yell")),
    (("sit", "sat ", "बैठ"), ("sit",)),
]


def _find_anim(s, needles):
    for n in needles:
        for a in s.animations:
            if n in a.name.lower():
                return a.name
    return ""


def _auto_assign(s):
    """Pick actions and lighting for every shot from the dialogue text."""
    talk = _find_anim(s, ("talk", "speak"))
    idle = _find_anim(s, ("idle", "stand", "breath"))
    fallback = s.animations[0].name if len(s.animations) else ""
    for i, sh in enumerate(s.shots):
        text = " " + (sh.action_note + " " + sh.dialogue).lower() + " "
        chosen = ""
        for words, needles in ACTION_KEYWORDS:
            if any(w in text for w in words):
                chosen = _find_anim(s, needles)
                if chosen:
                    break
        main = chosen or talk or idle or fallback
        quiet = idle or talk or fallback
        cast = _shot_cast(sh)
        speaker = i % len(cast) if cast else 0          # deterministic turn-taking fallback
        if cast and sh.speaker_name:
            speaker = next((k for k, c in enumerate(cast) if c["name"] == sh.speaker_name), speaker)
        for k, c in enumerate(cast):
            pick = main if (k == speaker or len(cast) == 1) else quiet
            if c["action"]:
                continue
            slot = c["slot"]
            if slot == "a":
                sh.action_a = pick
            elif slot == "b":
                sh.action_b = pick
            else:
                sh.extras[int(slot[1:])].action = pick
        if sh.light_type == "DAY":
            if any(w in text for w in ("night", "dark", "moon", "midnight", "रात", "अंधेरा", "चाँद", "चांद")):
                sh.light_type = "NIGHT"
            elif any(w in text for w in ("sunset", "evening", "dusk", "शाम", "सूर्यास्त")):
                sh.light_type = "SUNSET"
            elif any(w in text for w in ("room", "house", "kitchen", "inside", "home", "घर", "कमरा", "रसोई", "अंदर")):
                sh.light_type = "WARM"


class CARTOON_OT_auto_produce(Operator):
    bl_idname = "cartoon.auto_produce"
    bl_label = "Auto Produce Episode"
    bl_description = "Story to finished MP4 in one click: shots, actions, build, checks, render, export"

    final: BoolProperty(name="Final quality", default=False)
    _timer = None
    _stage = "setup"
    _queue = None
    _total = 0
    _mode = "preview"

    def invoke(self, context, event):
        s = _S(context)
        if not _project_dir(s):
            self.report({"ERROR"}, "Set the Project Folder first")
            return {"CANCELLED"}
        if s.story_text is None and not len(s.shots):
            self.report({"ERROR"}, "Pick a story Text block first")
            return {"CANCELLED"}
        if not len(s.characters):
            self.report({"ERROR"}, "Add at least one character first")
            return {"CANCELLED"}
        self._stage = "setup"
        self._queue = None
        self._mode = "final" if self.final else "preview"
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _end(self, context, msg, level="INFO", result="FINISHED"):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        self.report({level}, msg)
        return {result}

    def modal(self, context, event):
        if event.type == "ESC":
            return self._end(context, "Auto production cancelled", "WARNING", "CANCELLED")
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        s = _S(context)
        st = self._stage
        try:
            if st == "setup":
                bpy.ops.cartoon.project_create()
                self._stage = "shots"
            elif st == "shots":
                if not len(s.shots):
                    if s.ollama_story_enabled:
                        try:
                            result = bpy.ops.cartoon.ai_story_breakdown("EXEC_DEFAULT")
                        except Exception as exc:
                            _log(s, f"WARN Ollama story breakdown unavailable ({exc}); using built-in story split")
                            result = {"CANCELLED"}
                        if ("CANCELLED" in result or not len(s.shots)) and s.story_text:
                            _log(s, "WARN Using built-in paragraph split because Ollama did not return shots")
                            bpy.ops.cartoon.story_to_shots("EXEC_DEFAULT")
                    else:
                        bpy.ops.cartoon.story_to_shots("EXEC_DEFAULT")
                if not len(s.shots):
                    return self._end(context, "Could not create shots from the story", "ERROR", "CANCELLED")
                self._stage = "assign"
            elif st == "assign":
                _auto_assign(s)
                self._stage = "build"
            elif st == "build":
                for w in build_episode(context):
                    _log(s, "WARN " + w)
                self._stage = "checks"
            elif st == "checks":
                bpy.ops.cartoon.run_checks()
                errors = [c.name for c in s.checks if c.level == "ERROR"]
                if errors:
                    return self._end(context, "Stopped: " + errors[0] + " (see Quality Check)",
                                     "ERROR", "CANCELLED")
                self._stage = "render"
            elif st == "render":
                wm = context.window_manager
                if self._queue is None:
                    self._queue = [i for i, sh in enumerate(s.shots) if not _render_current(s, sh, self._mode)]
                    self._total = len(self._queue)
                    wm.progress_begin(0, max(1, self._total))
                if self._queue:
                    idx = self._queue.pop(0)
                    _render_shot(context, s.shots[idx], self._mode)
                    wm.progress_update(self._total - len(self._queue))
                    if bpy.data.is_saved:
                        try:
                            bpy.ops.wm.save_mainfile()
                        except Exception:
                            pass
                else:
                    failed = [sh.name for sh in s.shots if getattr(sh, "status_" + self._mode) == "FAILED"]
                    if failed:
                        return self._end(context, "Render failed for: " + ", ".join(failed),
                                         "ERROR", "CANCELLED")
                    self._stage = "export"
            elif st == "export":
                res = bpy.ops.cartoon.export_episode("EXEC_DEFAULT", render_now=True)
                if res != {"FINISHED"}:
                    return self._end(context, "Export failed - see the log", "ERROR", "CANCELLED")
                return self._end(context, "Episode finished! Check renders/final")
        except Exception as e:
            _log(s, traceback.format_exc())
            return self._end(context, f"Auto production error: {e}", "ERROR", "CANCELLED")
        return {"RUNNING_MODAL"}


classes = (
    CARTOON_OT_auto_produce,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
