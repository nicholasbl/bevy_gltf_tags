bl_info = {
    "name": "Generic Object Tags",
    "author": "NBL",
    "version": (0, 3, 2),
    "blender": (5, 0, 0),
    "location": "Object Properties / Mesh Data Properties > Tags",
    "description": "Attach generic string tags and string-valued tags to Blender objects and meshes",
    "category": "Object",
}

"""
Generic Object Tags
===================

This add-on is intentionally small glue between Blender-authored data and Bevy
runtime behavior.

The core idea is:

* Artists/designers add simple string tags in Blender.
* Blender exports those tags as glTF `extras`.
* Bevy reads node `GltfExtras` and mesh `GltfMeshExtras`, then invokes
  user-registered Rust callbacks.

The add-on stores tags in Blender custom properties under the key `tags`.
Blender's glTF exporter can copy custom properties into glTF `extras`, but only
when the user enables the exporter option usually shown as:

    Include > Custom Properties

That setting is easy to miss. Blender's exporter settings are not reliably
script-editable across its saved-exporter and per-collection exporter paths, so
the UI warns users rather than pretending it can fix every exporter for them.

Tag Storage Format
------------------

Each tagged Blender ID block gets:

    data_block["tags"] = "[\"some/key\", \"some/key=value\"]"

This is a JSON-encoded string array rather than a native Blender collection
property for two pragmatic reasons:

* custom properties export reliably to glTF `extras`;
* the Bevy side can support both this string form and a direct JSON array form.

Tags are either flags:

    physics/collider

or string-valued tags:

    physics/body=static

The first `=` separates key from value. Values may contain additional `=`
characters. Keys may not.

Object Tags vs Mesh Tags
------------------------

Blender users often think of a mesh object as "the thing", but Bevy's glTF scene
spawner commonly produces a semantic node entity and a rendered mesh/material
entity below it. That means there are two useful places to tag:

* Object Properties > Tags:
  Tags the Blender object / glTF node. Use this for semantic gameplay objects,
  spawn points, interaction targets, grouping markers, etc.

* Mesh Data Properties > Tags:
  Tags the mesh data-block / glTF mesh. Use this when the Bevy callback should
  act on the rendered mesh entity, such as adding mesh-derived colliders.

The add-on deliberately keeps both panels using the same generic tag format.
There is no Bevy-specific component schema here.
"""

import bpy
import json
from collections import Counter
from bpy.props import EnumProperty, StringProperty


OBJECT_TAGS_KEY = "tags"
SCENE_LIBRARY_KEY = "_object_tag_library"
_ENUM_CACHE = {
    "tags": [],
    "keys": [],
    "values": [],
    "objects": [],
}


# -----------------------------------------------------------------------------
# Storage
# -----------------------------------------------------------------------------

def _decode_string_list(value):
    """Decode a custom-property value that should contain a JSON string array.

    Blender custom properties are permissive, and old files or manual edits can
    leave surprising values behind. This helper is intentionally forgiving:
    malformed input simply behaves as "no tags" instead of breaking the panel.
    """
    if not isinstance(value, str):
        return []

    try:
        value = json.loads(value)
    except Exception:
        return []

    if not isinstance(value, list):
        return []

    return [x for x in value if isinstance(x, str) and x]


def get_tags(data_block):
    """Return valid non-empty string tags from an object, mesh, or scene-like ID."""
    return _decode_string_list(data_block.get(OBJECT_TAGS_KEY, "[]"))


def set_tags(data_block, tags):
    """Write tags back in deterministic form.

    Sorting and de-duplicating keeps exported glTF diffs stable. It also makes
    multi-selection display predictable, which matters once users build a shared
    tag database.
    """
    data_block[OBJECT_TAGS_KEY] = json.dumps(
        sorted(set(tags)),
        separators=(",", ":"),
    )


