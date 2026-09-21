//! Generic glTF tag actions for Bevy.
//!
//! This module is the Bevy side of the Blender tag workflow in
//! `support/generic_object_tags.py`.
//!
//! The contract is intentionally tiny:
//!
//! * Blender stores tags as custom properties under the key `tags`.
//! * The glTF exporter writes those custom properties into glTF `extras`.
//! * Bevy's glTF loader exposes node/primitive extras as [`GltfExtras`] and mesh
//!   data-block extras as [`GltfMeshExtras`].
//! * This plugin observes newly-added extras components, parses any tags, and
//!   runs callbacks registered by tag key.
//! * Whole scene instance callbacks can inspect an index of the names and tags
//!   in one spawned glTF/WorldAsset instance.
//!
//! Tags are strings in one of two forms:
//!
//! * `some/key`
//! * `some/key=value`
//!
//! The callback receives the Bevy [`Entity`] that owns the [`GltfExtras`] or
//! [`GltfMeshExtras`], the optional string value, and [`Commands`]. There is no
//! built-in coupling to a gameplay component type, physics crate, or asset
//! convention.
//!
//! # Object Tags vs Mesh Tags
//!
//! Blender object custom properties become glTF node extras. Mesh data-block
//! custom properties become glTF mesh extras. Bevy commonly spawns a glTF node
//! entity and a child render entity with [`Mesh3d`] / material components. That
//! split is why the Blender add-on has both an Object Properties panel and a
//! Mesh Data Properties panel:
//!
//! * tag the object when the callback should act on the semantic node;
//! * tag the mesh data when the callback should act on the rendered mesh entity.
//!
//! Keeping the callback shape to just `Entity` makes this module minimal. Code
//! that needs broader context can query it in a normal Bevy system or register a
//! tag action that inserts a marker component for later systems to consume.
//!
//! # Whole Scene Actions
//!
//! Some behavior needs knowledge of multiple authored objects in the same
//! imported scene instance. For example, an electrical arc may need to connect
//! all entities tagged `arc/source=panel_a` to entities tagged
//! `arc/target=panel_a`. Global names are not enough for this: if the same glTF
//! is instantiated twice, Blender names collide, while Bevy entities remain
//! unique.
//!
//! [`TaggedSceneActionAppExt::tagged_scene_action`] runs after a
//! [`WorldInstanceReady`] event. The callback receives a [`TaggedScene`] snapshot
//! containing only the entities from that instance, indexed by [`Name`], tag key,
//! and tag key/value pair. In normal use this means scenes spawned through
//! [`WorldAssetRoot`](bevy_world_serialization::WorldAssetRoot) /
//! `bevy_world_serialization`, which is also what `bevy_ahoy` uses for glTF
//! scene roots.

use std::{collections::HashMap, hash::Hash, sync::Arc};

use bevy_app::prelude::*;
use bevy_ecs::prelude::*;
use bevy_gltf::{GltfExtras, GltfMeshExtras};
use bevy_scene::{SceneInstanceReady, SceneSpawner};

use serde_json::Value;

/// Bevy plugin that enables tag-action callbacks for glTF extras.
///
/// The plugin registers lifecycle observers for `Add<GltfExtras>` and
/// `Add<GltfMeshExtras>`, so there is no per-frame query scanning for newly-added
/// extras. The work happens when the components are inserted by Bevy's glTF
/// scene spawning machinery.
pub struct ObjectTagsPlugin;

impl Plugin for ObjectTagsPlugin {
    fn build(&self, app: &mut App) {
        app.init_resource::<TagActions>()
            .init_resource::<TaggedSceneActions>()
            .add_observer(apply_tag_actions)
            .add_observer(apply_mesh_tag_actions)
            .add_observer(apply_tagged_scene_actions);
    }
}

