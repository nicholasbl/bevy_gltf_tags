# bevy_gltf_tags

Generic tag callbacks for Bevy glTF scenes, plus a companion Blender add-on for
authoring those tags as glTF `extras`.

1. Install `support/generic_object_tags.py` in Blender.
2. Add tags in Object Properties > Tags or Mesh Data Properties > Tags.
3. Enable glTF custom properties when exporting.
4. Add `ObjectTagsPlugin` in Bevy and register callbacks with `tag_action` or
   `tagged_scene_action`.

> [!IMPORTANT]
> Object tags land on glTF node entities. Mesh data-block tags land on the
> rendered mesh entities Bevy creates for mesh/material data. If you want to work with
> an object's mesh and material, use tags on the mesh data block!

```rust
use bevy::prelude::*;
use bevy_gltf_tags::{ObjectTagsPlugin, TagActionAppExt};

fn main() {
    App::new()
        .add_plugins(DefaultPlugins)
        .add_plugins(ObjectTagsPlugin)
        .tag_action("physics/collider", |entity, value, commands| {
            commands.entity(entity).insert(Name::new(
                value.unwrap_or_else(|| "collider".to_owned()),
            ));
        })
        .run();
}
```

For whole imported-scene behavior, use `tagged_scene_action` to receive a
`TaggedScene` indexed by name, tag key, and tag key/value pairs.

```rust
app.tagged_scene_action(|scene, commands| {
        for source in scene.by_tag_value("arc/source", "panel_a") {
            for target in scene.by_tag_value("arc/target", "panel_a") {
                commands.spawn(ElectricalArc::new(source, target));
            }
        }
    });
```
