"""Shot list: story to shots, add/remove/move/duplicate, cast (unlimited characters), playback."""
import bpy
import json
import os
import urllib.request
from bpy.props import BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator

from .helpers import (_S, _split_story, _pg_to_dict, _dict_to_pg, _next_shot_name,
                      _spread_x, _shot_cast, _shot_ranges)
from .data import CAMERA_ITEMS, LIGHT_ITEMS


class CARTOON_OT_story_load_text(Operator):
    bl_idname = "cartoon.story_load_text"
    bl_label = "Load Story File"
    bl_description = "Load a .txt script into Blender so Story to Shots and one-click production can use it"

    def execute(self, context):
        s = _S(context)
        path = bpy.path.abspath(s.story_file) if s.story_file else ""
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "Choose an existing .txt file first")
            return {"CANCELLED"}
        try:
            with open(path, "rb") as source:
                raw = source.read()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    content = raw.decode("utf-16")
                except UnicodeDecodeError:
                    content = raw.decode("cp1252")
            if not content.strip():
                self.report({"ERROR"}, "The selected text file is empty")
                return {"CANCELLED"}
            name = os.path.splitext(os.path.basename(path))[0] or "Cartoon_Story"
            text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
            text.clear()
            text.write(content)
            s.story_text = text
            self.report({"INFO"}, f"Loaded {os.path.basename(path)}. You can edit it in the Story Text field.")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Could not load story file: {exc}")
            return {"CANCELLED"}