/// Convenience extension for registering tag callbacks directly on [`App`].
///
/// Example:
///
/// ```ignore
/// app.add_plugins(ObjectTagsPlugin)
///     .tag_action("physics/collider", |entity, value, commands| {
///         commands.entity(entity).insert(MyColliderMarker(value));
///     });
/// ```
///
/// The registered key is the part before `=`. A Blender tag of
/// `physics/collider=trimesh` invokes the `physics/collider` callback with
/// `Some("trimesh".to_owned())`.
pub trait TagActionAppExt {
    fn tag_action<F>(&mut self, key: impl Into<String>, action: F) -> &mut Self
    where
        F: for<'w, 's> Fn(Entity, Option<String>, &mut Commands<'w, 's>) + Send + Sync + 'static;
}

impl TagActionAppExt for App {
    fn tag_action<F>(&mut self, key: impl Into<String>, action: F) -> &mut Self
    where
        F: for<'w, 's> Fn(Entity, Option<String>, &mut Commands<'w, 's>) + Send + Sync + 'static,
    {
        let key = key.into();
        assert!(!key.contains('='), "tag action keys may not contain '='");

        self.init_resource::<TagActions>();
        self.world_mut()
            .resource_mut::<TagActions>()
            .insert(key, action);

        self
    }
}

/// Convenience extension for registering whole scene instance callbacks.
///
/// Example:
///
/// ```ignore
/// app.add_plugins(ObjectTagsPlugin)
///     .tagged_scene_action(|scene, commands| {
///         for source in scene.by_tag_value("arc/source", "panel_a") {
///             for target in scene.by_tag_value("arc/target", "panel_a") {
///                 commands.spawn(ElectricalArc::new(source, target));
///             }
///         }
///     });
/// ```
pub trait TaggedSceneActionAppExt {
    fn tagged_scene_action<F>(&mut self, action: F) -> &mut Self
    where
        F: for<'a, 'w, 's> Fn(&'a TaggedScene, &mut Commands<'w, 's>) + Send + Sync + 'static;
}

impl TaggedSceneActionAppExt for App {
    fn tagged_scene_action<F>(&mut self, action: F) -> &mut Self
    where
        F: for<'a, 'w, 's> Fn(&'a TaggedScene, &mut Commands<'w, 's>) + Send + Sync + 'static,
    {
        self.init_resource::<TaggedSceneActions>();
        self.world_mut()
            .resource_mut::<TaggedSceneActions>()
            .insert(action);

        self
    }
}

type TagAction =
    Arc<dyn for<'w, 's> Fn(Entity, Option<String>, &mut Commands<'w, 's>) + Send + Sync + 'static>;

type TaggedSceneAction =
    Arc<dyn for<'a, 'w, 's> Fn(&'a TaggedScene, &mut Commands<'w, 's>) + Send + Sync + 'static>;

/// Registry of runtime actions keyed by tag name.
///
/// This is a resource so callers can register/remove actions either through the
/// [`TagActionAppExt`] convenience API during app setup or directly through
/// normal Bevy resource access.
#[derive(Resource, Default)]
pub struct TagActions {
    actions: HashMap<String, TagAction>,
}

impl TagActions {
    /// Register or replace the action for one tag key.
    ///
    /// Keys must not include `=` because `=` is reserved as the key/value
    /// separator in authored tag strings.
    pub fn insert<F>(&mut self, key: impl Into<String>, action: F)
    where
        F: for<'w, 's> Fn(Entity, Option<String>, &mut Commands<'w, 's>) + Send + Sync + 'static,
    {
        let key = key.into();
        assert!(!key.contains('='), "tag action keys may not contain '='");
        self.actions.insert(key, Arc::new(action));
    }

    /// Remove the action for `key`, returning whether an action existed.
    pub fn remove(&mut self, key: &str) -> bool {
        self.actions.remove(key).is_some()
    }

    /// Return whether an action is registered for `key`.
    pub fn contains(&self, key: &str) -> bool {
        self.actions.contains_key(key)
    }
}

/// Registry of callbacks that run once for each ready scene instance.
#[derive(Resource, Default)]
pub struct TaggedSceneActions {
    actions: Vec<TaggedSceneAction>,
}

