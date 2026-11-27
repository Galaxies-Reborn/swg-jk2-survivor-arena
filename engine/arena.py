"""GPL-2.0-only. Deterministic 20 Hz survivor simulation. All state is JSON checkpointable."""

import ctypes
import math
from pathlib import Path

DT = 0.05
SPAWN_RADIUS = 2.8
CUTS = (
    "overhead",
    "down-right",
    "up-right",
    "cut-right",
    "down-left",
    "up-left",
    "cut-left",
    "back",
)
SWEEPS = (
    (0, 0, 1.75, 0.65),
    (-1.0, 0.9, 1.7, 0.65),
    (-1.0, 0.9, 0.65, 1.7),
    (-1.35, 1.35, 1.15, 1.15),
    (1.0, -0.9, 1.7, 0.65),
    (1.0, -0.9, 0.65, 1.7),
    (1.35, -1.35, 1.15, 1.15),
    (math.pi - 0.55, math.pi + 0.55, 1.0, 1.0),
)
# A connected cut can reverse along its incoming arc. Neutral input alternates
# sides after the opener. Directional input still selects the next JK2 cut.
RETURN_CUT = dict(
    zip(
        CUTS,
        (
            "cut-right",
            "up-left",
            "down-left",
            "cut-left",
            "up-right",
            "down-right",
            "cut-right",
            "overhead",
        ),
    )
)
VEC = ctypes.c_float * 3


class Kernel:
    def __init__(self, path, navigation=None):
        self.navigation = navigation
        self.lib = ctypes.CDLL(str(Path(path).resolve()))
        vec = ctypes.POINTER(ctypes.c_float)
        self.lib.PM_Accelerate.argtypes = [
            vec,
            vec,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
        ]
        self.lib.PM_Accelerate.restype = None
        self.lib.PM_ClipVelocity.argtypes = [vec, vec, vec, ctypes.c_float]
        self.lib.PM_ClipVelocity.restype = None
        self.lib.saber_cut.argtypes = [ctypes.c_int] * 3
        self.lib.bryar_damage.argtypes = [ctypes.c_int] * 2

    def move(self, p, forward, side):
        if not (forward or side or p["vx"] or p["vz"]) and (
            not self.navigation or p.get("cell") is not None
        ):
            return
        dx = forward * math.sin(p["yaw"]) - side * math.cos(p["yaw"])
        dz = forward * math.cos(p["yaw"]) + side * math.sin(p["yaw"])
        norm = max(1.0, math.hypot(dx, dz))
        v = VEC(p["vx"], 0, p["vz"])
        # JK2 PM_Friction uses a minimum control speed, so releasing a key
        # brakes to a stop. Subdivide the realm tick for stable turns/contacts.
        grounded = p["y"] <= 0
        step = DT / 5
        wish = VEC(dx / norm, 0, dz / norm)
        for _ in range(5):
            speed = math.hypot(v[0], v[2])
            if grounded and speed:
                stop_speed = 5.8 * (100 / 250)
                friction = max(0.0, speed - 6 * max(speed, stop_speed) * step) / speed
                v[0] *= friction
                v[2] *= friction
            self.lib.PM_Accelerate(
                v,
                wish,
                5.8 if (side or forward) else 0.0,
                10.0 if grounded else 1.0,
                step,
            )
            if self.navigation:
                clear = self.navigation.slide(p, v[0] * step, v[2] * step)
                for index, allowed in zip((0, 2), clear):
                    if not allowed:
                        normal = VEC(0, 0, 0)
                        normal[index] = -math.copysign(1, v[index])
                        self.lib.PM_ClipVelocity(v, normal, v, 1.001)
            else:
                p["x"] += v[0] * step
                p["z"] += v[2] * step
        p["vx"], p["vz"] = v[0], v[2]


def actor(slot, x, z, style):
    return dict(
        slot=slot,
        x=x,
        y=0.0,
        z=z,
        yaw=0.0,
        pitch=0.0,
        health=100,
        energy=100,
        style=style,
        clip="idle",
        serial=0,
        age=0,
        duration=0,
        vx=0.0,
        vy=0.0,
        vz=0.0,
        landed=[],
        epoch=0,
        forward=0,
        side=0,
        turn=0,
        last_input=-20,
        charge=0,
    )