def get_library(scene):
    """Return all database tags plus tags currently authored in the file.

    The scene keeps an explicit database so a team can seed shared tags before
    any object uses them. We also scan object and mesh data-blocks so tags typed
    by hand are automatically remembered and suggested.
    """
    tags = set(_decode_string_list(scene.get(SCENE_LIBRARY_KEY, "[]")))

    for obj in bpy.data.objects:
        tags.update(get_tags(obj))

    for mesh in bpy.data.meshes:
        tags.update(get_tags(mesh))

    return sorted(tags)


def set_library(scene, tags):
    """Persist the tag database on the scene as a compact JSON string."""
    scene[SCENE_LIBRARY_KEY] = json.dumps(
        sorted(set(tags)),
        separators=(",", ":"),
    )


def remember_tag(scene, tag):
    """Add one tag to the scene database without losing existing entries."""
    library = set(get_library(scene))
    library.add(tag)
    set_library(scene, library)


def library_keys(scene):
    """Return unique keys from the tag database and currently authored tags."""
    return sorted({split_tag(tag)[0] for tag in get_library(scene) if split_tag(tag)[0]})


def library_values(scene, key=None):
    """Return unique values from the tag database.

    If `key` is provided, only values previously used with that key are returned.
    If no key is provided, values from every valued tag are returned. Empty/flag
    values are excluded because choosing "no value" is already possible by
    leaving the value text field empty.
    """
    values = set()

    for tag in get_library(scene):
        tag_key, value = split_tag(tag)

        if value is None:
            continue

        if key and tag_key != key:
            continue

        values.add(value)

    return sorted(values)


# -----------------------------------------------------------------------------
# Tag format
# -----------------------------------------------------------------------------

def split_tag(tag):
    """Split `key=value` into `(key, value)` while allowing `=` in the value."""
    if "=" in tag:
        key, value = tag.split("=", 1)
        return key, value
    return tag, None


def make_tag(key, value):
    """Validate UI input and convert it into the exported tag string format."""
    key = key.strip()
    value = value.strip()

    if not key:
        raise ValueError("Tag key cannot be empty")

    if "=" in key:
        raise ValueError("'=' is reserved and cannot appear in tag keys")

    return f"{key}={value}" if value else key


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def selected_objects(context):
    """Return selected Blender objects as a list for reuse by operators/panels."""
    return list(context.selected_objects)


def active_mesh(context):
    """Return the active object's mesh data-block, if the active object is a mesh."""
    obj = context.object
    if obj is None or obj.type != 'MESH':
        return None
    return obj.data


def selected_meshes(context):
    """Return unique mesh data-blocks referenced by the selected mesh objects.

    Multiple Blender objects may share the same mesh data-block. In the Mesh Data
    panel, adding a tag to the data-block should happen once, not once per object
    instance, so this function de-duplicates by Blender's runtime pointer.
    """
    meshes = []
    seen = set()

    for obj in selected_objects(context):
        if obj.type != 'MESH' or obj.data is None:
            continue

        pointer = obj.data.as_pointer()
        if pointer in seen:
            continue

        seen.add(pointer)
        meshes.append(obj.data)

    return meshes


def mesh_tab_targets(context):
    """Return the mesh data-blocks affected by Mesh Data panel operations.

    If several mesh objects are selected, the operation applies to each unique
    selected mesh data-block. If nothing useful is selected but the active object
    is a mesh, fall back to the active mesh so the panel still behaves naturally.
    """
    meshes = selected_meshes(context)

    if meshes:
        return meshes

    mesh = active_mesh(context)
    return [mesh] if mesh else []


def add_tag_to_data_blocks(data_blocks, tag):
    """Add or replace a tag on each supplied data-block.

    Tags are unique by key, not by full string. This makes valued tags pleasant:
    adding `physics/body=dynamic` replaces `physics/body=static`, while unrelated
    tags such as `interaction/clickable` remain in place.
    """
    key, _ = split_tag(tag)

    for data_block in data_blocks:
        tags = get_tags(data_block)

        # A key is unique per data block. Adding key=value replaces any existing
        # entry with the same key, while unrelated tags remain untouched.
        tags = [existing for existing in tags if split_tag(existing)[0] != key]
        tags.append(tag)
        set_tags(data_block, tags)