impl TaggedSceneActions {
    /// Register one whole-scene callback.
    pub fn insert<F>(&mut self, action: F)
    where
        F: for<'a, 'w, 's> Fn(&'a TaggedScene, &mut Commands<'w, 's>) + Send + Sync + 'static,
    {
        self.actions.push(Arc::new(action));
    }

    /// Return whether any scene callbacks are registered.
    pub fn is_empty(&self) -> bool {
        self.actions.is_empty()
    }
}

/// Small multi-map wrapper used by [`TaggedScene`].
///
/// It deliberately exposes a narrow read API: callers usually care about
/// iterating the values for one key, while construction remains internal to this
/// module.
#[derive(Debug, Clone)]
pub struct MultiMap<K, V> {
    inner: HashMap<K, Vec<V>>,
}

impl<K, V> Default for MultiMap<K, V> {
    fn default() -> Self {
        Self {
            inner: HashMap::new(),
        }
    }
}

impl<K, V> MultiMap<K, V>
where
    K: Eq + Hash,
{
    fn insert(&mut self, key: K, value: V) {
        self.inner.entry(key).or_default().push(value);
    }

    /// Return values stored for `key`.
    pub fn values(&self, key: &K) -> impl Iterator<Item = &V> {
        self.inner.get(key).into_iter().flatten()
    }

    /// Return all key/value groups.
    pub fn iter(&self) -> impl Iterator<Item = (&K, &[V])> {
        self.inner
            .iter()
            .map(|(key, values)| (key, values.as_slice()))
    }
}

/// One entity found by a tag lookup in [`TaggedScene`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaggedEntity {
    /// Entity carrying the tag.
    pub entity: Entity,
    /// Optional value from `key=value`.
    pub value: Option<String>,
}

/// Snapshot of one ready glTF/WorldAsset scene instance.
///
/// The snapshot owns its indexes so user callbacks can freely query it while
/// issuing [`Commands`]. It is intentionally read-only: mutations should happen
/// through commands or follow-up systems.
#[derive(Debug, Clone, Default)]
pub struct TaggedScene {
    root: Option<Entity>,
    entities: Vec<Entity>,
    by_name: MultiMap<String, Entity>,
    by_tag: MultiMap<String, TaggedEntity>,
    by_tag_value: MultiMap<(String, String), Entity>,
}

impl TaggedScene {
    fn from_entities(
        root: Option<Entity>,
        entities: Vec<Entity>,
        mut get_name: impl FnMut(Entity) -> Option<String>,
        mut get_tags: impl FnMut(Entity) -> Vec<ParsedTag>,
    ) -> Self {
        let mut scene = Self {
            root,
            entities,
            ..Default::default()
        };

        for &entity in &scene.entities {
            if let Some(name) = get_name(entity) {
                scene.by_name.insert(name, entity);
            }

            for tag in get_tags(entity) {
                if let Some(value) = &tag.value {
                    scene
                        .by_tag_value
                        .insert((tag.key.clone(), value.clone()), entity);
                }

                scene.by_tag.insert(
                    tag.key,
                    TaggedEntity {
                        entity,
                        value: tag.value,
                    },
                );
            }
        }

        scene
    }

    fn from_world(
        root: Option<Entity>,
        entities: Vec<Entity>,
        names: &Query<&Name>,
        extras: &Query<(Option<&GltfExtras>, Option<&GltfMeshExtras>)>,
    ) -> Self {
        Self::from_entities(
            root,
            entities,
            |entity| names.get(entity).map(|name| name.as_str().to_owned()).ok(),
            |entity| {
                extras
                    .get(entity)
                    .map(|(node_extras, mesh_extras)| {
                        tags_from_components(node_extras, mesh_extras)
                    })
                    .unwrap_or_default()
            },
        )
    }

    /// Return the entity that owns the spawned world instance, if there is one.
    pub fn root(&self) -> Option<Entity> {
        self.root
    }

    /// Return every entity in this scene instance.
    pub fn entities(&self) -> &[Entity] {
        &self.entities
    }

