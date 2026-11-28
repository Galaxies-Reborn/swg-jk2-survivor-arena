"""GPL-2.0-only. Deterministic 20 Hz survivor simulation. All state is JSON checkpointable."""

import ctypes
import math
from pathlib import Path

DT = 0.05
JUMP_PREP = 4
JUMP_FLIGHT = 15
JUMP_TICKS = 36  # Includes crouch, flight, impact and the return to stance.
# Authored fast-style return lengths, rounded up to the next 50 ms snapshot.
RECOVERY_TICKS = {
    "cut-right": 8, "cut-left": 5, "up-right": 5, "up-left": 4,
    "down-right": 4, "down-left": 5, "overhead": 3,
}
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
# Clockwise around the blade plane, starting at the top. Check the finish of
# the current arc against the start of the requested arc, as in bg_saber.c.
QUADRANTS = dict(
    zip(CUTS, ((0, 4), (7, 3), (5, 1), (6, 2), (1, 5), (3, 7), (2, 6), (4, 4)))
)


def sector_delta(origin, target):
    return (target - origin + 4) % 8 - 4


def prepare_cut(p, origin, clip, now, steer_until):
    travel = math.ceil(abs(sector_delta(origin, QUADRANTS[clip][0])) / 2)
    if travel:
        start(p, "ready-" + clip, travel + 1, now)
        p.update(ready_origin=origin, steer_until=steer_until)
    else:
        attack(p, clip, 8, now, lead=1)


def directional_cut(k, p, enemies, forward, side):
    behind = (
        forward < 0
        and not side
        and any(
            e["health"]
            and distance(p, e) < 1.9
            and (e["x"] - p["x"]) * math.sin(p["yaw"])
            + (e["z"] - p["z"]) * math.cos(p["yaw"])
            < 0
            for e in enemies
        )
    )
    return CUTS[k.lib.saber_cut(forward, side, int(behind))]


