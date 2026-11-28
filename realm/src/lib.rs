//! SPDX-License-Identifier: MIT
//! Session ownership, private checkpoints and atomic publication for the arena server.
use serde::{Deserialize, Serialize};
use serde_json::json;
use spacetimedb::{Identity, ReducerContext, SpacetimeType, Table, ViewContext};

#[derive(SpacetimeType, Clone, Serialize)]
pub struct Input {
    seq: u64,
    forward: i8,
    side: i8,
    turn: i8,
    look: i16,
    pitch: i16,
    button: String,
}

#[spacetimedb::table(accessor=seat,private)]
pub struct Seat {
    #[primary_key]
    slot: u8,
    #[unique]
    owner: Identity,
    epoch: u64,
    seq: u64,
    online: bool,
    seen: i64,
    inputs: Vec<Input>,
}
#[derive(SpacetimeType, Clone, Deserialize)]
pub struct Actor {
    slot: u8,
    x: f32,
    y: f32,
    z: f32,
    yaw: f32,
    health: u8,
    energy: u8,
    style: u8,
    clip: String,
    serial: u64,
    elapsed_ms: u32,
    duration_ms: u32,
    pitch: f32,
}
#[spacetimedb::table(accessor=arena,private)]
pub struct Arena {
    #[primary_key]
    id: u8,
    owner: Identity,
    worker: Option<Identity>,
    revision: u64,
    last_step: i64,
    state: String,
    actors: Vec<Actor>,
    caption: String,
    trails: Vec<Vec<f32>>,
}
#[spacetimedb::reducer(init)]
pub fn init(ctx: &ReducerContext) {
    ctx.db.arena().insert(Arena {
        id: 0,
        owner: ctx.sender(),
        worker: None,
        revision: 0,
        last_step: 0,
        state: "{}".into(),
        actors: vec![],
        caption: "Waiting for simulation".into(),
        trails: vec![],
    });
}
#[spacetimedb::reducer]
pub fn admit(
    ctx: &ReducerContext,
    first: Identity,
    second: Identity,
    worker: Identity,
) -> Result<(), String> {
    let mut a = ctx.db.arena().id().find(0).ok_or("missing realm")?;
    if ctx.sender() != a.owner
        || a.worker.is_some()
        || [first, second, worker].contains(&a.owner)
        || first == second
        || worker == first
        || worker == second
    {
        return Err("invalid roster or operator".into());
    }
    for (slot, owner) in [(1, first), (2, second)] {
        ctx.db.seat().try_insert(Seat {
            slot,
            owner,
            epoch: 0,
            seq: 0,
            online: false,
            seen: 0,
            inputs: vec![],
        })?;
    }
    a.worker = Some(worker);
    ctx.db.arena().id().update(a);
    Ok(())
}
fn lease(ctx: &ReducerContext, epoch: u64) -> Result<Seat, String> {
    let p = ctx
        .db
        .seat()
        .owner()
        .find(ctx.sender())
        .ok_or("unassigned identity")?;
    if !p.online
        || p.epoch != epoch
        || ctx.timestamp.to_micros_since_unix_epoch() - p.seen >= 5_000_000
    {
        return Err("session expired".into());
    }
    Ok(p)
}
#[spacetimedb::table(accessor=lease_clock,private,scheduled(check_leases))]
pub struct LeaseClock {
    #[primary_key]
    #[auto_inc]
    id: u64,
    scheduled_at: spacetimedb::ScheduleAt,
}
fn expire_sessions(ctx: &ReducerContext, now: i64) -> bool {
    let mut changed = false;
    for mut s in ctx.db.seat().iter().collect::<Vec<_>>() {
        if s.online && now - s.seen >= 5_000_000 {
            s.online = false;
            s.inputs.clear();
            ctx.db.seat().slot().update(s);
            changed = true;
        }
    }
    if changed {
        presence_changed(ctx);
    }
    changed
}
#[spacetimedb::reducer]
pub fn check_leases(ctx: &ReducerContext, clock: LeaseClock) -> Result<(), String> {
    if ctx.sender() != ctx.database_identity() {
        return Err("realm clock required".into());
    }
    expire_sessions(ctx, ctx.timestamp.to_micros_since_unix_epoch());
    if !ctx.db.seat().iter().any(|p| p.online) {
        ctx.db.lease_clock().id().delete(clock.id);
    }
    Ok(())
}
fn presence_changed(ctx: &ReducerContext) {
    let mut a = ctx.db.arena().id().find(0).unwrap();
    a.revision += 1;
    ctx.db.arena().id().update(a);
}
#[spacetimedb::reducer]
pub fn enter(ctx: &ReducerContext, pack: String) -> Result<(), String> {
    if pack != "jk2-survivor-v1" {
        return Err("wrong pack".into());
    }
    let mut p = ctx
        .db
        .seat()
        .owner()
        .find(ctx.sender())
        .ok_or("unassigned identity")?;
    p.epoch = p.epoch.checked_add(1).ok_or("epoch exhausted")?;
    p.seen = ctx.timestamp.to_micros_since_unix_epoch();
    p.seq = 0;
    ctx.db.lower_pose().slot().delete(p.slot);
    ctx.db.contact().slot().delete(p.slot);
    p.inputs.clear();
    p.online = true;
    if ctx.db.lease_clock().count() == 0 {
        ctx.db.lease_clock().try_insert(LeaseClock {
            id: 0,
            scheduled_at: std::time::Duration::from_millis(250).into(),
        })?;
    }
    ctx.db.seat().slot().update(p);
    presence_changed(ctx);
    Ok(())
}
#[spacetimedb::reducer]
pub fn leave(ctx: &ReducerContext, epoch: u64) -> Result<(), String> {
    let mut p = lease(ctx, epoch)?;
    p.online = false;
    p.inputs.clear();
    ctx.db.seat().slot().update(p);
    presence_changed(ctx);
    Ok(())
}
#[spacetimedb::reducer]
pub fn input(
    ctx: &ReducerContext,
    epoch: u64,
    seq: u64,
    forward: i8,
    side: i8,
    turn: i8,
    look: i16,
    pitch: i16,
    button: String,
) -> Result<(), String> {
    let mut p = lease(ctx, epoch)?;
    if p.seq.checked_add(1) != Some(seq)
        || ![forward, side, turn].iter().all(|v| (-1..=1).contains(v))
        || ![look, pitch].iter().all(|v| (-1800..=1800).contains(v))
        || !matches!(
            button.as_str(),
            "" | "attack" | "switch" | "guard" | "jump" | "restart"
        )
    {
        return Err("invalid input".into());
    }
    if p.inputs.len() >= 64 {
        return Err("simulation input backlog".into());
    }
    p.inputs.push(Input {
        seq,
        forward,
        side,
        turn,
        look,
        pitch,
        button,
    });
    p.seq = seq;
    p.seen = ctx.timestamp.to_micros_since_unix_epoch();
    ctx.db.seat().slot().update(p);
    Ok(())
}

