"""Asset library, image-guided environment setup, hybrid workflow and text mouth motion."""
import math
import os
import random
import re
from types import SimpleNamespace

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, UIList
from mathutils import Matrix, Vector

from .helpers import _S, _shot_cast
from .director import build_episode
from .auto_produce import _auto_assign


ASSET_CATEGORIES = [
    ("TREE", "Tree / Plant", "Vegetation and foliage"),
    ("CAR", "Vehicle", "Cars, trucks, and other vehicles"),
    ("BUILDING", "Building", "Buildings and structures"),
    ("PROP", "Prop", "Small scene objects"),
    ("OTHER", "Other", "Any other reusable model"),
]
ASSET_FILTERS = [("ALL", "All Assets", "Scatter all saved assets")] + ASSET_CATEGORIES
REFERENCE_TYPES = [
    ("MAP", "Top-down map", "Show the image flat as a layout guide"),
    ("PHOTO", "Perspective photo", "Show the image upright as a composition guide"),
]


def _collection(scene, name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        scene.collection.children.link(coll)
    elif coll.name not in scene.collection.children:
        scene.collection.children.link(coll)
    return coll


def _asset_tree(root):
    out, stack, seen = [], [root], set()
    while stack:
        obj = stack.pop()
        if obj in seen:
            continue
        seen.add(obj)
        out.append(obj)
        stack.extend(reversed(list(obj.children)))
    return out


def _duplicate_asset(scene, asset, position, rotation=0.0, scale=1.0, procedural=False):
    root = bpy.data.objects.get(asset.root_object)
    if root is None:
        raise RuntimeError(f"Asset source is missing: {asset.root_object}")
    sources = _asset_tree(root)
    dest = _collection(scene, "CS_Environment")
    copies = {}
    for src in sources:
        obj = src.copy()
        if src.data is not None:
            obj.data = src.data
        dest.objects.link(obj)
        copies[src] = obj
    root_world = root.matrix_world.copy()
    root_inverse = root_world.inverted_safe()
    target = (Matrix.Translation(Vector(position)) @ Matrix.Rotation(rotation, 4, "Z") @
              Matrix.Scale(scale, 4))
    for src in sources:
        obj = copies[src]
        if src.parent in copies:
            obj.parent = copies[src.parent]
        obj.matrix_world = target @ root_inverse @ src.matrix_world
        obj.hide_render = False
        obj.hide_set(False)
        obj["cs_environment_asset"] = asset.name
        obj["cs_asset_category"] = asset.category
        if procedural:
            obj["cs_procedural_world"] = True
    copies[root]["cs_asset_instance_root"] = True
    return copies[root]


class CARTOON_UL_assets(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon="OUTLINER_OB_MESH")
        layout.label(text=item.category.title())


class CARTOON_UL_vfx(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon="PARTICLES")
        layout.label(text=item.action_match or "set action match")


class CARTOON_OT_asset_capture(Operator):
    bl_idname = "cartoon.asset_capture"
    bl_label = "Save Selection to Asset Library"
    bl_description = "Store the active object and its children as a reusable scene asset"

    def execute(self, context):
        s = _S(context)
        root = context.view_layer.objects.active
        if root is None or root not in context.selected_objects:
            self.report({"ERROR"}, "Select one model and make it active first")
            return {"CANCELLED"}
        coll = _collection(context.scene, "CS_AssetLibrary")
        tree = _asset_tree(root)
        for obj in tree:
            for old in list(obj.users_collection):
                if old != coll:
                    old.objects.unlink(obj)
            if obj.name not in coll.objects:
                coll.objects.link(obj)
            obj.hide_render = True
            obj.hide_set(True)
        asset = s.assets.add()
        asset.name = root.name
        asset.root_object = root.name
        asset.category = s.asset_category
        s.asset_index = len(s.assets) - 1
        self.report({"INFO"}, f"Saved '{asset.name}' to the scene asset library")
        return {"FINISHED"}


class CARTOON_OT_vfx_capture(Operator):
    bl_idname = "cartoon.vfx_capture"
    bl_label = "Save Selection as VFX"
    bl_description = "Store the active effect object and its children as a reusable action-triggered effect"

    def execute(self, context):
        s = _S(context)
        root = context.view_layer.objects.active
        if root is None or root not in context.selected_objects:
            self.report({"ERROR"}, "Select an effect object and make it active first")
            return {"CANCELLED"}
        coll = _collection(context.scene, "CS_VFX_Library")
        for obj in _asset_tree(root):
            for old in list(obj.users_collection):
                if old != coll:
                    old.objects.unlink(obj)
            if obj.name not in coll.objects:
                coll.objects.link(obj)
            obj.hide_render = True
            obj.hide_set(True)
        item = s.vfx_items.add()
        item.name = root.name
        item.root_object = root.name
        s.vfx_index = len(s.vfx_items) - 1
        self.report({"INFO"}, f"Saved '{item.name}' to the VFX library; set its action match below")
        return {"FINISHED"}


class CARTOON_OT_vfx_remove(Operator):
    bl_idname = "cartoon.vfx_remove"
    bl_label = "Remove VFX Entry"

    def execute(self, context):
        s = _S(context)
        if 0 <= s.vfx_index < len(s.vfx_items):
            s.vfx_items.remove(s.vfx_index)
            s.vfx_index = max(0, min(s.vfx_index, len(s.vfx_items) - 1))
        return {"FINISHED"}


class CARTOON_OT_vfx_event_add(Operator):
    bl_idname = "cartoon.vfx_event_add"
    bl_label = "Add Timed VFX Event"
    bl_description = "Place a selected library effect on an exact shot-local frame and attach it to a character"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            self.report({"ERROR"}, "Choose a shot first")
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        event = sh.vfx_events.add()
        event.frame = max(1, min(sh.duration, context.scene.frame_current - (sh.start_frame or 1) + 1))
        if len(s.vfx_items):
            event.effect = s.vfx_items[min(max(0, s.vfx_index), len(s.vfx_items) - 1)].name
        cast = _shot_cast(sh)
        if cast:
            event.character = cast[0]["name"]
        sh.vfx_event_index = len(sh.vfx_events) - 1
        return {"FINISHED"}


class CARTOON_OT_vfx_event_remove(Operator):
    bl_idname = "cartoon.vfx_event_remove"
    bl_label = "Remove Timed VFX Event"
    event_index: IntProperty()

    def execute(self, context):
        s = _S(context)
        if 0 <= s.shot_index < len(s.shots):
            sh = s.shots[s.shot_index]
            if 0 <= self.event_index < len(sh.vfx_events):
                sh.vfx_events.remove(self.event_index)
                sh.vfx_event_index = max(0, min(self.event_index, len(sh.vfx_events) - 1))
        return {"FINISHED"}


class CARTOON_OT_asset_place(Operator):
    bl_idname = "cartoon.asset_place"
    bl_label = "Place Asset at Cursor"
    bl_description = "Add the selected library asset at the 3D cursor"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.asset_index < len(s.assets)):
            self.report({"ERROR"}, "Choose an asset first")
            return {"CANCELLED"}
        asset = s.assets[s.asset_index]
        try:
            _duplicate_asset(context.scene, asset, context.scene.cursor.location)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class CARTOON_OT_asset_scatter(Operator):
    bl_idname = "cartoon.asset_scatter"
    bl_label = "Scatter Assets"
    bl_description = "Scatter saved assets across the environment area with a repeatable seed"

    def execute(self, context):
        s = _S(context)
        assets = [a for a in s.assets if s.asset_filter == "ALL" or a.category == s.asset_filter]
        if not assets:
            self.report({"ERROR"}, "Save at least one matching model to the Asset Library")
            return {"CANCELLED"}
        placed = _smart_scatter(context.scene, s, assets, s.scatter_count, procedural=True)
        self.report({"INFO"}, f"Placed {placed} asset(s); road clearance and spacing were applied")
        return {"FINISHED"} if placed else {"CANCELLED"}


