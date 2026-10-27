"""Sidebar panels and lists."""
import bpy
from bpy.types import Panel, UIList

from .helpers import _S, _char_missing, _shot_ranges, _shot_cast


class CARTOON_UL_chars(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        missing = _char_missing(item, context.scene)
        arm = bpy.data.objects.get(item.armature_name)
        static = arm is not None and arm.type != "ARMATURE"
        layout.label(text=item.name, icon="ERROR" if missing else ("OBJECT_DATA" if static else "ARMATURE_DATA"))
        layout.label(text="deleted from layout" if missing else ("no skeleton" if static else item.armature_name))


class CARTOON_UL_anims(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon="ACTION")


CAMERA_SHORT = {"AUTO": "Auto", "WIDE": "Wide", "FULL": "Full", "MEDIUM": "Medium", "CLOSE": "Close",
                "OTS": "OTS", "TWO_SHOT": "Two-shot", "LOW": "Low", "HIGH": "High", "TRACK": "Track",
                "ORBIT": "Orbit"}


def _draw_action_ranges(layout, s, cues, slot, index=-1):
    box = layout.box()
    box.label(text="Action timeline (shot-local frames)", icon="ACTION")
    for cue_i, cue in enumerate(cues):
        row = box.row(align=True)
        row.prop_search(cue, "action", s, "animations", text=f"{cue.start_frame}-{cue.end_frame}")
        row.prop(cue, "start_frame", text="From")
        row.prop(cue, "end_frame", text="To")
        op = row.operator("cartoon.action_cue_remove", icon="X", text="")
        op.slot, op.index, op.cue_index = slot, index, cue_i
        pos = box.row(align=True)
        pos.prop(cue, "use_position", text="Position")
        if cue.use_position:
            pos.prop(cue, "position_x", text="X")
            pos.prop(cue, "position_y", text="Y")
            pos.prop(cue, "position_z", text="Z")
    row = box.row(align=True)
    row.label(text="Ranges can switch actions during this shot.")
    op = row.operator("cartoon.action_cue_add", icon="ADD", text="Add action range")
    op.slot, op.index = slot, index


class CARTOON_UL_shots(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        icons = {"PENDING": "TIME", "DONE": "CHECKMARK", "FAILED": "ERROR"}
        ranges = _shot_ranges(data)
        f0, f1 = ranges[index] if index < len(ranges) else (0, 0)
        row = layout.row(align=True)
        row.label(text=f"{index + 1:02d}")
        row.prop(item, "name", text="", emboss=False)
        row.label(text=f"{f0}-{f1}")
        row.label(text=CAMERA_SHORT.get(item.camera_type, item.camera_type.title()))
        row.label(text="", icon=icons[item.status_preview])
        row.label(text="", icon=icons[item.status_final])


class CARTOON_PT_main(Panel):
    bl_label = "Cartoon Studio"
    bl_idname = "CARTOON_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"

    def draw(self, context):
        s = _S(context)
        col = self.layout.column()
        box = col.box()
        box.label(text="One-click production and manual review")
        box.operator("cartoon.prepare_hybrid", text="Prepare Episode for Manual Review", icon="FILE_TICK")
        op = box.operator("cartoon.auto_produce", text="One-click Preview", icon="PLAY")
        op.final = False
        op = box.operator("cartoon.auto_produce", text="One-click Final Episode", icon="RENDER_ANIMATION")
        op.final = True
        col.prop(s, "project_dir")
        col.prop(s, "episode_name")
        row = col.row(align=True)
        row.prop(s, "res_x")
        row.prop(s, "res_y")
        col.prop(s, "fps")
        box = col.box()
        box.label(text="AI camera planner", icon="CAMERA_DATA")
        box.prop(s, "ollama_enabled")
        if s.ollama_enabled:
            box.prop(s, "ollama_url")
            box.prop(s, "ollama_model")
            box.prop(s, "ollama_timeout")
            box.prop(s, "ollama_story_enabled")
            box.label(text="Local Ollama only; built-in planner is the fallback.")
        row = col.row(align=True)
        row.operator("cartoon.project_create", icon="NEWFOLDER")
        row = col.row(align=True)
        row.operator("cartoon.save_json", icon="FILE_TICK")
        row.operator("cartoon.load_json", icon="FILE_REFRESH")


class CARTOON_PT_chars(Panel):
    bl_label = "Character Library"
    bl_idname = "CARTOON_PT_chars"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        row = self.layout.row()
        row.template_list("CARTOON_UL_chars", "", s, "characters", s, "char_index", rows=3)
        col = row.column(align=True)
        col.operator("cartoon.char_add", icon="ADD", text="")
        col.operator("cartoon.char_remove", icon="REMOVE", text="")
        if any(_char_missing(c, context.scene) for c in s.characters):
            self.layout.label(text="Some characters are not in the layout", icon="ERROR")
        row = self.layout.row(align=True)
        row.operator("cartoon.char_restore", icon="FILE_REFRESH")
        row.operator("cartoon.char_restore_all", icon="RECOVER_LAST")
        row.operator("cartoon.char_clear_selection", icon="X", text="Clear")
        self.layout.operator("cartoon.char_save_blend", icon="FILE_TICK")
        self.layout.label(text="Any model works: with/without skeleton, any pose", icon="INFO")
        self.layout.prop(s, "char_height")
        self.layout.prop(s, "rig_template")
        self.layout.operator("cartoon.auto_rig", icon="OUTLINER_OB_ARMATURE")
        box = self.layout.box()
        box.label(text="Fit a skeleton with markers (like Mixamo)", icon="EMPTY_AXIS")
        box.label(text="Mesh in T-pose, facing the front. Select it, then:")
        box.operator("cartoon.marker_add", icon="EMPTY_AXIS")
        row = box.row(align=True)
        op = row.operator("cartoon.marker_mirror", text="Mirror Left to Right")
        op.source = "L"
        op = row.operator("cartoon.marker_mirror", text="Right to Left")
        op.source = "R"
        box.operator("cartoon.rig_from_markers", icon="OUTLINER_OB_ARMATURE")


class CARTOON_PT_anims(Panel):
    bl_label = "Animation Library"
    bl_idname = "CARTOON_PT_anims"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        row = self.layout.row()
        row.template_list("CARTOON_UL_anims", "", s, "animations", s, "anim_index", rows=4)
        col = row.column(align=True)
        col.operator("cartoon.anim_add", icon="ADD", text="")
        col.operator("cartoon.anim_remove", icon="REMOVE", text="")
        row = self.layout.row(align=True)
        row.operator("cartoon.anim_register_existing", icon="ACTION")
        row.operator("cartoon.anim_clear_selection", icon="X", text="Clear")


class CARTOON_PT_shots(Panel):
    bl_label = "Shot Builder"
    bl_idname = "CARTOON_PT_shots"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"

    def draw(self, context):
        s = _S(context)
        lay = self.layout
        row = lay.row(align=True)
        row.prop(s, "story_file")
        row.operator("cartoon.story_load_text", text="Load .txt", icon="FILE_FOLDER")
        lay.prop(s, "story_text")
        lay.prop(s, "auto_cast_count")
        row = lay.row(align=True)
        row.operator("cartoon.ai_story_breakdown", text="AI Story Breakdown", icon="OUTLINER_OB_ARMATURE")
        row.operator("cartoon.story_to_shots", text="Split Blank Lines", icon="TEXT")
        row = lay.row()
        row.template_list("CARTOON_UL_shots", "", s, "shots", s, "shot_index", rows=5)
        col = row.column(align=True)
        col.operator("cartoon.shot_add", icon="ADD", text="")
        col.operator("cartoon.shot_remove", icon="REMOVE", text="")
        col.operator("cartoon.shot_duplicate", icon="DUPLICATE", text="")
        op = col.operator("cartoon.shot_move", icon="TRIA_UP", text="")
        op.direction = "UP"
        op = col.operator("cartoon.shot_move", icon="TRIA_DOWN", text="")
        op.direction = "DOWN"

        if len(s.shots):
            ranges = _shot_ranges(s)
            lay.label(text=f"Timeline: frames 1-{ranges[-1][1]}  ({len(s.shots)} shots, one after the other)",
                      icon="TIME")

        if 0 <= s.shot_index < len(s.shots):
            sh = s.shots[s.shot_index]
            f0, f1 = _shot_ranges(s)[s.shot_index]
            box = lay.box()
            box.label(text=f"{sh.name}:  frames {f0} - {f1}", icon="SEQUENCE")
            box.prop(sh, "duration")
            box.prop(sh, "shot_summary")
            box.prop(sh, "dialogue")
            box.prop(sh, "speaker_name")
            box.prop(sh, "location_note")
            box.prop(sh, "props_note")
            box.prop(sh, "emotion_note")
            box.prop(sh, "action_note")
            box.prop(sh, "camera_type")
            if sh.camera_type == "AUTO":
                box.prop(sh, "camera_angles")
            if sh.cam_note:
                box.label(text=sh.cam_note, icon="CAMERA_DATA")
            box.prop(sh, "light_type")
            box.prop(sh, "transition")
            if sh.transition == "CROSS":
                box.prop(sh, "transition_frames")
            box.prop(sh, "voice_file")

            b = box.box()
            b.label(text="Characters in this shot")
            b2 = b.box()
            b2.label(text="Character A")
            b2.prop_search(sh, "char_a", s, "characters", text="Who")
            b2.prop_search(sh, "action_a", s, "animations", text="Fallback action")
            r = b2.row(align=True)
            r.prop(sh, "char_a_x")
            r.prop(sh, "char_a_y")
            _draw_action_ranges(b2, s, sh.actions_a, "A")
            b2 = b.box()
            b2.label(text="Character B")
            b2.prop_search(sh, "char_b", s, "characters", text="Who")
            b2.prop_search(sh, "action_b", s, "animations", text="Fallback action")
            r = b2.row(align=True)
            r.prop(sh, "char_b_x")
            r.prop(sh, "char_b_y")
            _draw_action_ranges(b2, s, sh.actions_b, "B")
            for i, e in enumerate(sh.extras):
                b2 = b.box()
                r = b2.row()
                r.label(text=f"Character {i + 3}")
                op = r.operator("cartoon.cast_remove", icon="X", text="")
                op.index = i
                b2.prop_search(e, "char", s, "characters", text="Who")
                b2.prop_search(e, "action", s, "animations", text="Fallback action")
                r = b2.row(align=True)
                r.prop(e, "x")
                r.prop(e, "y")
                _draw_action_ranges(b2, s, e.actions, "X", i)
            r = b.row(align=True)
            r.operator("cartoon.cast_add", icon="ADD")
            r.operator("cartoon.cast_spread", icon="ALIGN_JUSTIFY")
            b.prop(sh, "auto_move")
            if sh.auto_move:
                b.prop(sh, "move_dist")
            b.prop(sh, "face_each_other")

        lay.prop(s, "keep_placement")
        lay.prop(s, "continue_shots")
        lay.operator("cartoon.build_episode", icon="SCENE_DATA")
        row = lay.row(align=True)
        row.operator("cartoon.shot_goto", icon="REW", text="Go To Shot")
        op = row.operator("cartoon.shot_play", icon="PLAY", text="Play Shot")
        op.mode = "SHOT"
        op = row.operator("cartoon.shot_play", icon="SEQ_STRIP_META", text="Play All")
        op.mode = "EPISODE"


class CARTOON_PT_render(Panel):
    bl_label = "Render Queue"
    bl_idname = "CARTOON_PT_render"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        lay = self.layout
        lay.prop(s, "preview_scale")
        lay.prop(s, "preview_samples")
        lay.prop(s, "final_samples")
        op = lay.operator("cartoon.render_queue", text="Render Previews", icon="RENDER_ANIMATION")
        op.mode, op.redo = "preview", False
        op = lay.operator("cartoon.render_queue", text="Render Finals", icon="RENDER_ANIMATION")
        op.mode, op.redo = "final", False
        row = lay.row(align=True)
        op = row.operator("cartoon.reset_status", text="Retry Failed")
        op.only_failed = True
        op = row.operator("cartoon.reset_status", text="Redo All")
        op.only_failed = False


class CARTOON_PT_export(Panel):
    bl_label = "Quality Check & Export"
    bl_idname = "CARTOON_PT_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        lay = self.layout
        lay.operator("cartoon.run_checks", icon="VIEWZOOM")
        icons = {"ERROR": "ERROR", "WARNING": "ERROR", "INFO": "INFO", "OK": "CHECKMARK"}
        for c in s.checks:
            lay.label(text=c.name, icon=icons.get(c.level, "INFO"))
        lay.separator()
        lay.prop(s, "voice_file")
        lay.prop(s, "music_path")
        lay.prop(s, "burn_subtitles")
        lay.prop(s, "subtitle_font")
        op = lay.operator("cartoon.export_episode", text="Assemble Edit (VSE)", icon="SEQUENCE")
        op.render_now = False
        op = lay.operator("cartoon.export_episode", text="Export Episode MP4", icon="FILE_MOVIE")
        op.render_now = True


# --------------------------------------------------------------------------


classes = (
    CARTOON_UL_chars,
    CARTOON_UL_anims,
    CARTOON_UL_shots,
    CARTOON_PT_main,
    CARTOON_PT_chars,
    CARTOON_PT_anims,
    CARTOON_PT_shots,
    CARTOON_PT_render,
    CARTOON_PT_export,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