#[derive(SpacetimeType)]
pub struct Work {
    snapshot: String,
}
#[spacetimedb::view(accessor=simulation_work,public)]
pub fn work(ctx: &ViewContext) -> Vec<Work> {
    let Some(a) = ctx.db.arena().id().find(0) else {
        return vec![];
    };
    if a.worker != Some(ctx.sender()) {
        return vec![];
    }
    let seats = (1..=2)
        .filter_map(|slot| ctx.db.seat().slot().find(slot))
        .filter(|s| s.online)
        .map(|s| json!({"slot":s.slot,"epoch":s.epoch,"seq":s.seq,"inputs":s.inputs}))
        .collect::<Vec<_>>();
    if seats.is_empty() {
        return vec![];
    }
    vec![Work {
        snapshot: json!({"revision":a.revision,"state":a.state,"seats":seats}).to_string(),
    }]
}
#[derive(Deserialize)]
struct Output {
    actors: Vec<Actor>,
    #[serde(default)]
    contacts: Vec<Contact>,
    #[serde(default)]
    lower_poses: Vec<LowerPose>,
    caption: String,
    trails: Vec<Vec<f32>>,
}
#[derive(Deserialize)]
#[spacetimedb::table(accessor=contact,private)]
pub struct Contact {
    #[primary_key]
    slot: u8,
    tick: u64,
    x: f32,
    y: f32,
    z: f32,
}
#[derive(Deserialize)]
#[spacetimedb::table(accessor=lower_pose,private)]
pub struct LowerPose {
    #[primary_key]
    slot: u8,
    clip: String,
    serial: u64,
    elapsed_ms: u32,
    duration_ms: u32,
}
#[spacetimedb::reducer]
pub fn commit_frame(
    ctx: &ReducerContext,
    revision: u64,
    acks: Vec<u64>,
    state: String,
    output: String,
) -> Result<(), String> {
    let mut a = ctx.db.arena().id().find(0).ok_or("missing realm")?;
    if a.worker != Some(ctx.sender()) {
        return Err("simulation identity required".into());
    }
    let now = ctx.timestamp.to_micros_since_unix_epoch();
    if revision != a.revision || now - a.last_step < 40_000 {
        return Err("stale or early frame".into());
    }
    if acks.len() != 2 || state.len() > 65536 || output.len() > 32768 {
        return Err("frame budget".into());
    }
    if expire_sessions(ctx, now) {
        return Ok(());
    }
    let frame: Output = serde_json::from_str(&output).map_err(|_| "invalid scene")?;
    if frame.actors.len() > 32
        || frame.contacts.len() > frame.actors.len()
        || frame.caption.len() > 160
        || frame.trails.len() > 32
        || frame
            .trails
            .iter()
            .any(|v| v.len() != 6 || v.iter().any(|n| !n.is_finite() || n.abs() > 128.))
    {
        return Err("invalid scene bounds".into());
    }
    let mut ids = std::collections::BTreeSet::new();
    for actor in &frame.actors {
        if !(1..=32).contains(&actor.slot)
            || !ids.insert(actor.slot)
            || actor.style > 2
            || actor.clip.len() > 48
            || ![actor.x, actor.y, actor.z, actor.yaw, actor.pitch]
                .iter()
                .all(|n| n.is_finite() && n.abs() <= 128.)
        {
            return Err("invalid actor".into());
        }
    }
    for mut s in ctx.db.seat().iter().collect::<Vec<_>>() {
        let ack = acks[usize::from(s.slot - 1)];
        if s.online != ids.contains(&s.slot) || ack > s.seq {
            return Err("invalid frame membership".into());
        }
        s.inputs.retain(|i| i.seq > ack);
        ctx.db.seat().slot().update(s);
    }
    let mut contact_slots = std::collections::BTreeSet::new();
    for contact in frame.contacts {
        if !ids.contains(&contact.slot) || !contact_slots.insert(contact.slot)
            || [contact.x, contact.y, contact.z].iter().any(|v| !v.is_finite() || v.abs() > 128.) {
            return Err("invalid contact".into());
        }
        if ctx.db.contact().slot().find(contact.slot).is_some() {
            ctx.db.contact().slot().update(contact);
        } else {
            ctx.db.contact().insert(contact);
        }
    }
    for old in ctx.db.contact().iter().collect::<Vec<_>>() {
        if !contact_slots.contains(&old.slot) {
            ctx.db.contact().slot().delete(old.slot);
        }
    }
    let mut lower_slots = std::collections::BTreeSet::new();
    for pose in frame.lower_poses {
        if !(1..=2).contains(&pose.slot) || !ids.contains(&pose.slot)
            || !lower_slots.insert(pose.slot) || pose.clip.is_empty() || pose.clip.len() > 48
            || pose.duration_ms == 0 || pose.elapsed_ms >= pose.duration_ms {
            return Err("invalid lower pose".into());
        }
        if ctx.db.lower_pose().slot().find(pose.slot).is_some() {
            ctx.db.lower_pose().slot().update(pose);
        } else {
            ctx.db.lower_pose().insert(pose);
        }
    }
    for slot in 1..=2u8 {
        if !lower_slots.contains(&slot) {
            ctx.db.lower_pose().slot().delete(slot);
        }
    }
    a.revision += 1;
    a.last_step = now;
    a.state = state;
    a.actors = frame.actors;
    a.caption = frame.caption;
    a.trails = frame.trails;
    ctx.db.arena().id().update(a);
    Ok(())
}
#[derive(SpacetimeType)]
pub struct Avatar {
    slot: u8,
    mine: bool,
    epoch: u64,
    x: f32,
    y: f32,
    z: f32,
    yaw: f32,
    health: u8,
    energy: u8,
    style: u8,
    clip: String,
    serial: u64,
    elapsed_ms: u32,
    duration_ms: u32,
    prop_distance: f32,
    prop_height: f32,
    prop_roll: f32,
    pitch: f32,
    caption: String,
    trails: Vec<Vec<f32>>,
    lower_clip: String,
    lower_serial: u64,
    lower_elapsed_ms: u32,
    lower_duration_ms: u32,
    contact_tick: u64,
    contact_point: Vec<f32>,
}
#[spacetimedb::view(accessor=action_scene,public,primary_key=slot)]
pub fn scene(ctx: &ViewContext) -> Vec<Avatar> {
    let Some(me) = ctx
        .db
        .seat()
        .owner()
        .find(ctx.sender())
        .filter(|s| s.online)
    else {
        return vec![];
    };
    let a = ctx.db.arena().id().find(0).unwrap();
    let mut actors = a.actors;
    actors.retain(|p| p.slot > 2 || ctx.db.seat().slot().find(p.slot).is_some_and(|s| s.online));
    if !actors.iter().any(|p| p.slot == me.slot) {
        actors.push(Actor {
            slot: me.slot,
            x: 0.,
            y: 0.,
            z: 0.,
            yaw: 0.,
            health: 100,
            energy: 100,
            style: 0,
            clip: "idle".into(),
            serial: 0,
            elapsed_ms: 0,
            duration_ms: 0,
            pitch: 0.,
        });
    }
    actors
        .into_iter()
        .map(|p| {
            let mine = p.slot == me.slot;
            let lower = ctx.db.lower_pose().slot().find(p.slot);
            let contact = ctx.db.contact().slot().find(p.slot);
            Avatar {
                slot: p.slot,
                mine,
                epoch: if mine { me.epoch } else { 0 },
                x: p.x,
                y: p.y,
                z: p.z,
                yaw: p.yaw,
                health: p.health,
                energy: p.energy,
                style: p.style,
                clip: p.clip,
                serial: p.serial,
                elapsed_ms: p.elapsed_ms,
                duration_ms: p.duration_ms,
                prop_distance: 0.,
                prop_height: 0.,
                prop_roll: 0.,
                pitch: p.pitch,
                caption: if mine {
                    a.caption.clone()
                } else {
                    String::new()
                },
                trails: if mine { a.trails.clone() } else { vec![] },
                lower_serial: lower.as_ref().map_or(0, |l| l.serial),
                lower_elapsed_ms: lower.as_ref().map_or(0, |l| l.elapsed_ms),
                lower_duration_ms: lower.as_ref().map_or(0, |l| l.duration_ms),
                lower_clip: lower.map_or_else(String::new, |l| l.clip),
                contact_tick: contact.as_ref().map_or(0, |c| c.tick),
                contact_point: contact.map_or_else(Vec::new, |c| vec![c.x, c.y, c.z]),
            }
        })
        .collect()
}