def start(a, clip, ticks, now):
    a.update(clip=clip, duration=ticks, age=0, serial=now, landed=[])


def fresh():
    return dict(
        clock=0,
        wave=0,
        score=0,
        phase="intermission",
        wait=30,
        players={},
        enemies=[],
        bolts=[],
    )


def spawn_wave(s, nav=None):
    s["wave"] += 1
    count = min(2 + s["wave"], 12)
    s["enemies"] = []
    for i in range(count):
        angle = (i / count) * math.tau
        e = actor(
            3 + i, SPAWN_RADIUS * math.sin(angle), SPAWN_RADIUS * math.cos(angle), 2
        )
        if nav:
            players = list(s["players"].values())
            anchor = players[i % len(players)] if players else dict(x=0.0, z=0.0)
            for radius in (5.5, 4.0, 2.8, 1.6, 0.8, 0.0):
                x, z = (
                    anchor["x"] + radius * math.sin(angle),
                    anchor["z"] + radius * math.cos(angle),
                )
                surface = nav.locate(
                    x, z, anchor.get("cell"), anchor.get("ground", 0.0)
                )
                if surface:
                    e.update(x=x, z=z, cell=surface[0], ground=surface[1])
                    break
        if i == 0 or (i >= 4 and i % 4 == 0):
            e.update(style=0, health=min(100, 52 + 4 * s["wave"]))
        else:
            e["health"] = min(100, 20 + 5 * s["wave"])
        s["enemies"].append(e)
    s["phase"] = "fighting"


def distance(a, b):
    return math.hypot(a["x"] - b["x"], a["z"] - b["z"])


def hurt(target, amount, now):
    target["health"] = max(0, target["health"] - amount)
    if not target["health"]:
        start(target, "down", 16, now)


def shoot(s, p, k, charge):
    cp = math.cos(p["pitch"])
    direction = [math.sin(p["yaw"]) * cp, math.sin(p["pitch"]), math.cos(p["yaw"]) * cp]
    s["bolts"].append(
        dict(
            pos=[p["x"], p["y"] + p.get("ground", 0.0) + 1.42, p["z"]],
            dir=direction,
            cell=p.get("cell"),
            ground=p.get("ground", 0.0),
            damage=k.lib.bryar_damage(14, charge),
            life=15,
        )
    )
    start(p, "fire", 6, s["clock"])