def remove_tag_key_from_data_blocks(data_blocks, key):
    """Remove every tag with the given key from each supplied data-block."""
    for data_block in data_blocks:
        tags = [
            existing
            for existing in get_tags(data_block)
            if split_tag(existing)[0] != key
        ]
        set_tags(data_block, tags)


def tag_rows(data_blocks):
    """Summarize tag values across a selection for UI display.

    Return shape:

        key -> Counter({value_or_None: data_block_count})

    The count lets the panel show three useful states with one representation:

    * present on every selected item with the same value;
    * present on only some selected items;
    * present with mixed values across selected items.
    """
    rows = {}

    for data_block in data_blocks:
        seen = set()
        for raw in get_tags(data_block):
            key, value = split_tag(raw)

            # Avoid double-counting malformed duplicate keys on one object.
            if key in seen:
                continue
            seen.add(key)

            rows.setdefault(key, Counter())[value] += 1

    return rows


def format_tag(tag):
    """Human-facing display for a raw tag string."""
    key, value = split_tag(tag)
    return f"{key} = {value}" if value is not None else key


def window_manager_tag_fields(context, target):
    """Return the key/value text properties used by an authoring area."""
    if target == "library":
        return "object_tag_library_key", "object_tag_library_value"

    return "object_tag_key", "object_tag_value"


def enum_tag_items(self, context):
    """Build the dynamic search-popup contents for "Reuse Tag".

    Blender's EnumProperty callback can retain references to returned strings.
    Keeping the tuples in `_ENUM_CACHE` avoids dangling temporary values in the
    UI callback machinery.
    """
    global _ENUM_CACHE

    if context is None or context.scene is None:
        _ENUM_CACHE["tags"] = [("__NONE__", "(No tags yet)", "", 0)]
        return _ENUM_CACHE["tags"]

    tags = get_library(context.scene)

    if not tags:
        _ENUM_CACHE["tags"] = [("__NONE__", "(No tags yet)", "", 0)]
        return _ENUM_CACHE["tags"]

    _ENUM_CACHE["tags"] = [
        (tag, format_tag(tag), f'Reuse "{tag}"', i)
        for i, tag in enumerate(tags)
    ]
    return _ENUM_CACHE["tags"]


def enum_key_items(self, context):
    """Build key suggestions for the key text field search popup."""
    global _ENUM_CACHE

    if context is None or context.scene is None:
        _ENUM_CACHE["keys"] = [("__NONE__", "(No tag keys yet)", "", 0)]
        return _ENUM_CACHE["keys"]

    keys = library_keys(context.scene)

    if not keys:
        _ENUM_CACHE["keys"] = [("__NONE__", "(No tag keys yet)", "", 0)]
        return _ENUM_CACHE["keys"]

    _ENUM_CACHE["keys"] = [
        (key, key, f'Use tag key "{key}"', i)
        for i, key in enumerate(keys)
    ]
    return _ENUM_CACHE["keys"]


def enum_value_items(self, context):
    """Build value suggestions for the value text field search popup."""
    global _ENUM_CACHE

    if context is None or context.scene is None:
        _ENUM_CACHE["values"] = [("__NONE__", "(No tag values yet)", "", 0)]
        return _ENUM_CACHE["values"]

    key_attr, _ = window_manager_tag_fields(context, getattr(self, "target", "tag"))
    key = getattr(context.window_manager, key_attr, "").strip()
    values = library_values(context.scene, key=key) or library_values(context.scene)

    if not values:
        _ENUM_CACHE["values"] = [("__NONE__", "(No tag values yet)", "", 0)]
        return _ENUM_CACHE["values"]

    _ENUM_CACHE["values"] = [
        (value, value, f'Use tag value "{value}"', i)
        for i, value in enumerate(values)
    ]
    return _ENUM_CACHE["values"]