def _asset_radius(asset, scale):
    root = bpy.data.objects.get(asset.root_object)
    if root is None:
        return 0.5
    return max(0.35, max(float(root.dimensions.x), float(root.dimensions.y)) * 0.5 * scale)


def _smart_scatter(scene, settings, assets, count, procedural=False):
    """Deterministic placement with road clearance and spacing based on model bounds."""
    rng = random.Random(settings.environment_seed)
    width = max(2.0, float(settings.environment_width))
    road_half = max(0.5, float(settings.road_width) * 0.5)
    spacing = max(0.0, float(settings.asset_spacing))
    occupied = []
    for obj in scene.objects:
        if obj.get("cs_asset_instance_root") or obj.get("cs_world_obstacle"):
            radius = max(0.35, max(float(obj.dimensions.x), float(obj.dimensions.y)) * 0.5)
            occupied.append((float(obj.matrix_world.translation.x),
                             float(obj.matrix_world.translation.y), radius))
    placed = 0
    for _ in range(max(0, int(count))):
        asset = rng.choice(assets)
        scale = rng.uniform(0.8, 1.2)
        radius = _asset_radius(asset, scale)
        for _attempt in range(32):
            x = rng.uniform(-width * 0.48, width * 0.48)
            y = rng.uniform(-width * 0.48, width * 0.48)
            # Keep the street and the immediate character blocking area clear.
            if abs(y) < road_half + radius + 0.75:
                continue
            if any(math.hypot(x - ox, y - oy) < radius + other_radius + spacing
                   for ox, oy, other_radius in occupied):
                continue
            try:
                _duplicate_asset(scene, asset, (x, y, 0.0), rng.uniform(-math.pi, math.pi),
                                 scale, procedural=procedural)
                occupied.append((x, y, radius))
                placed += 1
            except Exception:
                pass
            break
    return placed


