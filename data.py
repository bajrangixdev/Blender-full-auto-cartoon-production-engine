"""All stored data: characters, animations, shots, checks and settings."""
import bpy
from bpy.props import (
    StringProperty, IntProperty, FloatProperty, BoolProperty,
    EnumProperty, CollectionProperty, PointerProperty,
)
from bpy.types import PropertyGroup

STATUS_ITEMS = [("PENDING", "Pending", ""), ("DONE", "Done", ""), ("FAILED", "Failed", "")]
# Explicit numbers: shots saved with an older version keep the same camera type.
CAMERA_ITEMS = [
    ("AUTO", "Auto (follows the action)",
     "Camera is chosen from the action, where the characters stand, whether they move, and the dialogue", "CAMERA_DATA", 5),
    ("WIDE", "Wide", "Establishing shot, whole group and surroundings", "", 0),
    ("FULL", "Full body", "Whole body of everybody in the shot", "", 6),
    ("MEDIUM", "Medium", "Waist-up framing", "", 1),
    ("CLOSE", "Close-up", "Face framing (on the first character)", "", 2),
    ("OTS", "Over shoulder", "Camera behind one character looking at the other (needs 2 characters)", "", 3),
    ("TWO_SHOT", "Two-shot / group", "Everybody in frame together", "", 7),
    ("LOW", "Low angle", "Heroic low angle", "", 4),
    ("HIGH", "High angle", "Camera looks down on the characters", "", 8),
    ("TRACK", "Tracking (follow)", "Camera moves along with a walking/running character", "", 9),
    ("ORBIT", "Orbit", "Camera slowly circles the characters", "", 10),
]
TRANSITION_ITEMS = [
    ("CUT", "Cut", "No overlap between this shot and the next"),
    ("CROSS", "Crossfade", "Blend this shot into the next during export"),
]
ASSET_CATEGORY_ITEMS = [("TREE", "Tree / Plant", ""), ("CAR", "Vehicle", ""),
                        ("BUILDING", "Building", ""), ("PROP", "Prop", ""), ("OTHER", "Other", "")]
ASSET_FILTER_ITEMS = [("ALL", "All Assets", "")] + ASSET_CATEGORY_ITEMS
REFERENCE_ITEMS = [("MAP", "Top-down map", ""), ("PHOTO", "Perspective photo", "")]
LIGHT_ITEMS = [
    ("DAY", "Day", ""), ("NIGHT", "Night", ""),
    ("WARM", "Warm indoor", ""), ("SUNSET", "Sunset", ""),
]
ENVIRONMENT_ITEMS = [
    ("FOREST", "Forest", "Trees and a clear central path"),
    ("VILLAGE", "Village", "Village road and building asset placement"),
    ("ROAD", "Road", "Open road with roadside asset placement"),
    ("OPEN", "Open land", "Simple open ground for custom layouts"),
]
MOOD_ITEMS = [
    ("DAY", "Daylight", "Bright neutral world lighting"),
    ("SUNSET", "Sunset", "Warm late-day world lighting"),
    ("NIGHT", "Night", "Dark cool world lighting"),
    ("FOG", "Fog", "Cool low-contrast world with light volumetric fog"),
]


class CS_Character(PropertyGroup):
    name: StringProperty()
    filepath: StringProperty(subtype="FILE_PATH")
    armature_name: StringProperty()
    backup_path: StringProperty(subtype="FILE_PATH")


class CS_Anim(PropertyGroup):
    name: StringProperty()
    filepath: StringProperty(subtype="FILE_PATH")
    action_name: StringProperty()


class CS_ActionCue(PropertyGroup):
    """One action occupying an inclusive, shot-local frame range."""
    action: StringProperty(name="Action")
    start_frame: IntProperty(name="Start", default=1, min=1)
    end_frame: IntProperty(name="End", default=50, min=1)
    use_position: BoolProperty(name="Set action position", default=False,
                               description="Move the character toward this world position during this action")
    position_x: FloatProperty(name="Position X", default=0.0, unit="LENGTH")
    position_y: FloatProperty(name="Position Y", default=0.0, unit="LENGTH")
    position_z: FloatProperty(name="Position Z", default=0.0, unit="LENGTH")