def enum_object_items(self, context):
    """Build object-name suggestions for value fields.

    Object names are not globally unique once a glTF is instantiated many times,
    but they are still useful as local pairing labels when source/target tags use
    the same value inside one tagged scene action.
    """
    global _ENUM_CACHE

    if context is None:
        _ENUM_CACHE["objects"] = [("__NONE__", "(No objects)", "", 0)]
        return _ENUM_CACHE["objects"]

    names = sorted({obj.name for obj in bpy.data.objects if obj.name})

    if not names:
        _ENUM_CACHE["objects"] = [("__NONE__", "(No objects)", "", 0)]
        return _ENUM_CACHE["objects"]

    _ENUM_CACHE["objects"] = [
        (name, name, f'Use object name "{name}" as tag value', i)
        for i, name in enumerate(names)
    ]
    return _ENUM_CACHE["objects"]


def draw_export_warning(layout):
    """Warn users about the glTF exporter option this add-on depends on.

    Tags are just Blender custom properties. They only reach Bevy if every GLB
    export path, including collection exporters, has custom properties/extras
    enabled. We keep this as a warning instead of trying to edit Blender's saved
    exporter state because those settings are not consistently scriptable.
    """
    box = layout.box()
    box.alert = True
    box.label(text="Enable glTF Custom Properties", icon='ERROR')
    box.label(text="Export dialog: Include > Custom Properties")
    box.label(text="Also enable it on collection exporters")


def draw_tag_composer(layout, context, target, add_operator, add_text, label="New Tag"):
    """Draw editable key/value inputs with database and object-name search.

    Blender does not have a native "editable combo box" property. The add-on
    keeps plain text fields as the source of truth, then places search-popup
    buttons beside them. Choosing a suggestion fills the text field; typing an
    unknown key or value still works exactly like a normal text input.
    """
    key_attr, value_attr = window_manager_tag_fields(context, target)

    box = layout.box()
    box.label(text=label)

    row = box.row(align=True)
    row.prop(context.window_manager, key_attr, text="Key")
    op = row.operator("object.tag_fill_key", text="", icon='VIEWZOOM')
    op.target = target

    row = box.row(align=True)
    row.prop(context.window_manager, value_attr, text="Value")
    op = row.operator("object.tag_fill_value", text="", icon='VIEWZOOM')
    op.target = target
    op = row.operator("object.tag_fill_value_from_object", text="", icon='OBJECT_DATA')
    op.target = target

    box.operator(add_operator, text=add_text, icon='ADD')


# -----------------------------------------------------------------------------
# Operators
# -----------------------------------------------------------------------------

class OBJECT_OT_tag_add_known(bpy.types.Operator):
    """Reuse a complete database tag on all selected Blender objects."""

    bl_idname = "object.tag_add_known"
    bl_label = "Reuse Tag"
    bl_description = "Search complete tags and reuse one on all selected objects"
    bl_property = "tag"

    tag: EnumProperty(
        name="Tag",
        items=enum_tag_items,
    )

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def invoke(self, context, event):
        if not get_library(context.scene):
            self.report({'INFO'}, "No database tags yet")
            return {'CANCELLED'}

        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.tag == "__NONE__":
            return {'CANCELLED'}

        objects = selected_objects(context)
        add_tag_to_data_blocks(objects, self.tag)
        remember_tag(context.scene, self.tag)

        self.report({'INFO'}, f'Reused "{self.tag}" on {len(objects)} object(s)')
        return {'FINISHED'}