def controls(s, p, seat, k):
    now = s["clock"]
    if p["epoch"] != seat["epoch"]:
        p.pop("follow_until", None)
        p.pop("riposte_until", None)
        p.pop("queued", None)
        p.update(
            epoch=seat["epoch"], forward=0, side=0, turn=0, charge=0, last_input=-20
        )
    taps = []
    yaw_input = pitch_input = 0
    for i in seat["inputs"]:
        p.update(forward=i["forward"], side=i["side"], turn=i["turn"], last_input=now)
        yaw_input += i["look"]
        pitch_input += i["pitch"]
        if i["button"]:
            taps.append(i["button"])
    yaw_delta = max(-1800, min(1800, yaw_input)) / 100
    pitch_delta = max(-1800, min(1800, pitch_input)) / 100
    p["yaw"] = math.remainder(p["yaw"] - math.radians(yaw_delta), math.tau)
    p["pitch"] = max(-1.28, min(1.28, p["pitch"] - math.radians(pitch_delta)))
    if now - p["last_input"] > 5:
        p.update(forward=0, side=0, turn=0)
    if not p["health"]:
        return
    p["yaw"] = math.remainder(p["yaw"] - 0.10 * p["turn"], math.tau)
    k.move(p, p["forward"], p["side"])
    p["y"] = max(0.0, p["y"] + DT * p["vy"])
    p["vy"] = p["vy"] - DT * 13.0 if p["y"] > 0 else 0.0
    # Jump is a movement action; it must not lock out a mid-air saber attack.
    if "jump" in taps and p["y"] == 0 and p["clip"] != "stagger":
        p.update(vy=4.4, y=0.001)
        taps = [b for b in taps if b != "jump"]
        if not p["duration"] and "attack" not in taps:
            start(p, "jump", 6, now)
    if p["clip"] == "jump" and "attack" in taps:
        p["duration"] = 0
    chained = (
        p["clip"] in CUTS
        and p["age"] >= max(2, p["duration"] - 2)
        and ("attack" in taps or p.get("queued") == "attack")
    )
    counter = p.get("riposte_until", -1) >= now
    if chained or (counter and p["clip"] == "guard" and "attack" in taps):
        p["duration"] = 0
    if p["duration"] and p["age"] >= p["duration"]:
        start(p, "idle", 0, now)
    if p["duration"]:
        for button in taps:
            if button in ("switch", "guard") or (
                button == "attack"
                and p["duration"] - p["age"] <= 3
                and not p.get("queued")
            ):
                p["queued"] = button
    else:
        queued = p.pop("queued", None)
        if queued:
            taps.insert(0, queued)
        for b in taps:
            if b == "switch":
                p["style"] = 1 - p["style"]
                p["charge"] = 0
                start(p, "idle", 3, now)
            elif b == "guard" and p["style"] == 0 and p["energy"] >= 8:
                p["energy"] -= 8
                start(p, "guard", 10, now)
            elif b == "guard" and p["style"] == 1:
                p["charge"] = now or 1
            elif b == "attack" and p["style"] == 1:
                levels = min(5, max(1, (now - p["charge"]) // 4)) if p["charge"] else 0
                shoot(s, p, k, levels)
                p["charge"] = 0
            elif b == "attack":
                behind = any(
                    e["health"] > 0
                    and distance(p, e) < 1.9
                    and (e["x"] - p["x"]) * math.sin(p["yaw"])
                    + (e["z"] - p["z"]) * math.cos(p["yaw"])
                    < 0
                    for e in s["enemies"]
                )
                cut = k.lib.saber_cut(p["forward"], p["side"], int(behind))
                clip = CUTS[cut]
                following = (
                    p.get("follow_until", -1) >= now and p.get("last_cut") in RETURN_CUT
                )
                if following and not (p["forward"] or p["side"]):
                    clip = RETURN_CUT[p["last_cut"]]
                if counter and p["forward"] > 0 and p["y"] == 0:
                    # Forward-only impulse, resolved by the regular movement and wall slide.
                    p["vx"] = 7.2 * math.sin(p["yaw"])
                    p["vz"] = 7.2 * math.cos(p["yaw"])
                    clip = "up-right" if p["side"] >= 0 else "up-left"
                ticks = 6 if counter else 8 if following else 10
                start(p, clip, ticks, now)
                p.update(
                    last_cut=clip,
                    follow_until=now + ticks + 2,
                    strike_damage=44 if counter else 32,
                    riposte_until=-1,
                )
            if p["duration"]:
                break
        if not p["duration"]:
            p["clip"] = "walk" if p["forward"] or p["side"] else "idle"
    p["age"] += 1
    if now % 4 == 0:
        p["energy"] = min(100, p["energy"] + 1)


def faces(actor, target):
    dx, dz = target["x"] - actor["x"], target["z"] - actor["z"]
    return dx * math.sin(actor["yaw"]) + dz * math.cos(
        actor["yaw"]
    ) >= 0.3 * math.hypot(dx, dz)


def push(actor, attacker, amount, nav):
    dx, dz = actor["x"] - attacker["x"], actor["z"] - attacker["z"]
    scale = amount / max(0.01, math.hypot(dx, dz))
    if nav:
        nav.slide(actor, dx * scale, dz * scale)
    else:
        actor["x"] += dx * scale
        actor["z"] += dz * scale


def blade(actor, frame_fraction):
    """Return a hilt and tip at the same sub-frame time for both combatants."""
    if actor["clip"] not in CUTS:
        return None
    phase = (actor["age"] - 1 + frame_fraction) / (actor["duration"] or 9)
    if not 0.22 <= phase <= 0.72:
        return None
    t = (phase - 0.22) / 0.50
    start_yaw, end_yaw, top, bottom = SWEEPS[CUTS.index(actor["clip"])]
    yaw = actor["yaw"] - start_yaw - (end_yaw - start_yaw) * t
    pitch = actor["pitch"]
    y = actor["y"] + actor.get("ground", 0.0) + top + (bottom - top) * t

    def point(distance):
        horizontal = distance * math.cos(pitch)
        return (
            actor["x"] + horizontal * math.sin(yaw),
            y + distance * math.sin(pitch),
            actor["z"] + horizontal * math.cos(yaw),
        )

    return point(0.18), point(1.65)


def blade_contact(a, b):
    # Closest points on two finite 3D segments, including parallel overlap.
    (p, q), (r, s) = a, b
    u = tuple(y - x for x, y in zip(p, q))
    v = tuple(y - x for x, y in zip(r, s))
    w = tuple(x - y for x, y in zip(p, r))

    def dot(x, y):
        return sum(i * j for i, j in zip(x, y))

    def unit(t):
        return min(1.0, max(0.0, t))

    uu, uv, vv, uw, vw = dot(u, u), dot(u, v), dot(v, v), dot(u, w), dot(v, w)
    det = uu * vv - uv * uv
    along = unit((uv * vw - vv * uw) / det) if det > 1e-8 else 0.0
    other = unit((uv * along + vw) / vv)
    along = unit((uv * other - uw) / uu)
    left = tuple(x + along * d for x, d in zip(p, u))
    right = tuple(x + other * d for x, d in zip(r, v))
    if math.dist(left, right) <= 0.095:
        return [(x + y) * 0.5 for x, y in zip(left, right)]


def melee(p, enemies, now, nav=None):
    if p["clip"] not in CUTS:
        return
    for sample in range(5):
        fraction = sample / 4
        segment = blade(p, fraction)
        if not segment:
            continue
        hilt, tip = segment
        for e in enemies:
            if e["slot"] in p["landed"] or not e["health"]:
                continue
            if distance(p, e) > 3.4:
                continue
            other = blade(e, fraction) if e["style"] == 0 else None
            if other:
                contact = blade_contact(segment, other)
                if contact and (not nav or nav.clear(p, e)):
                    for fighter in (p, e):
                        start(fighter, "stagger", 4, now)
                        fighter["follow_until"] = -1
                    e["clash"] = [now, *contact]
                    return
            dx, dz = tip[0] - hilt[0], tip[2] - hilt[2]
            along = max(
                0.0,
                min(
                    1.0,
                    ((e["x"] - hilt[0]) * dx + (e["z"] - hilt[2]) * dz)
                    / (dx * dx + dz * dz),
                ),
            )
            height = (
                hilt[1] + along * (tip[1] - hilt[1]) - e["y"] - e.get("ground", 0.0)
            )
            if not (
                0.1 <= height <= 1.88
                and math.hypot(
                    e["x"] - hilt[0] - along * dx, e["z"] - hilt[2] - along * dz
                )
                <= 0.45
            ):
                continue
            if nav and not nav.clear(p, e):
                continue
            p["landed"].append(e["slot"])
            if e["clip"] == "guard" and faces(e, p):
                e["clash"] = [
                    now,
                    (p["x"] + e["x"]) * 0.5,
                    e.get("ground", 0.0) + e["y"] + 1.2,
                    (p["z"] + e["z"]) * 0.5,
                ]
                if e["age"] <= 4:
                    start(p, "stagger", 7, now)
                    p["follow_until"] = -1
                    e["riposte_until"] = now + 7
                    push(p, e, 0.20, nav)
                    return
                continue
            damage = p.get("strike_damage", 32 if p["slot"] <= 2 else 12)
            hurt(e, damage, now)
            if e["health"]:
                start(e, "stagger", 4, now)
                e["follow_until"] = -1
            push(e, p, 0.18, nav)


def projectiles(s, nav=None):
    trails = []
    surviving = []
    for b in s["bolts"]:
        origin = b["pos"]
        direction = b["dir"]
        length = 18.0 * DT
        end = [a + d * length for a, d in zip(origin, direction)]
        nearest = None
        best = length + 1.0
        wall = length + 1.0
        if nav:
            for j in range(1, 18):
                t = length * j / 17
                x, y, z = (a + d * t for a, d in zip(origin, direction))
                surface = nav.locate(x, z, b.get("cell"), b.get("ground", 0.0))
                if not surface or y < surface[1]:
                    wall = t
                    end = [x, y, z]
                    break
                b["cell"], b["ground"] = surface
        for e in s["enemies"]:
            if not e["health"]:
                continue
            # A vertical body capsule. Find first contact along the bolt's swept segment.
            for j in range(17):
                t = length * j / 16
                if t >= min(best, wall):
                    break
                x, y, z = [a + d * t for a, d in zip(origin, direction)]
                local_y = y - e.get("ground", 0.0)
                dy = local_y - max(0.32, min(1.45, local_y))
                if (x - e["x"]) ** 2 + (z - e["z"]) ** 2 + dy**2 <= 0.38**2:
                    nearest, best = e, t
                    break
        if nearest is not None:
            end = [a + d * best for a, d in zip(origin, direction)]
            hurt(nearest, b["damage"], s["clock"])
        elif (
            wall > length
            and b.get("ground", 0.0) < end[1] < b.get("ground", 0.0) + 3.8
            and b["life"] > 1
        ):
            b.update(pos=end, life=b["life"] - 1)
            surviving.append(b)
        trails.append(origin + end)
    s["bolts"] = surviving
    return trails


def pursue(s, e, players, nav=None):
    if not e["health"] or not players:
        return
    now = s["clock"]
    p = min(players, key=lambda a: (distance(e, a), a["slot"]))
    dx, dz = p["x"] - e["x"], p["z"] - e["z"]
    dist = math.hypot(dx, dz)
    # Commit the facing at the beginning of a cut so its telegraph can be dodged.
    if e["clip"] not in CUTS:
        e["yaw"] = math.atan2(dx, dz)
    e["age"] += 1
    if e["style"] == 0:
        if e["clip"] in CUTS:
            melee(e, players, now, nav)
        if e.get("riposte_until", -1) >= now and e["clip"] == "guard":
            start(e, "cut-left", 9, now)
            e["riposte_until"] = -1
        if (
            not e["duration"]
            and p["clip"] in CUTS
            and dist < 1.9
            and now >= e.get("guard_ready", 0)
            and faces(e, p)
        ):
            start(e, "guard", 7, now)
            e["guard_ready"] = now + 28
            return
    if e["duration"] and e["age"] >= e["duration"]:
        start(e, "idle", 0, now)
    if e["duration"]:
        if (
            e["style"] == 2
            and e["clip"] == "overhead"
            and e["age"] == 6
            and dist < 1.25
            and p["y"] < 0.8
            and (not nav or nav.clear(e, p))
        ):
            facing = -dx * math.sin(p["yaw"]) - dz * math.cos(p["yaw"])
            if p["clip"] == "guard" and facing >= 0.2 * dist:
                start(e, "stagger", 13, now)
                if p["age"] <= 4:
                    p["riposte_until"] = now + 7
            else:
                hurt(p, min(25, 6 + s["wave"]), now)
        return
    if dist <= (1.48 if e["style"] == 0 else 0.97) and (not nav or nav.clear(e, p)):
        if e["style"] == 0:
            sequence = ("down-right", "cut-left", "overhead", "up-right")
            index = e.get("cut_index", 0)
            start(e, sequence[index % len(sequence)], 13, now)
            e["cut_index"] = index + 1
        else:
            start(e, "overhead", 20, now)
    else:
        speed = (2.6 if e["style"] == 0 else min(2.3, 1.05 + s["wave"] * 0.10)) * DT
        if nav:
            wx, wz = nav.waypoint(e, p)
            dx, dz = wx - e["x"], wz - e["z"]
            length = max(speed, math.hypot(dx, dz))
            dx, dz = dx / length, dz / length
            for ally in s["enemies"]:
                if ally is e or not ally["health"] or ally.get("cell") != e.get("cell"):
                    continue
                sx, sz = e["x"] - ally["x"], e["z"] - ally["z"]
                gap = math.hypot(sx, sz)
                if 0.001 < gap < 0.75:
                    weight = (0.75 - gap) / gap
                    dx += sx * weight
                    dz += sz * weight
            length = max(1.0, math.hypot(dx, dz))
            nav.slide(e, dx / length * speed, dz / length * speed)
            e["yaw"] = math.atan2(dx, dz)
        else:
            e["x"] += dx / max(0.001, dist) * speed
            e["z"] += dz / max(0.001, dist) * speed
        e["clip"] = "walk"


def step(state, seats, k):
    s = state or fresh()
    if s["phase"] == "defeat" and any(
        i["button"] == "restart" for seat in seats for i in seat["inputs"]
    ):
        old = s["clock"]
        s = fresh()
        s["clock"] = old
    s["clock"] += 1
    active = []
    for seat in seats:
        n = seat["slot"]
        p = s["players"].setdefault(
            str(n), actor(n, (-0.55 if n == 1 else 0.55), 0.0, 0)
        )
        active.append(p)
        if s["phase"] != "defeat":
            controls(s, p, seat, k)
    trails = []
    if s["phase"] == "intermission":
        s["wait"] -= 1
        if s["wait"] <= 0:
            spawn_wave(s, k.navigation)
    if s["phase"] == "fighting":
        before = sum(e["health"] > 0 for e in s["enemies"])
        for p in active:
            if p["health"]:
                melee(p, s["enemies"], s["clock"], k.navigation)
        trails = projectiles(s, k.navigation)
        for e in s["enemies"]:
            pursue(s, e, [p for p in active if p["health"]], k.navigation)
        remaining = sum(e["health"] > 0 for e in s["enemies"])
        s["score"] += 100 * (before - remaining)
        if not any(p["health"] for p in active):
            s["phase"] = "defeat"
        elif not remaining:
            s.update(phase="intermission", wait=50, bolts=[])
            for p in s["players"].values():
                p["health"] = min(100, p["health"] + 35)
                start(p, "idle", 0, s["clock"])
    enemies = [e for e in s["enemies"] if e["health"]]
    caption = f"Wave {s['wave']}  |  {len(enemies)} raiders  |  Score {s['score']}"
    if s["phase"] == "intermission":
        caption += f"  |  Next wave in {math.ceil(s['wait'] * DT)}s"
    if s["phase"] == "defeat":
        caption += "  |  Party defeated. Press R to restart."
    actors = []
    falling = [
        e
        for e in s["enemies"]
        if not e["health"]
        and 0 <= s["clock"] - e["serial"] < 16
        and e["clip"] == "down"
    ]
    for e in falling:
        e["age"] = s["clock"] - e["serial"]
    for a in active + enemies + falling:
        clash = a.get("clash")
        if clash and len(trails) <= 29 and 0 <= s["clock"] - clash[0] <= 2:
            tick, x, y, z = clash
            spread = 0.07 + 0.055 * (s["clock"] - tick)
            for dx, dy, dz in ((1, 0.8, -0.4), (-0.7, 0.6, 0.9), (0.2, -1, 0.8)):
                trails.append(
                    [
                        x + dx * spread,
                        y + dy * spread,
                        z + dz * spread,
                        x + dx * (spread + 0.12),
                        y + dy * (spread + 0.12),
                        z + dz * (spread + 0.12),
                    ]
                )
        view = {
            name: a[name]
            for name in (
                "slot",
                "x",
                "y",
                "z",
                "yaw",
                "pitch",
                "health",
                "energy",
                "style",
                "clip",
                "serial",
            )
        }
        view["y"] += a.get("ground", 0.0)
        if a["style"] == 2 and a["clip"] == "overhead":
            view["clip"] = "staff-strike"
        view.update(
            elapsed_ms=min(a["age"], a["duration"]) * 50, duration_ms=a["duration"] * 50
        )
        actors.append(view)
    return s, dict(actors=actors, caption=caption, trails=trails)