def _image_material(image):
    mat = bpy.data.materials.new("CS_Reference_Image")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    emission = nodes.new("ShaderNodeEmission")
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(tex.outputs["Color"], emission.inputs["Color"])
    links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def _make_image_plane(scene, image, kind, width):
    coll = _collection(scene, "CS_Environment")
    old = bpy.data.objects.get("CS_Environment_Reference")
    if old:
        bpy.data.objects.remove(old, do_unlink=True)
    aspect = image.size[0] / max(1, image.size[1])
    height = width / max(aspect, 0.01)
    if kind == "MAP":
        z = 0.015
        verts = [(-width/2, -height/2, z), (width/2, -height/2, z),
                 (width/2, height/2, z), (-width/2, height/2, z)]
    else:
        y = width / 2 + 0.05
        verts = [(-width/2, y, 0), (width/2, y, 0),
                 (width/2, y, height), (-width/2, y, height)]
    mesh = bpy.data.meshes.new("CS_Environment_Reference_Mesh")
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    uv = mesh.uv_layers.new(name="UVMap")
    for loop, coord in zip(mesh.polygons[0].loop_indices, ((0, 0), (1, 0), (1, 1), (0, 1))):
        uv.data[loop].uv = coord
    obj = bpy.data.objects.new("CS_Environment_Reference", mesh)
    coll.objects.link(obj)
    obj.data.materials.append(_image_material(image))
    obj.hide_render = True
    obj["cs_environment_generated"] = True
    return obj