def attack(p, clip, ticks, now, *, counter=False, lead=0):
    start(p, clip, ticks, now)
    p.update(
        age=lead,
        last_cut=clip,
        follow_until=now + ticks - lead + 2,
        strike_damage=44 if counter else 32,
        riposte_until=-1,
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
    a.pop("recovery_pose", None)
    a.pop("rebound_pose", None)
    a.pop("parry_pose", None)
    a.pop("ready_origin", None)
    a.pop("steer_until", None)
    if clip in ("stagger", "down"):
        if "lift_tick" in a or clip == "down":
            a.pop("takeoff", None)
            a.pop("jump_clip", None)
        a.pop("lift_tick", None)
        for field in (
            "queued",
            "queued_cut_axes",
            "follow_until",
            "last_cut",
            "strafe_tick",
        ):
            a.pop(field, None)
    a.update(clip=clip, duration=ticks, age=0, serial=now, landed=[])


def finish(a, now):
    cut = a["clip"]
    start(a, "idle", 0, now)
    if cut in RECOVERY_TICKS and a["style"] == 0:
        a["recovery_pose"] = ["recover-" + cut, now, RECOVERY_TICKS[cut]]


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
    s["arrivals"] = []
    for i in range(count):
        door = (i + s["wave"] - 1) % 2
        e = actor(3 + i, 0, (-1 if door else 1) * SPAWN_RADIUS, 2)
        if nav:
            e.update(nav.entrances[door])
        if i == 0 or (i >= 4 and i % 4 == 0):
            e.update(style=0, health=min(100, 52 + 4 * s["wave"]))
        else:
            e["health"] = min(100, 38 + 5 * s["wave"])
        if i < 2:
            s["enemies"].append(e)
        else:
            s["arrivals"].append(e)
    s["next_arrival"] = s["clock"] + 18
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
        p.pop("strafe_tick", None)
        p.pop("steer_until", None)
        p.pop("follow_until", None)
        p.pop("riposte_until", None)
        p.pop("queued", None)
        p.pop("queued_cut_axes", None)
        p.update(
            epoch=seat["epoch"], forward=0, side=0, turn=0, charge=0, last_input=-20
        )
    taps = []
    cut_axes = None
    release_axes = None
    steer_axes = None
    yaw_input = pitch_input = 0
    for i in seat["inputs"]:
        axes = (i["forward"], i["side"])
        if any(axes) and (axes != (p["forward"], p["side"]) or i["button"] == "attack"):
            steer_axes = axes
        p.update(forward=i["forward"], side=i["side"], turn=i["turn"], last_input=now)
        if i["side"]:
            p.update(strafe_axes=(i["forward"], i["side"]), strafe_tick=now)
        yaw_input += i["look"]
        pitch_input += i["pitch"]
        if i["button"]:
            taps.append(i["button"])
            if i["button"] == "attack" and cut_axes is None:
                cut_axes = (i["forward"], i["side"])
                if now - p.get("strafe_tick", -10) <= 2:
                    release_axes = p["strafe_axes"]
    yaw_delta = max(-1800, min(1800, yaw_input)) / 100
    pitch_delta = max(-1800, min(1800, pitch_input)) / 100
    p["yaw"] = math.remainder(p["yaw"] - math.radians(yaw_delta), math.tau)
    p["pitch"] = max(-1.28, min(1.28, p["pitch"] - math.radians(pitch_delta)))
    if now - p["last_input"] > 5:
        p.update(forward=0, side=0, turn=0)
    if not p["health"]:
        return
    if p["side"]:
        p.update(strafe_axes=(p["forward"], p["side"]), strafe_tick=now)
    p["yaw"] = math.remainder(p["yaw"] - 0.10 * p["turn"], math.tau)
    k.move(p, p["forward"], p["side"])
    p["y"] = max(0.0, p["y"] + DT * p["vy"])
    p["vy"] = p["vy"] - DT * 13.0 if p["y"] > 0 else 0.0
    if now >= p.get("lift_tick", math.inf):
        p.pop("lift_tick")
        p.update(vy=4.4, y=0.001)
    # Jump is a movement action; it must not lock out a mid-air saber attack.
    if (
        "jump" in taps
        and p["y"] == 0
        and "lift_tick" not in p
        and p["clip"] != "stagger"
    ):
        jump = "jump"
        if p["forward"] > 0:
            jump = "jump-run" if math.hypot(p["vx"], p["vz"]) >= 2.5 else "jump-forward"
        elif p["forward"] < 0:
            jump = "jump-back"
        p.update(lift_tick=now + JUMP_PREP, takeoff=now, jump_clip=jump)
        taps = [b for b in taps if b != "jump"]
        if (not p["duration"] or p["clip"].startswith("jump")) and "attack" not in taps:
            start(p, jump, JUMP_TICKS, now)
    if p["clip"].startswith("jump") and any(
        b in ("attack", "guard", "switch") for b in taps
    ):
        p["duration"] = 0
    current = p["clip"]
    if current.startswith("ready-") and current[6:] in CUTS:
        if "guard" in taps or "switch" in taps:
            taps = [button for button in taps if button in ("guard", "switch")]
            start(p, "idle", 0, now)
        else:
            dest = current[6:]
            redirect = (
                steer_axes
                and now <= p.get("steer_until", -1)
                and p["age"] < p["duration"]
            )
            desired = (
                directional_cut(k, p, s["enemies"], *steer_axes) if redirect else dest
            )
            if desired != dest:
                # Turn from the current blade sector; the original deadline
                # stays fixed so fresh input cannot prolong recovery forever.
                origin = p["ready_origin"]
                fraction = p["age"] / p["duration"]
                sector = (
                    origin + sector_delta(origin, QUADRANTS[dest][0]) * fraction
                ) % 8
                prepare_cut(p, sector, desired, now, p["steer_until"])
            elif p["age"] >= p["duration"]:
                attack(p, dest, 8, now, lead=1)
            taps.clear()
        current = p["clip"]
    if (
        current in CUTS
        and p["age"] >= math.ceil(p["duration"] * 0.35)
        and "attack" in taps
    ):
        if p.get("queued") in (None, "attack"):
            p["queued"] = "attack"
            if cut_axes != (0, 0) or not p.get("queued_cut_axes"):
                p["queued_cut_axes"] = cut_axes
    if current in CUTS and p.get("queued") == "attack" and steer_axes:
        p["queued_cut_axes"] = steer_axes
    requested = None
    if p["style"] == 0 and ("attack" in taps or p.get("queued") == "attack"):
        forward, side = (
            p.get("queued_cut_axes") or cut_axes or (p["forward"], p["side"])
        )
        following = p.get("follow_until", -1) >= now and p.get("last_cut") in RETURN_CUT
        if not (forward or side) and not following and release_axes is not None:
            forward, side = release_axes
        requested = (
            RETURN_CUT[p["last_cut"]]
            if following and not (forward or side)
            else directional_cut(k, p, s["enemies"], forward, side)
        )
    chained = (
        current in CUTS
        and requested is not None
        and p["age"] >= math.ceil(p["duration"] * 0.72)
        and p.get("queued") in (None, "attack")
        and "guard" not in taps
        and "switch" not in taps
    )
    if chained and QUADRANTS[current][1] != QUADRANTS[requested][0]:
        prepare_cut(p, QUADRANTS[current][1], requested, now, now + 2)
        p.pop("queued", None)
        p.pop("queued_cut_axes", None)
        taps.clear()
        chained = False
    counter = p.get("riposte_until", -1) >= now
    if chained or (counter and p["clip"] == "guard" and "attack" in taps):
        p["duration"] = 0
    if p["duration"] and p["age"] >= p["duration"]:
        finish(p, now)
    if p["duration"]:
        for button in taps:
            if button in ("switch", "guard") or (
                button == "attack"
                and p["duration"] - p["age"] <= 3
                and not p.get("queued")
            ):
                p["queued"] = button
                if button == "attack":
                    p["queued_cut_axes"] = cut_axes
                else:
                    p.pop("queued_cut_axes", None)
    else:
        queued = p.pop("queued", None)
        p.pop("queued_cut_axes", None)
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
                clip = requested
                if counter and forward > 0 and p["y"] == 0:
                    # Forward-only impulse, resolved by the regular movement and wall slide.
                    p["vx"] = 7.2 * math.sin(p["yaw"])
                    p["vz"] = 7.2 * math.cos(p["yaw"])
                    clip = "up-right" if side >= 0 else "up-left"
                ticks = 6 if counter else 8 if following else 10
                lateral_speed = -p["vx"] * math.cos(p["yaw"]) + p["vz"] * math.sin(
                    p["yaw"]
                )
                carrying = not following and side * lateral_speed > 0.7
                attack(
                    p,
                    clip,
                    ticks,
                    now,
                    counter=counter,
                    lead=int(not counter and (chained or carrying)),
                )
            if p["duration"]:
                break
        if not p["duration"] and "takeoff" in p:
            jump_age = now - p["takeoff"]
            if jump_age < JUMP_TICKS:
                # An overlapping cut can finish after touchdown. Resume the
                # remaining landing frames on the original jump timeline.
                start(p, p.get("jump_clip", "jump"), JUMP_TICKS, p["takeoff"])
                p["age"] = jump_age
            else:
                p.pop("takeoff")
                p.pop("jump_clip", None)
        if not p["duration"]:
            p["clip"] = "walk" if math.hypot(p["vx"], p["vz"]) > 0.18 else "idle"
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
    if actor["slot"] <= 2 and actor["duration"] == 8 and actor["clip"] in ("cut-left", "cut-right"):
        # The player's linked lateral cut carries its outgoing guard before
        # accelerating across the body. Keep both active-window boundaries.
        t *= 0.15 + 0.85 * t
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


def contact_traces(actor, now):
    event = actor.get("clash")
    if not event:
        return []
    tick, x, y, z = event
    age = now - tick
    hit = actor.get("contact_kind") == "hit"
    if age < 0 or age >= (3 if hit else 5):
        return []
    # Reconstruct short ballistic streaks from the resolved world-space point.
    # Tick and slot give every observer the same fan without advancing the RNG.
    heading = (tick * 0.73 + actor["slot"] * 1.63) % math.tau
    traces = []
    end = (age + 1) * DT
    start = end - 0.9 * DT
    for i in range(3 if hit else 6):
        angle = heading + i * 2.4
        speed = (1.1 + 0.42 * (i % 3)) * (0.75 if hit else 1.4)
        vx, vz = math.cos(angle) * speed, math.sin(angle) * speed
        vy = (0.65 + 0.55 * (i % 4)) * (0.8 if hit else 1.3)
        traces.append([
            x + vx * start, y + vy * start - 4.9 * start * start, z + vz * start,
            x + vx * end, y + vy * end - 4.9 * end * end, z + vz * end,
        ])
    return traces


def contact_side(fighter, x, z):
    lateral = (x - fighter["x"]) * math.cos(fighter["yaw"]) - (
        z - fighter["z"]
    ) * math.sin(fighter["yaw"])
    if abs(lateral) < 0.10:
        return "top"
    return "left" if lateral > 0 else "right"


def timed_parry(defender, now, x, z):
    pose = "parry-" + contact_side(defender, x, z)
    remaining = max(1, defender["duration"] - defender["age"])
    defender["parry_pose"] = [now, pose, remaining]
    defender["riposte_until"] = now + 7


def melee(p, enemies, now, nav=None):
    if p["clip"] not in CUTS:
        return
    # Keep the fast end of a return cut resolved when comparing two moving blades.
    intervals = 8 if p["clip"] in ("cut-left", "cut-right") or any(
        e["style"] == 0 and e["clip"] in ("cut-left", "cut-right") for e in enemies
    ) else 4
    for sample in range(intervals + 1):
        fraction = sample / intervals
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
                    for fighter, role in ((p, "rebound"), (e, "clash")):
                        side = contact_side(fighter, contact[0], contact[2])
                        start(fighter, "stagger", 4, now)
                        fighter["rebound_pose"] = role + "-" + side
                        fighter["follow_until"] = -1
                    e["clash"] = [now, *contact]
                    e["contact_kind"] = "clash"
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
            e["clash"] = [
                now, hilt[0] + along * dx,
                hilt[1] + along * (tip[1] - hilt[1]), hilt[2] + along * dz,
            ]
            if e["clip"] == "guard" and faces(e, p):
                e["contact_kind"] = "guard"
                if e["age"] <= 4:
                    # A timed block turns the defender's blade into the incoming
                    # cut. Its visual recovery can be interrupted by a riposte.
                    _, x, _, z = e["clash"]
                    timed_parry(e, now, x, z)
                    start(p, "stagger", 7, now)
                    p["rebound_pose"] = "blocked-" + contact_side(p, x, z)
                    p["follow_until"] = -1
                    push(p, e, 0.20, nav)
                    return
                continue
            e["contact_kind"] = "hit"
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


def staff_attack(s, e, players, nav):
    """Resolve the committed gaderffii blow in its original facing."""
    sweeping = e["clip"] == "staff-sweep"
    age = e["age"]
    fx, fz = math.sin(e["yaw"]), math.cos(e["yaw"])
    if not sweeping and 4 <= age <= 6:
        # Plant, drive forward, then strike. Stop short of a player's capsule.
        gap = min(distance(e, p) for p in players)
        advance = min(0.22, max(0.0, gap - 0.64))
        if nav:
            nav.slide(e, fx * advance, fz * advance)
        else:
            e["x"] += fx * advance
            e["z"] += fz * advance
    strike_age = 9 if sweeping else 7
    # The horizontal sweep reaches a raised saber one tick before the body.
    intercept = sweeping and age == strike_age - 1
    if not intercept and age != strike_age:
        return
    for p in sorted(players, key=lambda target: (distance(e, target), target["slot"])):
        dx, dz = p["x"] - e["x"], p["z"] - e["z"]
        forward, lateral = dx * fx + dz * fz, abs(dx * fz - dz * fx)
        if (forward < 0 or distance(e, p) > (1.60 if sweeping else 1.52)
                or lateral > (1.32 if sweeping else 0.52)
                or p["y"] > (0.52 if sweeping else 0.95)
                or (nav and not nav.clear(e, p))):
            continue
        now = s["clock"]
        gap = max(0.01, distance(e, p))
        x, z = p["x"] - 0.20 * dx / gap, p["z"] - 0.20 * dz / gap
        foot = p.get("ground", 0.) + p["y"]
        if p["clip"] == "guard" and faces(p, e):
            # Choose the reaction from the incoming strike at the body. The
            # blade intercepts it farther forward, before it reaches the torso.
            if p["age"] <= 4:
                timed_parry(p, now, x, z)
            reach = min(0.60, gap * 0.8)
            p["clash"] = [now, p["x"] - reach * dx / gap,
                          foot + (1.22 if sweeping else 1.45), p["z"] - reach * dz / gap]
            p["contact_kind"] = "guard"
            start(e, "stagger", 13, now)
            return
        if intercept:
            # The closest exposed target still gets the ordinary damage tick.
            return
        p["clash"] = [now, x, foot + (0.7 if sweeping else 1.1), z]
        p["contact_kind"] = "hit"
        hurt(p, min(30, (13 if sweeping else 16) + s["wave"]), now)
        if p["health"]:
            start(p, "stagger", 3, now)
            p["rebound_pose"] = "staff-impact"
        push(p, e, 0.12 if sweeping else 0.20, nav)
        return


def pursue(s, e, players, nav=None):
    if not e["health"] or not players:
        return
    now = s["clock"]
    p = min(players, key=lambda a: (distance(e, a), a["slot"]))
    dx, dz = p["x"] - e["x"], p["z"] - e["z"]
    dist = math.hypot(dx, dz)
    # Commit the facing at the beginning of a cut so its telegraph can be dodged.
    if e["clip"] not in CUTS and e["clip"] != "staff-sweep":
        e["yaw"] = math.atan2(dx, dz)
    e["age"] += 1
    if e["style"] == 0:
        if e["clip"] in CUTS:
            melee(e, players, now, nav)
        # Give the duelist two published ticks to show the knockaway before
        # starting its automatic counter. Player ripostes remain input-driven.
        parry = e.get("parry_pose")
        if (
            e.get("riposte_until", -1) >= now
            and e["clip"] == "guard"
            and (not parry or now - parry[0] >= 2)
        ):
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
        finish(e, now)
    if e["duration"]:
        if e["style"] == 2 and e["clip"] in ("overhead", "staff-sweep"):
            staff_attack(s, e, players, nav)
        return
    if dist <= (1.48 if e["style"] == 0 else 1.94) and (not nav or nav.clear(e, p)):
        if e["style"] == 0:
            sequence = ("down-right", "cut-left", "overhead", "up-right")
            index = e.get("cut_index", 0)
            start(e, sequence[index % len(sequence)], 13, now)
            e["cut_index"] = index + 1
        else:
            sweep = dist <= 1.48 and e.get("last_staff") == "overhead"
            attack_clip = "staff-sweep" if sweep else "overhead"
            start(e, attack_clip, 19 if sweep else 18, now)
            e["last_staff"] = attack_clip
    else:
        speed = (2.6 if e["style"] == 0 else min(3.6, 2.85 + s["wave"] * 0.12)) * DT
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
        if s.get("arrivals") and s["clock"] >= s["next_arrival"]:
            s["enemies"].append(s["arrivals"].pop(0))
            s["next_arrival"] = s["clock"] + 12
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
        elif not remaining and not s.get("arrivals"):
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
    bolt_trail_count = len(trails)
    contacts = []
    for a in active + enemies + falling:
        streaks = contact_traces(a, s["clock"])
        if streaks:
            tick, x, y, z = a["clash"]
            contacts.append(dict(slot=a["slot"], tick=tick, x=x, y=y, z=z))
        trails.extend(streaks[: max(0, 32 - len(trails))])
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
        if a["clip"] == "stagger" and "rebound_pose" in a:
            view["clip"] = a["rebound_pose"]
        view.update(
            elapsed_ms=min(
                max(0, a["age"] - int(a["clip"].startswith("jump"))), a["duration"]
            )
            * 50,
            duration_ms=a["duration"] * 50,
        )
        if a["clip"] in ("walk", "idle") and "recovery_pose" in a:
            clip, tick, duration = a["recovery_pose"]
            elapsed = s["clock"] - tick
            if 0 <= elapsed < duration:
                view.update(
                    clip=clip, serial=tick, elapsed_ms=elapsed * 50, duration_ms=duration * 50
                )
            else:
                a.pop("recovery_pose")
        if a["clip"] == "guard" and "parry_pose" in a:
            tick, clip, duration = a["parry_pose"]
            view.update(
                clip=clip,
                serial=tick,
                elapsed_ms=min(s["clock"] - tick, duration) * 50,
                duration_ms=duration * 50,
            )
        actors.append(view)
    lower_poses = [
        dict(slot=p["slot"], clip=p["jump_clip"], serial=p["takeoff"],
             elapsed_ms=(s["clock"] - p["takeoff"]) * 50, duration_ms=JUMP_TICKS * 50)
        for p in active if p["health"] and "jump_clip" in p and "takeoff" in p
        and 0 <= s["clock"] - p["takeoff"] < JUMP_TICKS
    ]
    return s, dict(actors=actors, caption=caption, trails=trails, lower_poses=lower_poses,
                   contacts=contacts, bolt_trail_count=bolt_trail_count)