class CARTOON_OT_story_to_shots(Operator):
    bl_idname = "cartoon.story_to_shots"
    bl_label = "Story to Shots"
    bl_description = "Split the story Text block into shots (replaces the current shot list)"

    def invoke(self, context, event):
        if len(_S(context).shots):
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        s = _S(context)
        if s.story_text is None:
            self.report({"ERROR"}, "Pick a story Text block first")
            return {"CANCELLED"}
        beats = _split_story(s.story_text.as_string())
        if not beats:
            self.report({"ERROR"}, "Story is empty")
            return {"CANCELLED"}
        s.shots.clear()
        chars = [c.name for c in s.characters]
        n = max(1, min(len(chars), s.auto_cast_count)) if chars else 0
        xs = _spread_x(n) if n else []
        for i, beat in enumerate(beats):
            sh = s.shots.add()
            sh.name = f"S{i + 1:03d}"
            sh.dialogue = beat
            words = len(beat.split())
            frames = int(max(2.5, words / 2.5) * s.fps)
            sh.duration = max(60, ((frames + 9) // 10) * 10)     # tidy numbers: 100, 110, 120 ...
            sh.camera_type = "AUTO"
            if n:
                sh.char_a, sh.char_a_x = chars[0], xs[0]
            if n > 1:
                sh.char_b, sh.char_b_x = chars[1], xs[1]
            for k in range(2, n):
                e = sh.extras.add()
                e.char, e.x = chars[k], xs[k]
        s.shot_index = 0
        self.report({"INFO"}, f"Created {len(beats)} shots")
        return {"FINISHED"}


class CARTOON_OT_ai_story_breakdown(Operator):
    bl_idname = "cartoon.ai_story_breakdown"
    bl_label = "AI Story Breakdown"
    bl_description = "Ask the local Ollama model to turn the script into editable shots, locations, props, emotions and action notes"

    def invoke(self, context, event):
        if len(_S(context).shots):
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        s = _S(context)
        if s.story_text is None or not s.story_text.as_string().strip():
            self.report({"ERROR"}, "Choose a non-empty story Text block first")
            return {"CANCELLED"}
        script = s.story_text.as_string()
        roster = [character.name for character in s.characters]
        allowed_cameras = "AUTO, WIDE, FULL, MEDIUM, CLOSE, OTS, TWO_SHOT, LOW, HIGH, TRACK, ORBIT"
        prompt = (
            "Break this animation script into a concise, editable cartoon storyboard. Preserve every dialogue line "
            "verbatim and in order. Return JSON only: {\"shots\":[{\"summary\":\"visual beat\",\"dialogue\":\"exact words\","
            "\"characters\":[\"names from roster only\"],\"speaker\":\"exact roster name or empty\","
            "\"action\":\"short action cue\",\"emotion\":\"emotion\","
            "\"location\":\"place\",\"props\":[\"needed props\"],\"camera\":\"one allowed camera\","
            "\"light\":\"DAY or NIGHT or WARM or SUNSET\",\"seconds\":4}]}. Use at most 24 shots; combine adjacent lines only when they share one continuous action and location. "
            "Camera choices must be one of: " + allowed_cameras + ". Characters must exactly match the roster; if unclear use []. "
            "Roster: " + (", ".join(roster) if roster else "none") + "\nSCRIPT:\n" + script
        )
        url = s.ollama_url.rstrip("/") + "/api/generate"
        request = urllib.request.Request(url, data=json.dumps({
            "model": s.ollama_model or "qwen3-vl:4b", "prompt": prompt,
            "stream": False, "format": "json",
            # Disable extended reasoning for this constrained JSON task. On CPU this
            # greatly reduces stalls while retaining the model's shot suggestions.
            "think": False,
            "options": {"num_predict": 2048, "temperature": 0.15},
        }).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=max(30, int(s.ollama_timeout))) as response:
                payload = json.loads(response.read().decode("utf-8"))
            answer = json.loads(payload.get("response", "{}"))
            shots = answer.get("shots", [])
        except Exception as exc:
            self.report({"WARNING"}, f"Ollama story breakdown unavailable ({exc}); use Split Blank Lines or retry")
            return {"CANCELLED"}
        if not isinstance(shots, list) or not shots:
            self.report({"ERROR"}, "Ollama returned no usable shots; use Story to Shots for a local fallback")
            return {"CANCELLED"}
        shots = [item for item in shots[:24] if isinstance(item, dict)]
        if not shots:
            self.report({"ERROR"}, "Ollama returned no valid shot records")
            return {"CANCELLED"}

        s.shots.clear()
        roster_by_key = {name.casefold(): name for name in roster}
        default_count = max(1, min(len(roster), s.auto_cast_count)) if roster else 0
        default_chars = roster[:default_count]
        camera_values = {item[0] for item in CAMERA_ITEMS}
        light_values = {item[0] for item in LIGHT_ITEMS}
        for i, item in enumerate(shots):
            sh = s.shots.add()
            sh.name = f"S{i + 1:03d}"
            sh.shot_summary = str(item.get("summary", ""))[:240]
            sh.dialogue = str(item.get("dialogue", ""))[:2048]
            sh.location_note = str(item.get("location", ""))[:160]
            props = item.get("props", [])
            sh.props_note = ", ".join(str(v) for v in props)[:256] if isinstance(props, list) else str(props)[:256]
            sh.emotion_note = str(item.get("emotion", ""))[:120]
            speaker = str(item.get("speaker", ""))
            sh.speaker_name = roster_by_key.get(speaker.casefold(), "")
            action = item.get("action", "")
            sh.action_note = ", ".join(str(v) for v in action)[:200] if isinstance(action, list) else str(action)[:200]
            words = len(sh.dialogue.split())
            seconds = item.get("seconds", max(2.5, words / 2.5))
            try:
                seconds = max(2.0, min(30.0, float(seconds)))
            except (TypeError, ValueError):
                seconds = max(2.5, words / 2.5)
            frames = int(seconds * s.fps)
            sh.duration = max(48, ((frames + 5) // 10) * 10)
            camera = str(item.get("camera", "AUTO")).strip().upper()
            sh.camera_type = camera if camera in camera_values else "AUTO"
            light = str(item.get("light", "DAY")).strip().upper()
            sh.light_type = light if light in light_values else "DAY"
            requested = item.get("characters", [])
            if isinstance(requested, str):
                requested = [requested]
            chosen = []
            for name in requested if isinstance(requested, list) else []:
                match = roster_by_key.get(str(name).casefold())
                if match and match not in chosen:
                    chosen.append(match)
            if not chosen:
                chosen = list(default_chars)
            if chosen:
                sh.char_a = chosen[0]
                sh.char_a_x = -1.5 if len(chosen) > 1 else 0.0
            if len(chosen) > 1:
                sh.char_b, sh.char_b_x = chosen[1], 1.5
            for j, name in enumerate(chosen[2:]):
                extra = sh.extras.add()
                extra.char = name
                extra.x = (j + 1) * 2.2
        if not len(s.shots):
            self.report({"ERROR"}, "The response did not contain valid shot records")
            return {"CANCELLED"}
        s.shot_index = 0
        self.report({"INFO"}, f"AI created {len(s.shots)} editable shots using {s.ollama_model}; review before rendering")
        return {"FINISHED"}


class CARTOON_OT_shot_add(Operator):
    bl_idname = "cartoon.shot_add"
    bl_label = "Add Shot"

    def execute(self, context):
        s = _S(context)
        nm = _next_shot_name(s)
        sh = s.shots.add()
        sh.name = nm
        if len(s.characters):
            sh.char_a = s.characters[0].name
            sh.char_a_x = 0.0
        s.shot_index = len(s.shots) - 1
        return {"FINISHED"}


class CARTOON_OT_shot_remove(Operator):
    bl_idname = "cartoon.shot_remove"
    bl_label = "Remove Shot"

    def execute(self, context):
        s = _S(context)
        if 0 <= s.shot_index < len(s.shots):
            s.shots.remove(s.shot_index)
            s.shot_index = max(0, s.shot_index - 1)
        return {"FINISHED"}


class CARTOON_OT_shot_move(Operator):
    bl_idname = "cartoon.shot_move"
    bl_label = "Move Shot"
    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        s = _S(context)
        i = s.shot_index
        j = i - 1 if self.direction == "UP" else i + 1
        if 0 <= i < len(s.shots) and 0 <= j < len(s.shots):
            s.shots.move(i, j)
            s.shot_index = j
        return {"FINISHED"}


class CARTOON_OT_shot_duplicate(Operator):
    bl_idname = "cartoon.shot_duplicate"
    bl_label = "Duplicate Shot"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            return {"CANCELLED"}
        data = _pg_to_dict(s.shots[s.shot_index])
        nm = _next_shot_name(s)
        new = s.shots.add()
        _dict_to_pg(new, data)
        new.name = nm
        new.sig_preview = new.sig_final = ""
        new.status_preview = new.status_final = "PENDING"
        new.preview_path = new.final_path = ""
        s.shots.move(len(s.shots) - 1, s.shot_index + 1)
        s.shot_index += 1
        return {"FINISHED"}


class CARTOON_OT_reset_status(Operator):
    bl_idname = "cartoon.reset_status"
    bl_label = "Reset Render Status"
    only_failed: BoolProperty(default=True)

    def execute(self, context):
        for sh in _S(context).shots:
            for attr in ("status_preview", "status_final"):
                if not self.only_failed or getattr(sh, attr) == "FAILED":
                    setattr(sh, attr, "PENDING")
        return {"FINISHED"}


class CARTOON_OT_cast_add(Operator):
    bl_idname = "cartoon.cast_add"
    bl_label = "Add Character To Shot"
    bl_description = "Put one more character into the selected shot (no limit)"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            self.report({"ERROR"}, "Select a shot first")
            return {"CANCELLED"}
        if not len(s.characters):
            self.report({"ERROR"}, "Add characters to the Character Library first")
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        cast = _shot_cast(sh)
        used = {c["name"] for c in cast}
        pick = next((c.name for c in s.characters if c.name not in used), s.characters[0].name)
        e = sh.extras.add()
        e.char = pick
        e.x = (max(c["x"] for c in cast) + 2.5) if cast else 0.0
        return {"FINISHED"}


class CARTOON_OT_cast_remove(Operator):
    bl_idname = "cartoon.cast_remove"
    bl_label = "Remove From Shot"
    bl_description = "Take this extra character out of the shot"

    index: IntProperty()

    def execute(self, context):
        s = _S(context)
        if 0 <= s.shot_index < len(s.shots):
            sh = s.shots[s.shot_index]
            if 0 <= self.index < len(sh.extras):
                sh.extras.remove(self.index)
        return {"FINISHED"}


class CARTOON_OT_cast_spread(Operator):
    bl_idname = "cartoon.cast_spread"
    bl_label = "Spread Evenly"
    bl_description = "Stand everybody in the selected shot in an even row, centred in front of the camera"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        members = []
        if sh.char_a:
            members.append(("a", None))
        if sh.char_b:
            members.append(("b", None))
        members += [("x", e) for e in sh.extras if e.char]
        xs = _spread_x(len(members))
        for (kind, e), x in zip(members, xs):
            if kind == "a":
                sh.char_a_x, sh.char_a_y = x, 0.0
            elif kind == "b":
                sh.char_b_x, sh.char_b_y = x, 0.0
            else:
                e.x, e.y = x, 0.0
        return {"FINISHED"}


class CARTOON_OT_action_cue_add(Operator):
    bl_idname = "cartoon.action_cue_add"
    bl_label = "Add Action Range"
    bl_description = "Add another action with a local frame range inside this shot"

    slot: EnumProperty(items=[("A", "Character A", ""), ("B", "Character B", ""),
                              ("X", "Extra Character", "")])
    index: IntProperty(default=-1)

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        cues = sh.actions_a if self.slot == "A" else sh.actions_b if self.slot == "B" else (
            sh.extras[self.index].actions if 0 <= self.index < len(sh.extras) else None)
        if cues is None:
            return {"CANCELLED"}
        cue = cues.add()
        cue.start_frame = max([1] + [q.end_frame + 1 for q in cues[:-1]])
        cue.start_frame = min(cue.start_frame, max(1, sh.duration))
        cue.end_frame = min(sh.duration, cue.start_frame + 49)
        return {"FINISHED"}


class CARTOON_OT_action_cue_remove(Operator):
    bl_idname = "cartoon.action_cue_remove"
    bl_label = "Remove Action Range"

    slot: EnumProperty(items=[("A", "Character A", ""), ("B", "Character B", ""),
                              ("X", "Extra Character", "")])
    index: IntProperty(default=-1)
    cue_index: IntProperty(default=-1)

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        cues = sh.actions_a if self.slot == "A" else sh.actions_b if self.slot == "B" else (
            sh.extras[self.index].actions if 0 <= self.index < len(sh.extras) else None)
        if cues is not None and 0 <= self.cue_index < len(cues):
            cues.remove(self.cue_index)
        return {"FINISHED"}


class CARTOON_OT_shot_goto(Operator):
    bl_idname = "cartoon.shot_goto"
    bl_label = "Go To Shot"
    bl_description = "Jump the timeline to the first frame of the selected shot (the shot's camera switches in automatically)"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        if sh.end_frame <= 0:
            self.report({"ERROR"}, "Build the episode first")
            return {"CANCELLED"}
        context.scene.frame_set(sh.start_frame)
        return {"FINISHED"}


class CARTOON_OT_shot_play(Operator):
    bl_idname = "cartoon.shot_play"
    bl_label = "Play"
    bl_description = "Play the selected shot, or the whole episode, in the viewport (press again to stop)"

    mode: EnumProperty(items=[("SHOT", "Shot", "Play only the selected shot"),
                              ("EPISODE", "Episode", "Play every shot in order")], default="SHOT")

    def execute(self, context):
        s = _S(context)
        scene = context.scene
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
            return {"FINISHED"}
        if not len(s.shots) or s.shots[0].end_frame <= 0:
            self.report({"ERROR"}, "Build the episode first")
            return {"CANCELLED"}
        if self.mode == "SHOT":
            if not (0 <= s.shot_index < len(s.shots)):
                return {"CANCELLED"}
            sh = s.shots[s.shot_index]
            scene.use_preview_range = True
            scene.frame_preview_start, scene.frame_preview_end = sh.start_frame, sh.end_frame
            scene.frame_set(sh.start_frame)
        else:
            scene.use_preview_range = False
            scene.frame_set(scene.frame_start)
        bpy.ops.screen.animation_play()
        return {"FINISHED"}


classes = (
    CARTOON_OT_story_load_text,
    CARTOON_OT_story_to_shots,
    CARTOON_OT_ai_story_breakdown,
    CARTOON_OT_shot_add,
    CARTOON_OT_shot_remove,
    CARTOON_OT_shot_move,
    CARTOON_OT_shot_duplicate,
    CARTOON_OT_reset_status,
    CARTOON_OT_cast_add,
    CARTOON_OT_cast_remove,
    CARTOON_OT_cast_spread,
    CARTOON_OT_action_cue_add,
    CARTOON_OT_action_cue_remove,
    CARTOON_OT_shot_goto,
    CARTOON_OT_shot_play,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