class CARTOON_OT_environment_build(Operator):
    bl_idname = "cartoon.environment_build"
    bl_label = "Build Procedural World"
    bl_description = "Create an editable world layout with a road, mood lighting and smart placement of saved assets"

    def execute(self, context):
        s = _S(context)
        coll = _collection(context.scene, "CS_Environment")
        scene = context.scene
        # Rebuild only generated layout pieces; saved source assets and manually placed objects remain.
        for obj in list(scene.objects):
            if obj.get("cs_procedural_world"):
                bpy.data.objects.remove(obj, do_unlink=True)
        legacy_ground = bpy.data.objects.get("CS_Environment_Ground")
        if legacy_ground:
            bpy.data.objects.remove(legacy_ground, do_unlink=True)
        size = max(4.0, float(s.environment_width))
        road_w = min(size * 0.8, max(1.0, float(s.road_width)))
        ground_mat = _world_material("CS_World_Ground_Material", {
            "FOREST": (0.12, 0.25, 0.10, 1), "VILLAGE": (0.28, 0.34, 0.17, 1),
            "ROAD": (0.24, 0.30, 0.18, 1), "OPEN": (0.30, 0.36, 0.23, 1)
        }.get(s.environment_preset, (0.25, 0.32, 0.2, 1)))
        road_mat = _world_material("CS_World_Road_Material", (0.12, 0.13, 0.15, 1))
        stripe_mat = _world_material("CS_World_Road_Marking", (0.92, 0.78, 0.25, 1))
        ground = _world_box(coll, "CS_ProcWorld_Ground", (0, 0, -0.12), (size, size, 0.24), ground_mat)
        ground["cs_procedural_world"] = True
        road = _world_box(coll, "CS_ProcWorld_Road", (0, 0, 0.012), (size, road_w, 0.035), road_mat)
        road["cs_procedural_world"] = True
        hill_mat = _world_material("CS_World_Hill_Material", (0.19, 0.31, 0.14, 1))
        if s.environment_preset in {"FOREST", "VILLAGE"}:
            hill_sites = [(-0.40, -0.39), (0.40, -0.39), (-0.44, 0.36), (0.44, 0.36),
                          (-0.12, 0.43), (0.15, -0.43)]
            for hill_i, (hx, hy) in enumerate(hill_sites):
                radius = max(0.7, size * (0.035 + 0.006 * (hill_i % 3)))
                hill = _world_hill(coll, f"CS_ProcWorld_Hill_{hill_i + 1:02d}",
                                   (hx * size, hy * size, 0.0), radius,
                                   radius * (1.1 + 0.15 * (hill_i % 3)), hill_mat)
                hill["cs_procedural_world"] = True
                hill["cs_world_obstacle"] = True
        dash_len = max(0.5, min(2.0, size / 20.0))
        gap = dash_len * 0.8
        x = -size * 0.47
        index = 0
        while x < size * 0.47:
            mark = _world_box(coll, f"CS_ProcWorld_RoadMark_{index:03d}",
                              (x + dash_len * 0.5, 0, 0.035), (dash_len, 0.09, 0.012), stripe_mat)
            mark["cs_procedural_world"] = True
            x += dash_len + gap
            index += 1
        _apply_world_mood(scene, s.environment_mood)
        if s.environment_image and os.path.isfile(bpy.path.abspath(s.environment_image)):
            try:
                image = bpy.data.images.load(bpy.path.abspath(s.environment_image), check_existing=True)
                _make_image_plane(context.scene, image, s.reference_type, size)
            except Exception as exc:
                self.report({"WARNING"}, f"Ground created; reference image could not be loaded: {exc}")
        categories = {"FOREST": {"TREE"}, "VILLAGE": {"TREE", "BUILDING"},
                      "ROAD": {"TREE"}, "OPEN": set()}.get(s.environment_preset, set())
        assets = [a for a in s.assets if a.category in categories]
        auto_count = min(s.scatter_count, 250) if assets else 0
        placed = _smart_scatter(scene, s, assets, auto_count, procedural=True) if auto_count else 0
        if s.environment_image and os.path.isfile(bpy.path.abspath(s.environment_image)):
            self.report({"INFO"}, f"World built: {s.environment_preset.title()}, {placed} saved asset(s), map/photo shown as a guide")
        else:
            self.report({"INFO"}, f"World built: {s.environment_preset.title()}, {placed} saved asset(s); add your models in the Asset Library")
        return {"FINISHED"}


def _world_material(name, color):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.diffuse_color = color
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = color
        bsdf.inputs["Roughness"].default_value = 0.9
    return mat