class CS_VFX(PropertyGroup):
    name: StringProperty(name="Name")
    root_object: StringProperty(name="Source Object")
    action_match: StringProperty(name="Action contains", description="Only apply when an action name contains this text")
    anchor: EnumProperty(name="Attach To", items=[("AUTO", "Auto", "Choose a hand or root from the action"),
        ("ROOT", "Character Root", "Attach at the character origin"), ("HAND", "Hand", "Attach to a hand bone"),
        ("HEAD", "Head", "Attach to a head bone")], default="AUTO")
    offset_x: FloatProperty(name="Offset X", default=0.0)
    offset_y: FloatProperty(name="Offset Y", default=0.0)
    offset_z: FloatProperty(name="Offset Z", default=0.0)
    rotation_z: FloatProperty(name="Rotation Z", default=0.0, subtype="ANGLE")
    scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=100.0)


class CS_VFXEvent(PropertyGroup):
    """A hand-editable, shot-local effect event, optionally attached to one cast member."""
    effect: StringProperty(name="Effect")
    character: StringProperty(name="Character")
    frame: IntProperty(name="Shot Frame", default=30, min=1)
    duration: IntProperty(name="Duration", default=24, min=1, max=10000)
    offset_x: FloatProperty(name="Offset X", default=0.0)
    offset_y: FloatProperty(name="Offset Y", default=0.0)
    offset_z: FloatProperty(name="Offset Z", default=0.0)
    scale: FloatProperty(name="Scale", default=1.0, min=0.01, max=100.0)


class CS_Cast(PropertyGroup):
    """An extra character in a shot (Character A and B live directly on the shot)."""
    char: StringProperty(name="Character")
    action: StringProperty(name="Action")
    x: FloatProperty(name="X", default=0.0, description="Left / right position (metres)")
    y: FloatProperty(name="Depth", default=0.0, description="Towards (-) or away from (+) the audience (metres)")
    actions: CollectionProperty(type=CS_ActionCue)


class CS_Shot(PropertyGroup):
    name: StringProperty(default="S001")
    shot_summary: StringProperty(name="Scene / Shot", description="AI or manually written description of the visual beat")
    location_note: StringProperty(name="Location", description="Continuity location for this shot")
    props_note: StringProperty(name="Props", description="Important props required in this shot")
    emotion_note: StringProperty(name="Emotion", description="Acting and camera emotion guide")
    action_note: StringProperty(name="Action Notes", description="What should happen in the shot")
    speaker_name: StringProperty(name="Speaker", description="Dialogue speaker from the character library")
    dialogue: StringProperty(name="Dialogue / Subtitle")
    voice_file: StringProperty(name="Voice File", subtype="FILE_PATH",
                               description="Optional audio for this shot (your Hindi recording)")
    duration: IntProperty(name="Frames", default=100, min=6,
                          description="Length of this shot. Shots follow each other: 100 frames each gives 1-100, 101-200, 201-300 ...")
    start_frame: IntProperty(default=0)
    end_frame: IntProperty(default=0)
    char_a: StringProperty(name="Character A")
    action_a: StringProperty(name="Action A")
    actions_a: CollectionProperty(type=CS_ActionCue)
    char_a_x: FloatProperty(name="X", default=-1.5, description="Left / right position (metres)")
    char_a_y: FloatProperty(name="Depth", default=0.0, description="Towards (-) or away from (+) the audience (metres)")
    char_b: StringProperty(name="Character B")
    action_b: StringProperty(name="Action B")
    actions_b: CollectionProperty(type=CS_ActionCue)
    char_b_x: FloatProperty(name="X", default=1.5, description="Left / right position (metres)")
    char_b_y: FloatProperty(name="Depth", default=0.0, description="Towards (-) or away from (+) the audience (metres)")
    extras: CollectionProperty(type=CS_Cast)
    auto_move: BoolProperty(
        name="Walk / run across the shot", default=True,
        description="When the action is a walk or run that stays in place, move the character through the scene "
                    "(and turn it to face the way it goes) so the camera has something to follow")
    move_dist: FloatProperty(name="Walk distance", default=0.0, min=0.0, soft_max=15.0,
                             description="Metres travelled during the shot. 0 = automatic from the action and shot length")
    face_each_other: BoolProperty(
        name="Turn towards each other", default=True,
        description="Characters standing still in a shot with others turn partly towards them")
    cam_note: StringProperty(name="Camera plan", description="What the Auto camera decided (filled by Build Episode)")
    vfx_events: CollectionProperty(type=CS_VFXEvent)
    vfx_event_index: IntProperty()
    camera_type: EnumProperty(name="Camera", items=CAMERA_ITEMS, default="AUTO")
    camera_angles: IntProperty(name="Angles per shot", default=3, min=1, max=12,
                               description="Auto camera makes several close, medium or wide angles inside this shot")
    light_type: EnumProperty(name="Lighting", items=LIGHT_ITEMS, default="DAY")
    transition: EnumProperty(name="Transition to next shot", items=TRANSITION_ITEMS, default="CUT")
    transition_frames: IntProperty(name="Transition Frames", default=12, min=1, max=120)
    status_preview: EnumProperty(items=STATUS_ITEMS, default="PENDING")
    status_final: EnumProperty(items=STATUS_ITEMS, default="PENDING")
    preview_path: StringProperty(subtype="FILE_PATH")
    sig_preview: StringProperty()
    sig_final: StringProperty()
    final_path: StringProperty(subtype="FILE_PATH")