    /// Return entities with the exact Bevy [`Name`] string.
    pub fn by_name<'a>(&'a self, name: &'a str) -> impl Iterator<Item = Entity> + 'a {
        self.by_name.inner.get(name).into_iter().flatten().copied()
    }

    /// Return tagged entities for one tag key, preserving optional values.
    pub fn by_tag<'a>(&'a self, key: &'a str) -> impl Iterator<Item = &'a TaggedEntity> + 'a {
        self.by_tag.inner.get(key).into_iter().flatten()
    }

    /// Return entities with an exact tag key/value pair.
    pub fn by_tag_value<'a>(
        &'a self,
        key: &'a str,
        value: &'a str,
    ) -> impl Iterator<Item = Entity> + 'a {
        self.by_tag_value
            .inner
            .iter()
            .filter(move |((tag_key, tag_value), _)| tag_key == key && tag_value == value)
            .flat_map(|(_, entities)| entities.iter().copied())
    }

    /// Return the underlying name index.
    pub fn names(&self) -> &MultiMap<String, Entity> {
        &self.by_name
    }

    /// Return the underlying tag-key index.
    pub fn tags(&self) -> &MultiMap<String, TaggedEntity> {
        &self.by_tag
    }

    /// Return the underlying exact tag-value index.
    pub fn tag_values(&self) -> &MultiMap<(String, String), Entity> {
        &self.by_tag_value
    }
}

/// Parsed representation of one authored tag.
///
/// `value` is `None` for flag tags like `spawn/player`, and `Some(_)` for
/// valued tags like `physics/body=static`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedTag {
    pub key: String,
    pub value: Option<String>,
}

/// Parse one raw tag string.
///
/// Empty strings and empty keys are ignored. The first `=` separates key from
/// value; additional `=` characters belong to the value so URL-like strings
/// remain valid.
pub fn parse_tag(tag: &str) -> Option<ParsedTag> {
    let tag = tag.trim();
    if tag.is_empty() {
        return None;
    }

    let (key, value) = match tag.split_once('=') {
        Some((key, value)) => (key.trim(), Some(value.to_owned())),
        None => (tag, None),
    };

    if key.is_empty() || key.contains('=') {
        return None;
    }

    Some(ParsedTag {
        key: key.to_owned(),
        value,
    })
}

/// Extract tags from a Bevy [`GltfExtras`] or [`GltfMeshExtras`] JSON string.
///
/// Blender exports custom properties as JSON object members. For this add-on,
/// the relevant member is `tags`.
///
/// The Blender script stores `tags` as a JSON-encoded string array because that
/// round-trips predictably as a custom property:
///
/// ```json
/// { "tags": "[\"physics/collider\", \"spawn/player\"]" }
/// ```
///
/// This function also accepts a direct JSON array:
///
/// ```json
/// { "tags": ["physics/collider", "spawn/player"] }
/// ```
///
/// Supporting both forms keeps the runtime permissive for hand-authored glTFs or
/// future exporter changes.
pub fn tags_from_gltf_extras(extras: &str) -> Vec<ParsedTag> {
    let Ok(root) = serde_json::from_str::<Value>(extras) else {
        return Vec::new();
    };

    let Some(tags) = root.get("tags") else {
        return Vec::new();
    };

    let raw_tags: Vec<String> = match tags {
        Value::String(s) => serde_json::from_str::<Vec<String>>(s).unwrap_or_default(),

        Value::Array(values) => values
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect(),

        _ => Vec::new(),
    };

    raw_tags.iter().filter_map(|tag| parse_tag(tag)).collect()
}

fn tags_from_components(
    node_extras: Option<&GltfExtras>,
    mesh_extras: Option<&GltfMeshExtras>,
) -> Vec<ParsedTag> {
    node_extras
        .into_iter()
        .map(|extras| extras.value.as_str())
        .chain(mesh_extras.into_iter().map(|extras| extras.value.as_str()))
        .flat_map(tags_from_gltf_extras)
        .collect()
}