class OBJECT_OT_tag_add_new(bpy.types.Operator):
    """Create a new object tag from the text fields and apply it to selection."""

    bl_idname = "object.tag_add_new"
    bl_label = "Add Tag"
    bl_description = "Add this tag to all selected objects"

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        wm = context.window_manager

        try:
            tag = make_tag(wm.object_tag_key, wm.object_tag_value)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

        objects = selected_objects(context)
        add_tag_to_data_blocks(objects, tag)
        remember_tag(context.scene, tag)

        wm.object_tag_key = ""
        wm.object_tag_value = ""

        self.report({'INFO'}, f'Added "{tag}" to {len(objects)} object(s)')
        return {'FINISHED'}


class OBJECT_OT_tag_remove_key(bpy.types.Operator):
    """Remove one object tag key from all selected objects."""

    bl_idname = "object.tag_remove_key"
    bl_label = "Remove Tag"
    bl_description = "Remove this tag key from all selected objects"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        objects = selected_objects(context)
        remove_tag_key_from_data_blocks(objects, self.key)

        self.report({'INFO'}, f'Removed "{self.key}" from {len(objects)} object(s)')
        return {'FINISHED'}


class OBJECT_OT_tag_fill_key(bpy.types.Operator):
    """Fill a key text field from the tag database."""

    bl_idname = "object.tag_fill_key"
    bl_label = "Search Tag Key"
    bl_description = "Search tag keys and copy one into the key field"
    bl_property = "key"

    target: StringProperty(default="tag")
    key: EnumProperty(
        name="Key",
        items=enum_key_items,
    )

    def invoke(self, context, event):
        if not library_keys(context.scene):
            self.report({'INFO'}, "No tag keys yet")
            return {'CANCELLED'}

        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.key == "__NONE__":
            return {'CANCELLED'}

        key_attr, _ = window_manager_tag_fields(context, self.target)
        setattr(context.window_manager, key_attr, self.key)
        return {'FINISHED'}


class OBJECT_OT_tag_fill_value(bpy.types.Operator):
    """Fill a value text field from values already present in the tag database."""

    bl_idname = "object.tag_fill_value"
    bl_label = "Search Tag Value"
    bl_description = "Search tag values and copy one into the value field"
    bl_property = "value"

    target: StringProperty(default="tag")
    value: EnumProperty(
        name="Value",
        items=enum_value_items,
    )

    def invoke(self, context, event):
        key_attr, _ = window_manager_tag_fields(context, self.target)
        key = getattr(context.window_manager, key_attr, "").strip()

        if not (library_values(context.scene, key=key) or library_values(context.scene)):
            self.report({'INFO'}, "No tag values yet")
            return {'CANCELLED'}

        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.value == "__NONE__":
            return {'CANCELLED'}

        _, value_attr = window_manager_tag_fields(context, self.target)
        setattr(context.window_manager, value_attr, self.value)
        return {'FINISHED'}


class OBJECT_OT_tag_fill_value_from_object(bpy.types.Operator):
    """Fill a value text field from a Blender object name."""

    bl_idname = "object.tag_fill_value_from_object"
    bl_label = "Use Object Name"
    bl_description = "Search object names and copy one into the value field"
    bl_property = "object_name"

    target: StringProperty(default="tag")
    object_name: EnumProperty(
        name="Object",
        items=enum_object_items,
    )

    def invoke(self, context, event):
        if not bpy.data.objects:
            self.report({'INFO'}, "No objects in this file")
            return {'CANCELLED'}

        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.object_name == "__NONE__":
            return {'CANCELLED'}

        _, value_attr = window_manager_tag_fields(context, self.target)
        setattr(context.window_manager, value_attr, self.object_name)
        return {'FINISHED'}


class MESH_OT_tag_add_known(bpy.types.Operator):
    """Reuse a complete database tag on selected mesh data-blocks."""

    bl_idname = "mesh.tag_add_known"
    bl_label = "Reuse Tag"
    bl_description = "Search complete tags and reuse one on selected mesh data"
    bl_property = "tag"

    tag: EnumProperty(
        name="Tag",
        items=enum_tag_items,
    )

    @classmethod
    def poll(cls, context):
        return bool(mesh_tab_targets(context))

    def invoke(self, context, event):
        if not get_library(context.scene):
            self.report({'INFO'}, "No database tags yet")
            return {'CANCELLED'}

        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.tag == "__NONE__":
            return {'CANCELLED'}

        meshes = mesh_tab_targets(context)
        add_tag_to_data_blocks(meshes, self.tag)
        remember_tag(context.scene, self.tag)

        self.report({'INFO'}, f'Reused "{self.tag}" on {len(meshes)} mesh data-block(s)')
        return {'FINISHED'}