class CS_Asset(PropertyGroup):
    name: StringProperty()
    root_object: StringProperty()
    category: EnumProperty(items=ASSET_CATEGORY_ITEMS, default="OTHER")


class CS_Check(PropertyGroup):
    name: StringProperty()
    level: StringProperty(default="INFO")


class CS_Settings(PropertyGroup):
    project_dir: StringProperty(name="Project Folder", subtype="DIR_PATH")
    episode_name: StringProperty(name="Episode", default="Episode_01")
    story_file: StringProperty(name="Story .txt", subtype="FILE_PATH",
                               description="Choose a UTF-8 text script, then press Load Story File")
    story_text: PointerProperty(name="Story", type=bpy.types.Text,
                                description="Text block with your story. Blank line = new shot")
    fps: IntProperty(name="FPS", default=24, min=1, max=120)
    res_x: IntProperty(name="Width", default=1920, min=16)
    res_y: IntProperty(name="Height", default=1080, min=16)
    preview_scale: IntProperty(name="Preview %", default=50, min=10, max=100)
    preview_samples: IntProperty(name="Preview Samples", default=16, min=1)
    final_samples: IntProperty(name="Final Samples", default=64, min=1)
    voice_file: StringProperty(name="Narration", subtype="FILE_PATH",
                               description="Optional full-episode narration, placed at frame 1")
    music_path: StringProperty(name="Music", subtype="FILE_PATH")
    subtitle_font: StringProperty(name="Subtitle Font", subtype="FILE_PATH",
                                  description="Use a Devanagari font (e.g. C:\\Windows\\Fonts\\Nirmala.ttc) for Hindi")
    burn_subtitles: BoolProperty(name="Burn-in Subtitles", default=False)
    environment_image: StringProperty(name="Reference Image", subtype="FILE_PATH")
    reference_type: EnumProperty(name="Reference Type", items=REFERENCE_ITEMS, default="MAP")
    environment_width: FloatProperty(name="Environment Width", default=30.0, min=1.0, max=10000.0)
    environment_preset: EnumProperty(name="World Layout", items=ENVIRONMENT_ITEMS, default="FOREST")
    environment_mood: EnumProperty(name="World Mood", items=MOOD_ITEMS, default="DAY")
    road_width: FloatProperty(name="Road Width", default=4.0, min=1.0, max=30.0, unit="LENGTH")
    asset_spacing: FloatProperty(name="Asset Spacing", default=2.5, min=0.0, max=100.0, unit="LENGTH")
    asset_category: EnumProperty(name="New Asset Category", items=ASSET_CATEGORY_ITEMS, default="OTHER")
    asset_filter: EnumProperty(name="Scatter Category", items=ASSET_FILTER_ITEMS, default="TREE")
    scatter_count: IntProperty(name="Scatter Count", default=30, min=1, max=10000)
    environment_seed: IntProperty(name="Scatter Seed", default=7, min=0, max=2147483647)
    assets: CollectionProperty(type=CS_Asset)
    asset_index: IntProperty()
    vfx_items: CollectionProperty(type=CS_VFX)
    vfx_index: IntProperty()
    ollama_enabled: BoolProperty(name="Use local Ollama camera planner", default=True,
                                 description="Ask a local Ollama model for shot sizes and angles during Build Episode")
    ollama_story_enabled: BoolProperty(name="Use Ollama story breakdown", default=True,
                                       description="Use your local Ollama model to suggest editable shots when one-click production starts from a story")
    ollama_url: StringProperty(name="Ollama URL", default="http://localhost:11434")
    ollama_model: StringProperty(name="Ollama Model", default="qwen3-vl:4b")
    ollama_timeout: IntProperty(name="Ollama timeout (seconds)", default=300, min=30, max=900,
                                description="Maximum wait for local AI story and camera suggestions; production falls back to built-in tools on timeout")
    mouth_shape_key: StringProperty(name="Open Mouth Shape Key", default="CS_Mouth_Open")
    mouth_open_axis: EnumProperty(name="Open Axis", items=[("X", "Local X", ""),
        ("Y", "Local Y", ""), ("Z", "Local Z", "")], default="Z")
    mouth_open_factor: FloatProperty(name="Open Height", default=2.5, min=1.05, max=10.0)
    mouth_start: IntProperty(name="Start Frame", default=1, min=1)
    mouth_end: IntProperty(name="End Frame", default=100, min=1)
    mouth_cycle: IntProperty(name="Open / close cycle", default=8, min=2, max=120)
    mouth_open_frames: IntProperty(name="Open Frames", default=3, min=1, max=60)
    rig_template: StringProperty(
        name="Rig Template", subtype="FILE_PATH",
        description="Any Mixamo T-pose character .fbx. Its skeleton is fitted to meshes that have no rig")

    auto_cast_count: IntProperty(
        name="Characters per shot (auto)", default=2, min=1, max=12,
        description="How many library characters 'Story to Shots' puts into every shot")
    char_height: FloatProperty(
        name="Character height", default=1.75, min=0.0, max=10.0, unit="LENGTH",
        description="Imported characters are scaled to this height in metres (0 = keep the original size)")

    keep_placement: BoolProperty(
        name="Keep my character placement", default=True,
        description="Build Episode starts every shot with each character exactly where, how big and how turned "
                    "you placed it in the scene (the X / Depth boxes of the shots are ignored). "
                    "Turn off to use the X / Depth layout of each shot instead")

    continue_shots: BoolProperty(
        name="Continue from last shot", default=True,
        description="Every shot starts each character where (and facing the way) it ended in the previous shot, "
                    "instead of jumping back to its start place. Inside one shot, each action also starts where "
                    "the one before it ended. The first shot a character appears in still uses its own placement. "
                    "Turn off to restart every character at its start place in every shot")

    characters: CollectionProperty(type=CS_Character)
    char_index: IntProperty()
    animations: CollectionProperty(type=CS_Anim)
    anim_index: IntProperty()
    shots: CollectionProperty(type=CS_Shot)
    shot_index: IntProperty()
    checks: CollectionProperty(type=CS_Check)


classes = (
    CS_Character,
    CS_Anim,
    CS_ActionCue,
    CS_VFX,
    CS_VFXEvent,
    CS_Cast,
    CS_Shot,
    CS_Check,
    CS_Asset,
    CS_Settings,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.cartoon_studio = PointerProperty(type=CS_Settings)


def unregister():
    del bpy.types.Scene.cartoon_studio
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