fn apply_tag_actions(
    add: On<Add, GltfExtras>,
    mut commands: Commands,
    actions: Res<TagActions>,
    extras: Query<&GltfExtras>,
) {
    // Lifecycle observers tell us which entity changed, but not the component
    // value itself. A point query is still event-driven and avoids an Update
    // system scanning `Added<GltfExtras>` each frame.
    let entity = add.entity;
    let Ok(extras) = extras.get(entity) else {
        return;
    };

    dispatch_tag_actions(entity, &extras.value, &actions, &mut commands);
}

fn apply_mesh_tag_actions(
    add: On<Add, GltfMeshExtras>,
    mut commands: Commands,
    actions: Res<TagActions>,
    extras: Query<&GltfMeshExtras>,
) {
    let entity = add.entity;
    let Ok(extras) = extras.get(entity) else {
        return;
    };

    dispatch_tag_actions(entity, &extras.value, &actions, &mut commands);
}

fn dispatch_tag_actions(
    entity: Entity,
    extras: &str,
    actions: &TagActions,
    commands: &mut Commands,
) {
    for tag in tags_from_gltf_extras(extras) {
        if let Some(action) = actions.actions.get(&tag.key) {
            // Clone-free dispatch: each parsed tag is owned, so the optional
            // value can move into the user's callback.
            action(entity, tag.value, commands);
        }
    }
}