def _world_box(coll, name, location, dimensions, material):
    hx, hy, hz = (float(v) * 0.5 for v in dimensions)
    verts = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
             (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5),
             (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh = bpy.data.meshes.new(name + "_Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    coll.objects.link(obj)
    obj.location = location
    return obj


def _world_hill(coll, name, location, radius, height, material):
    segments = 10
    rings = ((1.0, 0.0), (0.72, height * 0.42), (0.34, height * 0.78))
    verts = []
    for ring_radius, z in rings:
        for i in range(segments):
            angle = 2.0 * math.pi * i / segments
            wobble = 1.0 + 0.07 * math.sin(i * 3.7 + len(name))
            verts.append((radius * ring_radius * wobble * math.cos(angle),
                          radius * ring_radius * wobble * math.sin(angle), z))
    top = len(verts)
    verts.append((0.0, 0.0, height))
    faces = []
    for ring in range(len(rings) - 1):
        for i in range(segments):
            a = ring * segments + i
            b = ring * segments + (i + 1) % segments
            c = (ring + 1) * segments + (i + 1) % segments
            d = (ring + 1) * segments + i
            faces.append((a, b, c, d))
    offset = (len(rings) - 1) * segments
    for i in range(segments):
        faces.append((offset + i, offset + (i + 1) % segments, top))
    mesh = bpy.data.meshes.new(name + "_Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    coll.objects.link(obj)
    obj.location = location
    return obj


def _apply_world_mood(scene, mood):
    world = scene.world or bpy.data.worlds.new("CS_World")
    if not world.get("cs_mood_copy"):
        world = world.copy()
        world.name = "CS_World_Mood"
        world["cs_mood_copy"] = True
    scene.world = world
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    output = next((node for node in nodes if node.type == "OUTPUT_WORLD"), None)
    if output is None:
        output = nodes.new("ShaderNodeOutputWorld")
    background = next((node for node in nodes if node.type == "BACKGROUND"), None)
    if background is None:
        background = nodes.new("ShaderNodeBackground")
    palettes = {
        "DAY": ((0.28, 0.43, 0.68, 1), 0.8, 0.0, (1.0, 0.94, 0.78)),
        "SUNSET": ((0.55, 0.22, 0.12, 1), 0.6, 0.0, (1.0, 0.45, 0.18)),
        "NIGHT": ((0.015, 0.025, 0.075, 1), 0.22, 0.0, (0.22, 0.32, 0.8)),
        "FOG": ((0.35, 0.42, 0.48, 1), 0.5, 0.006, (0.65, 0.72, 0.8)),
    }
    color, strength, fog_density, sun_color = palettes.get(mood, palettes["DAY"])
    background.inputs["Color"].default_value = color
    background.inputs["Strength"].default_value = strength
    if not output.inputs["Surface"].is_linked or output.inputs["Surface"].links[0].from_node != background:
        links.new(background.outputs["Background"], output.inputs["Surface"])
    atmosphere = nodes.get("CS_Atmosphere")
    if fog_density > 0:
        volume = atmosphere or nodes.new("ShaderNodeVolumePrincipled")
        volume.name = "CS_Atmosphere"
        volume.inputs["Color"].default_value = (0.68, 0.74, 0.82, 1)
        volume.inputs["Density"].default_value = fog_density
        connected = output.inputs["Volume"].links
        if not connected or connected[0].from_node == volume:
            links.new(volume.outputs["Volume"], output.inputs["Volume"])
    elif atmosphere:
        for link in list(atmosphere.outputs["Volume"].links):
            links.remove(link)
        nodes.remove(atmosphere)
    sun = bpy.data.objects.get("CS_ProcWorld_Sun")
    if sun is None:
        data = bpy.data.lights.new("CS_ProcWorld_Sun", "SUN")
        sun = bpy.data.objects.new("CS_ProcWorld_Sun", data)
        _collection(scene, "CS_Environment").objects.link(sun)
        sun["cs_procedural_world"] = True
    sun.data.color = sun_color
    sun.data.energy = {"DAY": 2.0, "SUNSET": 1.5, "NIGHT": 0.45, "FOG": 1.0}.get(mood, 2.0)
    sun.rotation_euler = (math.radians(35 if mood != "NIGHT" else 65), 0, math.radians(-35))


def _syllables(text):
    words = re.findall(r"[^\s]+", text)
    out = []
    devanagari_vowels = set("अआइईउऊऋॠऌएऐओऔािीुूृॄॅेैॉोौ")
    for word in words:
        if any("\u0900" <= c <= "\u097f" for c in word):
            count = sum(c in devanagari_vowels for c in word)
        else:
            groups = re.findall(r"[aeiouy]+", word.lower())
            count = len(groups)
        out.extend([word] * max(1, count))
    return out


class CARTOON_OT_text_lipsync(Operator):
    bl_idname = "cartoon.text_lipsync"
    bl_label = "Generate Mouth Motion from Dialogue"
    bl_description = "Keyframe existing MouthOpen/JawOpen shape keys from dialogue text only; no audio is used"

    def execute(self, context):
        s = _S(context)
        if not (0 <= s.shot_index < len(s.shots)):
            self.report({"ERROR"}, "Choose a shot first")
            return {"CANCELLED"}
        sh = s.shots[s.shot_index]
        syllables = _syllables(sh.dialogue)
        if not syllables:
            self.report({"ERROR"}, "Add dialogue text to the selected shot first")
            return {"CANCELLED"}
        start = sh.start_frame or 1
        end = sh.end_frame or (start + sh.duration - 1)
        cast = _shot_cast(sh)
        keyed, skipped = 0, 0
        for member in cast:
            char = s.characters.get(member["name"])
            arm = bpy.data.objects.get(char.armature_name) if char else None
            meshes = [obj for obj in bpy.data.objects if obj.type == "MESH" and
                      (obj == arm or (arm and obj in arm.children_recursive))]
            shape_keys = [mesh.data.shape_keys for mesh in meshes if mesh.data.shape_keys]
            target = None
            for keys in shape_keys:
                target = next((key for key in keys.key_blocks
                               if re.sub(r"[^a-z]", "", key.name.lower()) in
                               {"mouthopen", "jawopen", "visemeaa", "moutha"}), None)
                if target:
                    break
            if target is None:
                skipped += 1
                continue
            data = target.id_data
            path = target.path_from_id("value")
            data.keyframe_insert(data_path=path, frame=start, group="Dialogue Mouth Motion")
            target.value = 0.0
            data.keyframe_insert(data_path=path, frame=start, group="Dialogue Mouth Motion")
            span = max(1, end - start)
            total = len(syllables)
            for i in range(total):
                center = start + int((i + 0.5) * span / total)
                target.value = 0.0
                data.keyframe_insert(data_path=path, frame=max(start, center - 2), group="Dialogue Mouth Motion")
                target.value = 0.8
                data.keyframe_insert(data_path=path, frame=center, group="Dialogue Mouth Motion")
                target.value = 0.0
                data.keyframe_insert(data_path=path, frame=min(end, center + 2), group="Dialogue Mouth Motion")
            keyed += 1
        if keyed:
            self.report({"INFO"}, f"Generated text-driven mouth motion for {keyed} character(s); {skipped} had no supported mouth shape key")
            return {"FINISHED"}
        self.report({"WARNING"}, "No cast character has a MouthOpen, JawOpen, VisemeAA, or MouthA shape key")
        return {"CANCELLED"}


class CARTOON_OT_mouth_shape_from_selection(Operator):
    bl_idname = "cartoon.mouth_shape_from_selection"
    bl_label = "Create Open Mouth Shape"
    bl_description = "Expand selected mouth vertices into an open-mouth shape key; use a separate mouth mesh or select only mouth vertices"

    @classmethod
    def poll(cls, context):
        return context.view_layer.objects.active is not None and context.view_layer.objects.active.type == "MESH"

    def execute(self, context):
        import bmesh
        obj = context.view_layer.objects.active
        if obj.mode != "EDIT":
            self.report({"ERROR"}, "Enter Edit Mode and select only the mouth vertices first")
            return {"CANCELLED"}
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        selected = [v.index for v in bm.verts if v.select]
        if not selected:
            self.report({"ERROR"}, "Select the closed-mouth vertices in Edit Mode")
            return {"CANCELLED"}
        axis = {"X": 0, "Y": 1, "Z": 2}[_S(context).mouth_open_axis]
        pivot = sum(bm.verts[i].co[axis] for i in selected) / len(selected)
        bpy.ops.object.mode_set(mode="OBJECT")
        if obj.data.shape_keys is None:
            obj.shape_key_add(name="Basis", from_mix=False)
        key = obj.data.shape_keys.key_blocks.get(_S(context).mouth_shape_key)
        if key is None:
            key = obj.shape_key_add(name=_S(context).mouth_shape_key, from_mix=False)
        factor = _S(context).mouth_open_factor
        for i in selected:
            point = key.data[i].co
            point[axis] = pivot + (point[axis] - pivot) * factor
        self.report({"INFO"}, f"Created '{key.name}'. If the mouth opens sideways, edit the shape key along another local axis.")
        return {"FINISHED"}


class CARTOON_OT_mouth_cycle(Operator):
    bl_idname = "cartoon.mouth_cycle"
    bl_label = "Animate Open / Close"
    bl_description = "Alternate the mouth shape key between closed and open over the selected frame range"

    @classmethod
    def poll(cls, context):
        obj = context.view_layer.objects.active
        return obj is not None and obj.type == "MESH" and obj.data.shape_keys is not None

    def execute(self, context):
        s = _S(context)
        obj = context.view_layer.objects.active
        keys = obj.data.shape_keys
        target = keys.key_blocks.get(s.mouth_shape_key)
        if target is None:
            target = next((key for key in keys.key_blocks if key.name.lower() in
                           {"mouthopen", "jawopen", "visemeaa", "moutha"}), None)
        if target is None:
            self.report({"ERROR"}, "Create the open-mouth shape key first or choose an existing mouth key name")
            return {"CANCELLED"}
        start, end = s.mouth_start, max(s.mouth_start, s.mouth_end)
        cycle = max(2, s.mouth_cycle)
        open_frames = min(max(1, s.mouth_open_frames), cycle - 1)
        data_path = target.path_from_id("value")
        target.value = 0.0
        keys.keyframe_insert(data_path=data_path, frame=start, group="Mouth Open Close")
        frame = start + 1
        while frame <= end:
            target.value = 1.0
            keys.keyframe_insert(data_path=data_path, frame=frame, group="Mouth Open Close")
            close_frame = min(end, frame + open_frames)
            target.value = 0.0
            keys.keyframe_insert(data_path=data_path, frame=close_frame, group="Mouth Open Close")
            frame += cycle
        if keys.animation_data and keys.animation_data.action:
            try:
                for fc in keys.animation_data.action.fcurves:
                    if fc.data_path == data_path:
                        for point in fc.keyframe_points:
                            if start <= point.co.x <= end:
                                point.interpolation = "CONSTANT"
            except Exception:
                pass
        self.report({"INFO"}, f"Animated '{target.name}' from frame {start} to {end}")
        return {"FINISHED"}


class CARTOON_OT_prepare_hybrid(Operator):
    bl_idname = "cartoon.prepare_hybrid"
    bl_label = "Prepare Hybrid Episode"
    bl_description = "Create shots, assign actions, build cameras and run checks, then leave the project ready for manual review"

    def execute(self, context):
        s = _S(context)
        if not s.project_dir:
            self.report({"ERROR"}, "Set the Project Folder first")
            return {"CANCELLED"}
        if not s.shots and s.story_text:
            result = bpy.ops.cartoon.story_to_shots("EXEC_DEFAULT")
            if "CANCELLED" in result:
                return {"CANCELLED"}
        if not s.shots:
            self.report({"ERROR"}, "Add shots or choose a story Text block first")
            return {"CANCELLED"}
        _auto_assign(s)
        warnings = build_episode(context)
        bpy.ops.cartoon.run_checks()
        self.report({"WARNING" if warnings else "INFO"},
                    f"Hybrid setup ready: {len(s.shots)} shot(s) built for review. Rendering was not started.")
        return {"FINISHED"}


class CARTOON_PT_assets_environment(Panel):
    bl_label = "Assets and Environment"
    bl_idname = "CARTOON_PT_assets_environment"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        col = self.layout.column(align=True)
        col.label(text="Import a model in Blender, select it, then save it here")
        col.prop(s, "asset_category")
        col.operator("cartoon.asset_capture", icon="ASSET_MANAGER")
        row = col.row()
        row.template_list("CARTOON_UL_assets", "", s, "assets", s, "asset_index", rows=3)
        row.column(align=True).operator("cartoon.asset_place", icon="EMPTY_AXIS", text="Place")
        col.prop(s, "environment_width")
        col.prop(s, "environment_preset")
        col.prop(s, "environment_mood")
        col.prop(s, "road_width")
        col.prop(s, "asset_spacing")
        col.prop(s, "environment_image")
        col.prop(s, "reference_type")
        col.label(text="Map/photo guides help layout; they do not reconstruct exact 3D automatically.", icon="INFO")
        col.operator("cartoon.environment_build", text="Build / Rebuild World", icon="WORLD_DATA")
        col.prop(s, "asset_filter")
        col.prop(s, "scatter_count")
        col.prop(s, "environment_seed")
        col.operator("cartoon.asset_scatter", icon="PARTICLES")


class CARTOON_PT_vfx_library(Panel):
    bl_label = "Action VFX Library"
    bl_idname = "CARTOON_PT_vfx_library"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        col = self.layout.column(align=True)
        col.label(text="Select an effect object or particle rig and save it here.")
        col.operator("cartoon.vfx_capture", icon="PARTICLES")
        row = col.row()
        row.template_list("CARTOON_UL_vfx", "", s, "vfx_items", s, "vfx_index", rows=3)
        row.column(align=True).operator("cartoon.vfx_remove", icon="REMOVE", text="")
        if 0 <= s.vfx_index < len(s.vfx_items):
            item = s.vfx_items[s.vfx_index]
            box = col.box()
            box.prop(item, "name")
            box.prop(item, "action_match")
            box.label(text="Matches text inside an animation name.")
            box.prop(item, "anchor")
            row = box.row(align=True)
            row.prop(item, "offset_x")
            row.prop(item, "offset_y")
            row.prop(item, "offset_z")
            box.prop(item, "rotation_z")
            box.prop(item, "scale")
            box.label(text="Effects appear only during matching action frames.")
        col.separator()
        col.label(text="Exact shot events", icon="TIME")
        if 0 <= s.shot_index < len(s.shots):
            sh = s.shots[s.shot_index]
            col.operator("cartoon.vfx_event_add", icon="ADD")
            for i, event in enumerate(sh.vfx_events):
                box = col.box()
                row = box.row(align=True)
                row.prop_search(event, "effect", s, "vfx_items", text="Effect")
                row.prop(event, "frame", text="Frame")
                op = row.operator("cartoon.vfx_event_remove", icon="X", text="")
                op.event_index = i
                box.prop_search(event, "character", s, "characters", text="Attach to")
                box.prop(event, "duration")
                row = box.row(align=True)
                row.prop(event, "offset_x")
                row.prop(event, "offset_y")
                row.prop(event, "offset_z")
                row.prop(event, "scale")
            col.label(text="VFX events attach to the chosen character at the chosen frame.", icon="INFO")


class CARTOON_PT_dialogue_motion(Panel):
    bl_label = "Dialogue Motion"
    bl_idname = "CARTOON_PT_dialogue_motion"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_shots"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = _S(context)
        col = self.layout.column(align=True)
        col.label(text="Text timing uses existing mouth shape keys; no audio input.")
        col.operator("cartoon.text_lipsync", icon="OUTLINER_OB_ARMATURE")
        col.separator()
        box = col.box()
        box.label(text="Simple rectangle mouth", icon="MESH_CUBE")
        box.label(text="Select only the mouth vertices in Edit Mode, then create an open shape.")
        box.prop(s, "mouth_shape_key")
        box.prop(s, "mouth_open_axis")
        box.prop(s, "mouth_open_factor")
        box.operator("cartoon.mouth_shape_from_selection", icon="SHAPEKEY_DATA")
        box.label(text="With the mouth mesh selected, set frames and alternate open / closed.")
        row = box.row(align=True)
        row.prop(s, "mouth_start")
        row.prop(s, "mouth_end")
        row = box.row(align=True)
        row.prop(s, "mouth_cycle")
        row.prop(s, "mouth_open_frames")
        box.operator("cartoon.mouth_cycle", icon="ACTION")


class CARTOON_PT_hybrid(Panel):
    bl_label = "Hybrid Workflow"
    bl_idname = "CARTOON_PT_hybrid"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cartoon Studio"
    bl_parent_id = "CARTOON_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.layout.label(text="Build automatically, review and adjust before rendering")
        self.layout.operator("cartoon.prepare_hybrid", icon="FILE_TICK")


classes = (CARTOON_UL_assets, CARTOON_UL_vfx, CARTOON_OT_asset_capture, CARTOON_OT_vfx_capture,
           CARTOON_OT_vfx_remove, CARTOON_OT_vfx_event_add, CARTOON_OT_vfx_event_remove,
           CARTOON_OT_asset_place,
           CARTOON_OT_asset_scatter, CARTOON_OT_environment_build,
           CARTOON_OT_text_lipsync, CARTOON_OT_mouth_shape_from_selection,
           CARTOON_OT_mouth_cycle, CARTOON_OT_prepare_hybrid,
           CARTOON_PT_assets_environment, CARTOON_PT_vfx_library,
           CARTOON_PT_dialogue_motion, CARTOON_PT_hybrid)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