class MESH_OT_tag_add_new(bpy.types.Operator):
    """Create a new mesh-data tag from the text fields and apply it."""

    bl_idname = "mesh.tag_add_new"
    bl_label = "Add Tag"
    bl_description = "Add this tag to selected mesh data"

    @classmethod
    def poll(cls, context):
        return bool(mesh_tab_targets(context))

    def execute(self, context):
        wm = context.window_manager

        try:
            tag = make_tag(wm.object_tag_key, wm.object_tag_value)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

        meshes = mesh_tab_targets(context)
        add_tag_to_data_blocks(meshes, tag)
        remember_tag(context.scene, tag)

        wm.object_tag_key = ""
        wm.object_tag_value = ""

        self.report({'INFO'}, f'Added "{tag}" to {len(meshes)} mesh data-block(s)')
        return {'FINISHED'}


class MESH_OT_tag_remove_key(bpy.types.Operator):
    """Remove one tag key from selected mesh data-blocks."""

    bl_idname = "mesh.tag_remove_key"
    bl_label = "Remove Tag"
    bl_description = "Remove this tag key from selected mesh data"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return bool(mesh_tab_targets(context))

    def execute(self, context):
        meshes = mesh_tab_targets(context)
        remove_tag_key_from_data_blocks(meshes, self.key)

        self.report({'INFO'}, f'Removed "{self.key}" from {len(meshes)} mesh data-block(s)')
        return {'FINISHED'}


class OBJECT_OT_tag_library_add(bpy.types.Operator):
    """Add a tag to the database without applying it to any object or mesh."""

    bl_idname = "object.tag_library_add"
    bl_label = "Add Database Tag"

    def execute(self, context):
        wm = context.window_manager

        try:
            tag = make_tag(wm.object_tag_library_key, wm.object_tag_library_value)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}

        remember_tag(context.scene, tag)
        wm.object_tag_library_key = ""
        wm.object_tag_library_value = ""
        return {'FINISHED'}


class OBJECT_OT_tag_library_remove(bpy.types.Operator):
    """Remove one exact raw tag string from the tag database only."""

    bl_idname = "object.tag_library_remove"
    bl_label = "Remove Database Tag"
    bl_description = "Remove this exact entry from the tag database only"

    tag: StringProperty()

    def execute(self, context):
        library = set(get_library(context.scene))
        library.discard(self.tag)
        set_library(context.scene, library)
        return {'FINISHED'}


class OBJECT_OT_tag_library_rebuild(bpy.types.Operator):
    """Replace the tag database with tags currently used by scene data."""

    bl_idname = "object.tag_library_rebuild"
    bl_label = "Rebuild From Scene"
    bl_description = "Replace the tag database with tags currently used by objects and meshes"

    def execute(self, context):
        tags = set()
        for obj in bpy.data.objects:
            tags.update(get_tags(obj))

        for mesh in bpy.data.meshes:
            tags.update(get_tags(mesh))

        set_library(context.scene, tags)
        self.report({'INFO'}, f"Rebuilt database with {len(tags)} tag(s)")
        return {'FINISHED'}


class OBJECT_OT_tag_library_export(bpy.types.Operator):
    """Write the tag database to a small portable JSON file."""

    bl_idname = "object.tag_library_export"
    bl_label = "Export Tag Database"

    filepath: StringProperty(subtype='FILE_PATH')

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = "tag_library.json"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        payload = {
            "format": "generic-object-tags",
            "version": 1,
            "tags": get_library(context.scene),
        }

        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
        except OSError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, f"Exported tag database to {self.filepath}")
        return {'FINISHED'}