fn apply_tagged_scene_actions(
    ready: On<SceneInstanceReady>,
    mut commands: Commands,
    actions: Res<TaggedSceneActions>,
    spawner: Res<SceneSpawner>,
    names: Query<&Name>,
    extras: Query<(Option<&GltfExtras>, Option<&GltfMeshExtras>)>,
) {
    if actions.is_empty() {
        return;
    }

    let root = (ready.entity != Entity::PLACEHOLDER).then_some(ready.entity);
    let entities = spawner
        .iter_instance_entities(ready.instance_id)
        .collect::<Vec<_>>();
    let scene = TaggedScene::from_world(root, entities, &names, &extras);

    for action in &actions.actions {
        action(&scene, &mut commands);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    use bevy_asset::prelude::*;
    use bevy_mesh::prelude::*;

    #[derive(Component)]
    struct TaggedMarker(Option<String>);

    #[test]
    fn tagged_scene_indexes_names_tags_and_tag_values() {
        let mut app = App::new();

        let root = app.world_mut().spawn(Name::new("Imported Fixture")).id();
        let source = app
            .world_mut()
            .spawn((
                Name::new("ArcNode"),
                GltfExtras {
                    value: r#"{"tags":["arc/source=main","arc/source"]}"#.into(),
                },
            ))
            .id();
        let target = app
            .world_mut()
            .spawn((
                Name::new("ArcNode"),
                GltfMeshExtras {
                    value: r#"{"tags":["arc/target=main"]}"#.into(),
                },
            ))
            .id();

        let scene = TaggedScene::from_entities(
            Some(root),
            vec![source, target],
            |entity| {
                app.world()
                    .get::<Name>(entity)
                    .map(|name| name.as_str().to_owned())
            },
            |entity| {
                tags_from_components(
                    app.world().get::<GltfExtras>(entity),
                    app.world().get::<GltfMeshExtras>(entity),
                )
            },
        );

        assert_eq!(scene.root(), Some(root));
        assert_eq!(scene.entities(), &[source, target]);
        assert_eq!(
            scene.by_name("ArcNode").collect::<Vec<_>>(),
            vec![source, target]
        );
        assert_eq!(
            scene.by_tag_value("arc/source", "main").collect::<Vec<_>>(),
            vec![source]
        );
        assert_eq!(
            scene.by_tag_value("arc/target", "main").collect::<Vec<_>>(),
            vec![target]
        );

        let source_tags = scene.by_tag("arc/source").collect::<Vec<_>>();
        assert_eq!(source_tags.len(), 2);
        assert!(
            source_tags
                .iter()
                .any(|tagged| tagged.entity == source && tagged.value.as_deref() == Some("main"))
        );
        assert!(
            source_tags
                .iter()
                .any(|tagged| tagged.entity == source && tagged.value.is_none())
        );
    }

    #[test]
    fn tagged_scene_action_registers_callback() {
        let mut app = App::new();

        app.tagged_scene_action(|_, _| {});

        assert!(!app.world().resource::<TaggedSceneActions>().is_empty());
    }

    #[test]
    fn parses_flag() {
        assert_eq!(
            parse_tag("physics/collider"),
            Some(ParsedTag {
                key: "physics/collider".into(),
                value: None,
            })
        );
    }

    #[test]
    fn parses_value() {
        assert_eq!(
            parse_tag("physics/body=static"),
            Some(ParsedTag {
                key: "physics/body".into(),
                value: Some("static".into()),
            })
        );
    }

    #[test]
    fn value_may_contain_equals() {
        assert_eq!(
            parse_tag("url=https://example.test/?a=b"),
            Some(ParsedTag {
                key: "url".into(),
                value: Some("https://example.test/?a=b".into()),
            })
        );
    }

    #[test]
    fn action_receives_tagged_entity() {
        // Object/node tags should act on exactly the entity carrying
        // `GltfExtras`. We intentionally do not walk children here; if an action
        // belongs on the rendered mesh, authors should tag mesh data in Blender.
        let mut app = App::new();
        app.add_plugins(ObjectTagsPlugin).tag_action(
            "physics/collider",
            |entity, value, commands| {
                commands.entity(entity).insert(TaggedMarker(value));
            },
        );

        let node = app
            .world_mut()
            .spawn(GltfExtras {
                value: r#"{"tags":["physics/collider=trimesh"]}"#.into(),
            })
            .id();

        let mesh = app.world_mut().spawn(Mesh3d(Handle::default())).id();

        app.world_mut().entity_mut(node).add_child(mesh);

        app.update();

        assert_eq!(
            app.world()
                .entity(node)
                .get::<TaggedMarker>()
                .map(|marker| marker.0.as_deref()),
            Some(Some("trimesh"))
        );
        assert!(!app.world().entity(mesh).contains::<TaggedMarker>());
    }

    #[test]
    fn action_on_mesh_tag_receives_mesh_entity() {
        // Bevy 0.18 represents glTF mesh-level extras with GltfMeshExtras, not
        // GltfExtras. Keep this faithful to the component inserted by the real
        // loader so the observer contract cannot accidentally regress.
        let mut app = App::new();
        app.add_plugins(ObjectTagsPlugin).tag_action(
            "physics/collider",
            |entity, value, commands| {
                commands.entity(entity).insert(TaggedMarker(value));
            },
        );

        let mesh = app
            .world_mut()
            .spawn((
                GltfMeshExtras {
                    value: r#"{"tags":["physics/collider=convex"]}"#.into(),
                },
                Mesh3d(Handle::default()),
            ))
            .id();

        app.update();

        assert_eq!(
            app.world()
                .entity(mesh)
                .get::<TaggedMarker>()
                .map(|marker| marker.0.as_deref()),
            Some(Some("convex"))
        );
    }

    #[test]
    fn test_level_mesh_extras_trigger_actions() {
        // Recreate the components Bevy 0.18's glTF loader inserts for each mesh
        // in the real fixture. This keeps the regression test deterministic and
        // synchronous while exercising the plugin's actual Add observer.
        let root = glb_json("assets/gltfs/test_level.glb");
        let meshes = root
            .get("meshes")
            .and_then(Value::as_array)
            .expect("test level should contain meshes");

        let mut app = App::new();
        app.add_plugins(ObjectTagsPlugin).tag_action(
            "physics/coll_simple_cube",
            |entity, value, commands| {
                commands.entity(entity).insert(TaggedMarker(value));
            },
        );

        let mut mesh_entities = Vec::new();
        for mesh in meshes {
            let extras = mesh
                .get("extras")
                .and_then(|extras| serde_json::to_string(extras).ok())
                .expect("each test-level mesh should have extras");

            mesh_entities.push(
                app.world_mut()
                    .spawn((GltfMeshExtras { value: extras }, Mesh3d(Handle::default())))
                    .id(),
            );
        }

        app.update();

        assert!(!mesh_entities.is_empty());
        for entity in mesh_entities {
            assert!(
                app.world().entity(entity).contains::<TaggedMarker>(),
                "mesh-level tag action should run for {entity}"
            );
        }
    }

    #[test]
    fn ci_gltf_exports_object_and_mesh_tags_with_material() {
        // This is an exporter-contract test for the checked-in CI fixture. It
        // parses the binary GLB JSON chunk directly so CI catches missing glTF
        // extras even without spinning up Bevy's async asset loader.
        //
        // It does not assert Bevy's final spawned hierarchy. Bevy may split one
        // glTF node with a mesh into a semantic node entity plus a render child.
        // The synthetic ECS tests above cover this module's callback behavior.
        let root = glb_json("assets/gltfs/tag_ci_test.glb");

        let scene = root
            .get("scene")
            .and_then(Value::as_u64)
            .expect("glTF should declare a default scene") as usize;
        let scene = root
            .get("scenes")
            .and_then(Value::as_array)
            .and_then(|scenes| scenes.get(scene))
            .expect("default scene should exist");
        let scene_nodes = scene
            .get("nodes")
            .and_then(Value::as_array)
            .expect("default scene should contain root nodes");
        assert_eq!(
            scene_nodes.len(),
            1,
            "CI fixture should have one root semantic parent node"
        );

        let nodes = root
            .get("nodes")
            .and_then(Value::as_array)
            .expect("glTF should contain nodes");
        let parent_index = scene_nodes[0]
            .as_u64()
            .expect("scene root node should be an index") as usize;
        let parent = nodes
            .get(parent_index)
            .expect("scene root node index should resolve");

        let parent_name = parent.get("name").and_then(Value::as_str).unwrap_or("");
        assert!(
            parent_name.to_lowercase().contains("cube"),
            "parent node should be the cube object, got {parent_name:?}"
        );

        let mut errors = Vec::new();

        if tags_from_extras_value(parent.get("extras")).is_empty() {
            errors.push(
                "parent cube object should export at least one tag in extras.tags".to_owned(),
            );
        }

        let Some(mesh_index) = parent
            .get("mesh")
            .and_then(Value::as_u64)
            .map(|index| index as usize)
        else {
            errors.push("parent cube node should reference a mesh".to_owned());
            panic_on_errors(errors);
            return;
        };

        let mesh = root
            .get("meshes")
            .and_then(Value::as_array)
            .and_then(|meshes| meshes.get(mesh_index));

        let Some(mesh) = mesh else {
            errors.push("cube mesh index should resolve".to_owned());
            panic_on_errors(errors);
            return;
        };

        if tags_from_extras_value(mesh.get("extras")).is_empty() {
            errors.push("cube mesh should export at least one tag in extras.tags".to_owned());
        }

        let has_material = mesh
            .get("primitives")
            .and_then(Value::as_array)
            .map(|primitives| {
                primitives
                    .iter()
                    .any(|primitive| primitive.get("material").is_some())
            })
            .unwrap_or(false);

        if !has_material {
            errors.push("cube mesh should have at least one material-backed primitive".to_owned());
        }

        if let Some(materials) = root.get("materials").and_then(Value::as_array) {
            if materials.is_empty() {
                errors.push("fixture should export at least one material".to_owned());
            }
        } else {
            errors.push("fixture should export a materials array".to_owned());
        }

        panic_on_errors(errors);
    }

    #[test]
    fn ci_scene_gltf_exports_pairable_source_and_target_tags() {
        // Scene-level actions need tags that can be paired inside one scene
        // instance without relying on globally non-unique names. This fixture
        // represents the electrical-arc style workflow: authored source nodes
        // and target nodes share a tag value, and runtime code can connect each
        // source to the target with the same value.
        let root = glb_json("assets/gltfs/scene_ci_test.glb");
        let nodes = root
            .get("nodes")
            .and_then(Value::as_array)
            .expect("scene fixture should contain nodes");

        let mut app = App::new();
        let mut entities = Vec::new();

        for node in nodes {
            let mut entity = app.world_mut().spawn_empty();

            if let Some(name) = node.get("name").and_then(Value::as_str) {
                entity.insert(Name::new(name.to_owned()));
            }

            if let Some(extras) = node
                .get("extras")
                .and_then(|extras| serde_json::to_string(extras).ok())
            {
                entity.insert(GltfExtras { value: extras });
            }

            entities.push(entity.id());
        }

        let scene = TaggedScene::from_entities(
            None,
            entities,
            |entity| {
                app.world()
                    .get::<Name>(entity)
                    .map(|name| name.as_str().to_owned())
            },
            |entity| {
                app.world()
                    .get::<GltfExtras>(entity)
                    .map(|extras| tags_from_gltf_extras(&extras.value))
                    .unwrap_or_default()
            },
        );

        let sources = scene.by_tag("source").collect::<Vec<_>>();
        let targets = scene.by_tag("target").collect::<Vec<_>>();
        let source_values = sources
            .iter()
            .filter_map(|tagged| tagged.value.as_deref())
            .collect::<Vec<_>>();
        let target_values = targets
            .iter()
            .filter_map(|tagged| tagged.value.as_deref())
            .collect::<Vec<_>>();

        let mut errors = Vec::new();

        if sources.is_empty() {
            errors.push("scene fixture should export at least one source tag".to_owned());
        }

        if targets.is_empty() {
            errors.push("scene fixture should export at least one target tag".to_owned());
        }

        for value in &source_values {
            if scene.by_tag_value("target", value).next().is_none() {
                errors.push(format!(
                    "source tag value {value:?} should have a matching target tag"
                ));
            }
        }

        for value in &target_values {
            if scene.by_tag_value("source", value).next().is_none() {
                errors.push(format!(
                    "target tag value {value:?} should have a matching source tag"
                ));
            }
        }

        panic_on_errors(errors);
    }

    fn tags_from_extras_value(extras: Option<&Value>) -> Vec<ParsedTag> {
        // Reuse the production parser so fixture assertions and runtime behavior
        // stay locked to the same accepted tag formats.
        extras
            .and_then(|extras| serde_json::to_string(extras).ok())
            .map(|extras| tags_from_gltf_extras(&extras))
            .unwrap_or_default()
    }

    fn panic_on_errors(errors: Vec<String>) {
        // Accumulate fixture failures so a bad export reports all missing pieces
        // in one test run: object tags, mesh tags, and material assignment.
        if !errors.is_empty() {
            panic!("CI GLB fixture contract failed:\n{}", errors.join("\n"));
        }
    }

    fn glb_json(path: impl AsRef<Path>) -> Value {
        // Minimal GLB reader: enough to validate the JSON chunk without adding a
        // test-only dependency. GLB stores a 12-byte header followed by typed
        // chunks; the first chunk must be JSON for glTF 2.0.
        let bytes = std::fs::read(path.as_ref()).expect("GLB fixture should be readable");

        assert!(bytes.len() >= 20, "GLB fixture is too small");
        assert_eq!(&bytes[0..4], b"glTF", "fixture should be a binary glTF");
        assert_eq!(
            u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
            2,
            "fixture should be glTF 2.0"
        );

        let total_len = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
        assert_eq!(
            total_len,
            bytes.len(),
            "GLB header length should match file"
        );

        let json_len = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        let json_type = u32::from_le_bytes(bytes[16..20].try_into().unwrap());
        assert_eq!(json_type, 0x4E4F534A, "first GLB chunk should be JSON");

        let json_end = 20 + json_len;
        assert!(
            json_end <= bytes.len(),
            "GLB JSON chunk should fit inside fixture"
        );

        serde_json::from_slice(&bytes[20..json_end]).expect("GLB JSON chunk should parse")
    }
}
