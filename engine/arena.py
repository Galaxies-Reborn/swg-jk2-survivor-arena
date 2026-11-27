"""GPL-2.0-only. Deterministic 20 Hz survivor simulation. All state is JSON checkpointable."""

import ctypes
import math
from pathlib import Path

DT = 0.05
EDGE = 3.0
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
VEC = ctypes.c_float * 3


class Kernel:
    def __init__(self, path):
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
        dx = forward * math.sin(p["yaw"]) - side * math.cos(p["yaw"])
        dz = forward * math.cos(p["yaw"]) + side * math.sin(p["yaw"])
        norm = max(1.0, math.hypot(dx, dz))
        v = VEC(p["vx"], 0, p["vz"])
        if p["y"] <= 0:
            friction = max(0.0, 1.0 - 6.0 * DT)
            v[0] *= friction
            v[2] *= friction
        self.lib.PM_Accelerate(
            v,
            VEC(dx / norm, 0, dz / norm),
            3.6 if (side or forward) else 0.0,
            8.0 if p["y"] <= 0 else 1.0,
            DT,
        )
        for axis, index in [("x", 0), ("z", 2)]:
            raw = p[axis] + v[index] * DT
            p[axis] = max(-EDGE, min(EDGE, raw))
            if raw != p[axis]:
                normal = VEC(0, 0, 0)
                normal[index] = -math.copysign(1, raw)
                self.lib.PM_ClipVelocity(v, normal, v, 1.001)
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


def spawn_wave(s):
    s["wave"] += 1
    count = min(2 + s["wave"], 12)
    s["enemies"] = []
    for i in range(count):
        angle = (i / count) * math.tau
        e = actor(
            3 + i, EDGE * 0.94 * math.sin(angle), EDGE * 0.94 * math.cos(angle), 2
        )
        e["health"] = min(100, 20 + 5 * s["wave"])
        s["enemies"].append(e)
    s["phase"] = "fighting"


def distance(a, b):
    return math.hypot(a["x"] - b["x"], a["z"] - b["z"])


def hurt(target, amount, now):
    target["health"] = max(0, target["health"] - amount)
    if not target["health"]:
        start(target, "down", 0, now)


def shoot(s, p, k, charge):
    cp = math.cos(p["pitch"])
    direction = [math.sin(p["yaw"]) * cp, math.sin(p["pitch"]), math.cos(p["yaw"]) * cp]
    s["bolts"].append(
        dict(
            pos=[p["x"], p["y"] + 1.42, p["z"]],
            dir=direction,
            damage=k.lib.bryar_damage(14, charge),
            life=15,
        )
    )
    start(p, "fire", 6, s["clock"])


def controls(s, p, seat, k):
    now = s["clock"]
    if p["epoch"] != seat["epoch"]:
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
    if p["duration"] and p["age"] >= p["duration"]:
        start(p, "idle", 0, now)
    if p["duration"]:
        for button in taps:
            if button in ("switch", "guard", "jump"):
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
            elif b == "jump" and p["y"] == 0:
                p["vy"] = 4.4
                p["y"] = 0.001
                start(p, "jump", 6, now)
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
                start(p, CUTS[cut], 9, now)
            if p["duration"]:
                break
        if not p["duration"]:
            p["clip"] = "walk" if p["forward"] or p["side"] else "idle"
    p["age"] += 1
    if now % 4 == 0:
        p["energy"] = min(100, p["energy"] + 1)


def melee(p, enemies, now):
    if p["clip"] not in CUTS or not 2 <= p["age"] <= 6:
        return
    start_yaw, end_yaw, top, bottom = SWEEPS[CUTS.index(p["clip"])]
    for sample in range(5):
        t = (p["age"] - 2 + sample / 4) / 5
        yaw = p["yaw"] - (start_yaw + (end_yaw - start_yaw) * t)
        height = p["y"] + top + (bottom - top) * t
        for e in enemies:
            if e["slot"] in p["landed"] or not e["health"]:
                continue
            dx, dz = e["x"] - p["x"], e["z"] - p["z"]
            along = max(0.0, min(1.65, dx * math.sin(yaw) + dz * math.cos(yaw)))
            if (
                0.2 <= height <= 1.82
                and math.hypot(dx - along * math.sin(yaw), dz - along * math.cos(yaw))
                <= 0.45
            ):
                hurt(e, 32, now)
                p["landed"].append(e["slot"])


def projectiles(s):
    trails = []
    surviving = []
    for b in s["bolts"]:
        origin = b["pos"]
        direction = b["dir"]
        length = 18.0 * DT
        end = [a + d * length for a, d in zip(origin, direction)]
        nearest = None
        best = length + 1.0
        for e in s["enemies"]:
            if not e["health"]:
                continue
            # A vertical body capsule. Find first contact along the bolt's swept segment.
            for j in range(17):
                t = length * j / 16
                if t >= best:
                    break
                x, y, z = [a + d * t for a, d in zip(origin, direction)]
                dy = y - max(0.32, min(1.45, y))
                if (x - e["x"]) ** 2 + (z - e["z"]) ** 2 + dy**2 <= 0.38**2:
                    nearest, best = e, t
                    break
        if nearest is not None:
            end = [a + d * best for a, d in zip(origin, direction)]
            hurt(nearest, b["damage"], s["clock"])
        elif (
            max(abs(end[0]), abs(end[2])) <= EDGE + 0.25
            and 0 < end[1] < 3.8
            and b["life"] > 1
        ):
            b.update(pos=end, life=b["life"] - 1)
            surviving.append(b)
        trails.append(origin + end)
    s["bolts"] = surviving
    return trails


def pursue(s, e, players):
    if not e["health"] or not players:
        return
    now = s["clock"]
    p = min(players, key=lambda a: (distance(e, a), a["slot"]))
    dx, dz = p["x"] - e["x"], p["z"] - e["z"]
    dist = math.hypot(dx, dz)
    e["yaw"] = math.atan2(dx, dz)
    e["age"] += 1
    if e["duration"] and e["age"] >= e["duration"]:
        start(e, "idle", 0, now)
    if e["duration"]:
        if e["clip"] == "overhead" and e["age"] == 6 and dist < 1.25 and p["y"] < 0.8:
            facing = -dx * math.sin(p["yaw"]) - dz * math.cos(p["yaw"])
            if p["clip"] == "guard" and facing >= 0.2 * dist:
                start(e, "stagger", 13, now)
            else:
                hurt(p, min(25, 6 + s["wave"]), now)
        return
    if dist <= 0.97:
        start(e, "overhead", 20, now)
    else:
        speed = min(2.3, 0.68 + s["wave"] * 0.10) * DT
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
            spawn_wave(s)
    if s["phase"] == "fighting":
        before = sum(e["health"] > 0 for e in s["enemies"])
        for p in active:
            if p["health"]:
                melee(p, s["enemies"], s["clock"])
        trails = projectiles(s)
        for e in s["enemies"]:
            pursue(s, e, [p for p in active if p["health"]])
        remaining = sum(e["health"] > 0 for e in s["enemies"])
        s["score"] += 100 * (before - remaining)
        if not any(p["health"] for p in active):
            s["phase"] = "defeat"
        elif not remaining:
            s.update(phase="intermission", wait=50, enemies=[], bolts=[])
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
    for a in active + enemies:
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
        view.update(
            elapsed_ms=min(a["age"], a["duration"]) * 50, duration_ms=a["duration"] * 50
        )
        actors.append(view)
    return s, dict(actors=actors, caption=caption, trails=trails)