class OBJECT_OT_tag_library_import(bpy.types.Operator):
    """Merge or replace the tag database from a JSON file."""

    bl_idname = "object.tag_library_import"
    bl_label = "Import Tag Database"

    filepath: StringProperty(subtype='FILE_PATH')

    replace_existing: bpy.props.BoolProperty(
        name="Replace Existing Database",
        description="Replace rather than merge with the current tag database",
        default=False,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        if isinstance(payload, dict):
            tags = payload.get("tags")
        else:
            tags = payload

        if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
            self.report({'ERROR'}, 'Expected {"tags": [...]} or a JSON string array')
            return {'CANCELLED'}

        valid = set()

        for tag in tags:
            key, value = split_tag(tag.strip())

            if not key or "=" in key:
                continue

            valid.add(f"{key}={value}" if value is not None else key)

        if self.replace_existing:
            merged = valid
        else:
            merged = set(get_library(context.scene))
            merged.update(valid)

        set_library(context.scene, merged)

        self.report({'INFO'}, f"Imported {len(valid)} tag(s)")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------

class OBJECT_PT_generic_tags(bpy.types.Panel):
    """Object Properties panel for semantic object/node tags."""

    bl_label = "Tags"
    bl_idname = "OBJECT_PT_generic_tags"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "object"

    def draw(self, context):
        layout = self.layout
        objects = selected_objects(context)

        if not objects:
            layout.label(text="Select one or more objects")
            return

        count = len(objects)

        if count == 1:
            layout.label(text=objects[0].name, icon='OBJECT_DATA')
        else:
            layout.label(text=f"{count} objects selected", icon='RESTRICT_SELECT_OFF')

        draw_export_warning(layout)

        layout.operator(
            "object.tag_add_known",
            text="Reuse Tag...",
            icon='VIEWZOOM',
        )

        draw_tag_composer(
            layout,
            context,
            "tag",
            "object.tag_add_new",
            "Add To Selection",
        )

        layout.separator()

        rows = tag_rows(objects)

        if not rows:
            layout.label(text="No tags on selection")
            return

        for key in sorted(rows):
            values = rows[key]
            tagged_count = sum(values.values())

            # If every selected object has the same value, show it directly.
            if len(values) == 1 and tagged_count == count:
                value = next(iter(values))
                text = f"{key} = {value}" if value is not None else key
                icon = 'CHECKMARK'

            # Otherwise show mixed/partial state.
            else:
                value_parts = []
                for value, n in sorted(
                    values.items(),
                    key=lambda item: "" if item[0] is None else item[0]
                ):
                    label = "(flag)" if value is None else value
                    value_parts.append(f"{label}: {n}")

                text = f"{key}  [{', '.join(value_parts)}]"
                icon = 'REMOVE'

            row = layout.row(align=True)
            row.label(text=text, icon=icon)

            op = row.operator("object.tag_remove_key", text="", icon='X')
            op.key = key


class DATA_PT_generic_mesh_tags(bpy.types.Panel):
    """Mesh Data Properties panel for mesh data-block tags."""

    bl_label = "Tags"
    bl_idname = "DATA_PT_generic_mesh_tags"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return active_mesh(context) is not None

    def draw(self, context):
        layout = self.layout
        meshes = mesh_tab_targets(context)

        if not meshes:
            layout.label(text="Select a mesh object")
            return

        count = len(meshes)

        if count == 1:
            layout.label(text=meshes[0].name, icon='MESH_DATA')
        else:
            layout.label(text=f"{count} mesh data-blocks selected", icon='MESH_DATA')

        draw_export_warning(layout)

        layout.operator(
            "mesh.tag_add_known",
            text="Reuse Tag...",
            icon='VIEWZOOM',
        )

        draw_tag_composer(
            layout,
            context,
            "tag",
            "mesh.tag_add_new",
            "Add To Mesh Data",
        )

        layout.separator()

        rows = tag_rows(meshes)

        if not rows:
            layout.label(text="No tags on mesh data")
            return

        for key in sorted(rows):
            values = rows[key]
            tagged_count = sum(values.values())

            if len(values) == 1 and tagged_count == count:
                value = next(iter(values))
                text = f"{key} = {value}" if value is not None else key
                icon = 'CHECKMARK'
            else:
                value_parts = []
                for value, n in sorted(
                    values.items(),
                    key=lambda item: "" if item[0] is None else item[0]
                ):
                    label = "(flag)" if value is None else value
                    value_parts.append(f"{label}: {n}")

                text = f"{key}  [{', '.join(value_parts)}]"
                icon = 'REMOVE'

            row = layout.row(align=True)
            row.label(text=text, icon=icon)

            op = row.operator("mesh.tag_remove_key", text="", icon='X')
            op.key = key


class OBJECT_PT_generic_tag_library(bpy.types.Panel):
    """Collapsed subpanel for maintaining the shared tag database."""

    bl_label = "Tag Database"
    bl_idname = "OBJECT_PT_generic_tag_library"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "object"
    bl_parent_id = "OBJECT_PT_generic_tags"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout

        draw_tag_composer(
            layout,
            context,
            "library",
            "object.tag_library_add",
            "Add To Database",
            label="Database Entry",
        )

        layout.separator()

        tags = get_library(context.scene)

        if tags:
            for tag in tags:
                row = layout.row(align=True)
                row.label(text=format_tag(tag))

                op = row.operator(
                    "object.tag_library_remove",
                    text="",
                    icon='X',
                )
                op.tag = tag
        else:
            layout.label(text="No database tags")

        layout.separator()

        row = layout.row(align=True)
        row.operator("object.tag_library_import", text="Import JSON", icon='IMPORT')
        row.operator("object.tag_library_export", text="Export JSON", icon='EXPORT')

        draw_export_warning(layout)

        layout.operator(
            "object.tag_library_rebuild",
            text="Rebuild From Scene",
            icon='FILE_REFRESH',
        )


classes = (
    OBJECT_OT_tag_add_known,
    OBJECT_OT_tag_add_new,
    OBJECT_OT_tag_remove_key,
    OBJECT_OT_tag_fill_key,
    OBJECT_OT_tag_fill_value,
    OBJECT_OT_tag_fill_value_from_object,
    MESH_OT_tag_add_known,
    MESH_OT_tag_add_new,
    MESH_OT_tag_remove_key,
    OBJECT_OT_tag_library_add,
    OBJECT_OT_tag_library_remove,
    OBJECT_OT_tag_library_rebuild,
    OBJECT_OT_tag_library_export,
    OBJECT_OT_tag_library_import,
    OBJECT_PT_generic_tags,
    DATA_PT_generic_mesh_tags,
    OBJECT_PT_generic_tag_library,
)


def register():
    """Register Blender classes and transient WindowManager text fields."""
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.object_tag_key = StringProperty(
        name="Tag Key",
        description="Tag key; '=' is reserved",
        default="",
    )

    bpy.types.WindowManager.object_tag_value = StringProperty(
        name="Tag Value",
        description="Optional string value",
        default="",
    )

    bpy.types.WindowManager.object_tag_library_key = StringProperty(
        name="Tag Key",
        description="Database tag key; '=' is reserved",
        default="",
    )

    bpy.types.WindowManager.object_tag_library_value = StringProperty(
        name="Tag Value",
        description="Optional database tag string value",
        default="",
    )


def unregister():
    """Undo `register` so the script can be reloaded during add-on development."""
    for name in (
        "object_tag_key",
        "object_tag_value",
        "object_tag_library_key",
        "object_tag_library_value",
    ):
        if hasattr(bpy.types.WindowManager, name):
            delattr(bpy.types.WindowManager, name)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
