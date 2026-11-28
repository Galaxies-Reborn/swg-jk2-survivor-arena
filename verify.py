"""Run deterministic combat checks and a live two-identity survivor match."""

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from engine.arena import (
    CUTS,
    JUMP_TICKS,
    JUMP_PREP,
    JUMP_FLIGHT,
    RECOVERY_TICKS,
    Kernel,
    controls,
    blade,
    blade_contact,
    actor,
    attack,
    fresh,
    melee,
    projectiles,
    pursue,
    spawn_wave,
    step,
)
from server import Client, http, provision

ROOT = Path(__file__).resolve().parent


def staff_counters():
    def exchange(sweeping=False, dodge=0., jump=0., guard=False, turn=math.pi):
        s = fresh()
        s["wave"] = 1
        e = actor(3, 0., 0., 2)
        p = actor(1, 0., 1.35 if sweeping else 1.85, 0)
        if sweeping:
            e["last_staff"] = "overhead"
        pursue(s, e, [p])
        assert e["clip"] == ("staff-sweep" if sweeping else "overhead")
        p.update(x=dodge, y=jump, yaw=turn)
        for age in range(1, 10 if sweeping else 8):
            s["clock"] = age
            if guard and age == (9 if sweeping else 7):
                p.update(clip="guard", age=2, duration=10)
            pursue(s, e, [p])
            assert e["yaw"] == 0.
            if age < (9 if sweeping else 7):
                assert p["health"] == 100
        return e, p

    raider, target = exchange()
    assert target["health"] == 83 and raider["z"] > 0.6 and target["contact_kind"] == "hit"
    assert exchange(dodge=0.8)[1]["health"] == 100
    assert exchange(sweeping=True, dodge=0.65)[1]["health"] == 86
    assert exchange(sweeping=True, jump=0.6)[1]["health"] == 100
    raider, target = exchange(guard=True)
    assert target["health"] == 100 and target["contact_kind"] == "guard"
    assert raider["clip"] == "stagger" and target["riposte_until"] > 7
    assert target["parry_pose"] == [7, "parry-top", 8]
    _, x, y, z = target["clash"]
    assert math.isclose(y, 1.45) and math.isclose(target["z"] - z, 0.60)
    assert raider["z"] < z < target["z"] and x == target["x"]
    side_block = exchange(sweeping=True, dodge=0.83, guard=True)[1]
    assert side_block["parry_pose"] == [9, "parry-left", 8]
    assert exchange(guard=True, turn=0.)[1]["health"] == 83
    print("PASS committed Tusken lunge, sweep, sidestep, jump and facing parry")


def rules(kernel):
    staff_counters()
    for clip, sign in (("cut-left", 1), ("cut-right", -1)):
        fighter = actor(1, 0, 0, 0)
        fighter.update(clip=clip, duration=8)
        def at(phase):
            position = phase * fighter["duration"]
            fighter["age"] = math.floor(position) + 1
            return blade(fighter, position % 1)
        assert at(0.21) is None and at(0.73) is None
        assert at(0.54)[1][0] * sign < 0 < at(0.58)[1][0] * sign
    print("PASS linked saber sweeps cross forward during the displayed return cut")
    pack = json.loads((ROOT / "action-pack.json").read_bytes())
    swings = pack["view_swings"]
    for cut, opposite in (("cut-left", "cut-right"), ("cut-right", "cut-left")):
        start, end = swings[cut]
        assert end == swings[opposite][0] and swings["ready-" + cut] == [start, start]

        def blade_axis(angles):
            yaw, pitch, roll = map(math.radians, angles)
            x = -math.sin(roll)
            y = math.cos(pitch) * math.cos(roll)
            z = math.sin(pitch) * math.cos(roll)
            return (
                math.cos(yaw) * x + math.sin(yaw) * z,
                y,
                -math.sin(yaw) * x + math.cos(yaw) * z,
            )

        first, last = blade_axis(start), blade_axis(end)
        assert first[0] * last[0] < -0.25 and first[2] > 0.7

        # The entire grip orientation rotates about view Z: all three entries
        # of its depth row must be identical for start and finish. This makes
        # slerp preserve the depth of every point on the rigid blade.
        def depth_row(angles):
            yaw, pitch, roll = map(math.radians, angles)
            return (
                -math.sin(yaw) * math.cos(roll)
                + math.cos(yaw) * math.sin(pitch) * math.sin(roll),
                blade_axis(angles)[2],
                math.cos(yaw) * math.cos(pitch),
            )

        assert (
            max(abs(a - b) for a, b in zip(depth_row(start), depth_row(end))) < 0.00001
        )
    print(
        "PASS horizontal swipes cross the view, preserve blade depth, and share their return poses"
    )
    k = Kernel(kernel)
    v = actor(1, 0, 0, 0)
    k.move(v, 1, 1)
    assert 0 < math.hypot(v["vx"], v["vz"]) <= 3.6 and abs(v["vx"] + v["vz"]) < 0.001
    for _ in range(20):
        k.move(v, 1, 0)
    assert 5.7 < v["vz"] <= 5.81
    for _ in range(10):
        k.move(v, 0, 0)
    assert v["vx"] == v["vz"] == 0
    k.move(v, 0, 1)
    assert v["vx"] < 0 and v["vz"] == 0
    print("PASS ground acceleration, finite braking and strafe response")
    # A key-up can arrive in the same worker tick as the mouse-down.
    tap = dict(forward=0, side=1, turn=0, look=0, pitch=0, button="attack")
    release = dict(tap, side=0, button="")
    swing = actor(1, 0, 0, 0)
    controls(fresh(), swing, dict(epoch=0, inputs=[tap, release]), k)
    assert swing["clip"] == CUTS[k.lib.saber_cut(0, 1, 0)] and swing["side"] == 0
    neutral_attack = dict(release, button="attack")
    strafe = dict(tap, button="")
    for inputs, expected in (
        ([strafe, release, neutral_attack], "cut-right"),
        ([neutral_attack, strafe], "overhead"),
    ):
        swing = actor(1, 0, 0, 0)
        controls(fresh(), swing, dict(epoch=0, inputs=inputs), k)
        assert swing["clip"] == expected
    print("PASS directional swing survives same-tick strafe release")

    # Reverse during a committed preparation using movement alone. The current
    # strike stays immutable and the redirected preparation survives a reload.
    state = fresh()
    fighter = actor(1, 0, 0, 0)

    def advance(command):
        state["clock"] += 1
        controls(state, fighter, dict(epoch=0, inputs=[command]), k)

    advance(tap)
    opening = fighter["serial"]
    while fighter["clip"] == "cut-right" and state["clock"] < 15:
        advance(tap)
    assert fighter["clip"] == "ready-cut-right"
    advance(dict(strafe, side=-1))
    assert fighter["clip"] == "ready-cut-left" and blade(fighter, 0.5) is None
    fighter = json.loads(json.dumps(fighter))
    for _ in range(4):
        advance(release)
        if fighter["clip"] == "cut-left":
            break
    assert fighter["clip"] == "cut-left" and fighter["serial"] > opening
    print(
        "PASS movement-only redirect during saber preparation after checkpoint restore"
    )

    def combo(next_side):
        fighter = actor(1, 0, 0, 0)
        state = fresh()

        def frame(inputs):
            state["clock"] += 1
            controls(state, fighter, dict(epoch=0, inputs=inputs), k)

        frame([dict(tap, button="")])
        frame([release])
        assert fighter["clip"] == "walk" and fighter["side"] == 0
        frame([dict(release, button="attack")])
        assert fighter["clip"] == "cut-right" and fighter["vx"] < 0
        first = fighter["serial"]
        preparing = []
        for tick in range(1, 13):
            frame([dict(tap, side=next_side)] if tick == 5 else [release])
            if fighter["clip"].startswith("ready-"):
                assert blade(fighter, 0.5) is None
                preparing.append(fighter["clip"])
                fighter = json.loads(json.dumps(fighter))
            elif fighter["serial"] != first:
                return tick, fighter["clip"], preparing
        raise AssertionError("Buffered cut did not start")

    returning, mirrored, direct = combo(-1)
    rewinding, repeated, link = combo(1)
    assert not direct and link == ["ready-cut-right"] * 3
    assert (mirrored, repeated) == ("cut-left", "cut-right") and returning < rewinding
    # Action age includes the tick on which the first pose is published.
    # Check the configured endpoint against the next actual linked cut.
    for ticks, lead in [(6, 0), (8, 1), (10, 0), (10, 1)]:
        fighter = actor(1, 0, 0, 0)
        state = fresh()
        attack(fighter, "cut-right", ticks, state["clock"], lead=lead)
        fighter["age"] += 1
        first_age = fighter["age"]
        for elapsed in range(1, ticks + 1):
            state["clock"] += 1
            controls(state, fighter, dict(epoch=0, inputs=[dict(tap, side=-1)]), k)
            if fighter["clip"] == "cut-left":
                break
        assert fighter["clip"] == "cut-left"
        assert pack["cut_playback_ms"][str(ticks * 50)] == (first_age + elapsed) * 50
    print("PASS authored swing playback meets the first linked return cut for every player attack duration")
    print(
        "PASS braking strafe into cut and buffered opposite/same-side links",
        returning,
        rewinding,
    )
    from engine.navigation import Navigation

    nav = Navigation(ROOT / ".local/navigation.json")
    wave = fresh()
    spawn_wave(wave, nav)
    assert len(wave["enemies"]) == 2 and len(wave["arrivals"]) == 1
    for i, foe in enumerate(wave["enemies"] + wave["arrivals"]):
        door = nav.entrances[i % 2]
        assert all(foe[field] == door[field] for field in ("cell", "x", "z", "ground"))
    k.navigation = nav
    for forward, speed, clip in [
        (0, 0, "jump"),
        (-1, 0, "jump-back"),
        (1, 0, "jump-forward"),
        (1, 4, "jump-run"),
    ]:
        jumper = actor(1, -0.55, 0, 0)
        jumper["vz"] = speed
        seat = dict(
            slot=1,
            epoch=0,
            inputs=[
                dict(forward=forward, side=0, turn=0, look=0, pitch=0, button="jump")
            ],
        )
        state = fresh()
        for tick in range(JUMP_TICKS + 1):
            state["clock"] = tick
            controls(state, jumper, seat, k)
            seat["inputs"] = []
            assert (jumper["y"] > 0) == (JUMP_PREP <= tick < JUMP_PREP + JUMP_FLIGHT), (
                tick,
                jumper,
            )
            if tick < JUMP_TICKS:
                assert jumper["clip"] == clip, (tick, jumper)
        assert jumper["y"] == 0 and jumper["clip"] in ("walk", "idle")
    print("PASS standing, forward, running and backward jump clips through landing")
    for cut, duration in RECOVERY_TICKS.items():
        match = fresh()
        match.update(wait=100)
        fighter = actor(1, 0, 0, 0)
        fighter.update(clip=cut, age=10, duration=10)
        match["players"]["1"] = fighter
        seat = dict(slot=1, epoch=0, inputs=[])
        for age in range(duration + 1):
            match, frame = step(match, [seat], k)
            pose = frame["actors"][0]
            if age == duration:
                assert pose["clip"] == "idle"
            else:
                assert pose["clip"] == "recover-" + cut and pose["elapsed_ms"] == age * 50
                assert fighter["duration"] == 0 and blade(fighter, 0.5) is None
    print("PASS seven directional saber recoveries finish without extending the hit window")
    for action_tick in (0, JUMP_PREP + JUMP_FLIGHT - 3):
        jumper = actor(1, -0.55, 0, 0)
        state = fresh()
        saw_cut = saw_recovery = False
        for tick in range(JUMP_TICKS + 1):
            buttons = (["jump"] if tick == 0 else []) + (
                ["attack"] if tick == action_tick else []
            )
            inputs = [
                dict(forward=-1, side=0, turn=0, look=0, pitch=0, button=b)
                for b in buttons
            ]
            state["clock"] = tick
            controls(state, jumper, dict(slot=1, epoch=0, inputs=inputs), k)
            saw_cut |= jumper["clip"] in CUTS
            if saw_cut and jumper["clip"] not in CUTS and tick < JUMP_TICKS:
                assert jumper["clip"] == "jump-back", (tick, jumper)
                assert jumper["age"] == tick + 1
                saw_recovery |= jumper["y"] == 0
        assert saw_cut and saw_recovery and jumper["clip"] == "idle"
    print("PASS saber cut across touchdown resumes the original landing phase")
    v = actor(1, -0.55, 0.0, 0)
    for _ in range(30):
        k.move(v, 1, 0)
    assert v["cell"] == 3 and v["z"] > 4
    for _ in range(150):
        k.move(v, -1, 0)
    assert v["cell"] == 8 and -6.5 < v["z"] < -5.5
    print("PASS spawn to main room, return through portal, stop at solid wall")
    # Check the upstream clip response against a perpendicular wall.
    from engine.arena import VEC

    vel = VEC(6.0, 0.0, 0.0)
    k.lib.PM_ClipVelocity(vel, VEC(-1.0, 0.0, 0.0), vel, 1.001)
    assert abs(vel[0]) < 0.01
    assert k.lib.bryar_damage(14, 10) == 119 and k.lib.bryar_damage(14, 0) == 14
    assert sorted(
        set(k.lib.saber_cut(f, s, 1) for f in (-1, 0, 1) for s in (-1, 0, 1))
    ) == list(range(8))
    s = fresh()
    spawn_wave(s)
    p = actor(1, 0, 0, 0)
    e = actor(3, 0, 1.1, 2)
    p.update(clip="overhead", age=4)
    melee(p, [e], 5)
    melee(p, [e], 5)
    from engine.arena import contact_traces
    assert e["contact_kind"] == "hit" and len(contact_traces(e, 5)) == 3
    assert contact_traces(e, 6) != contact_traces(e, 5) and not contact_traces(e, 8)
    assert e["health"] == 68
    rear = actor(4, 0, -1.0, 2)
    melee(p, [rear], 5)
    assert rear["health"] == 100 and not contact_traces(rear, 5)
    s["enemies"] = [e]
    s["bolts"] = [dict(pos=[0, 1.4, 0.5], dir=[0, 0, 1], damage=14, life=3)]
    assert projectiles(s) and e["health"] == 54
    duel = fresh()
    spawn_wave(duel)
    assert duel["enemies"][0]["style"] == 0
    attacker = actor(3, 0, 1, 0)
    attacker.update(yaw=math.pi, clip="overhead", age=4, duration=10)
    crossed = actor(1, 0, 0, 0)
    crossed.update(clip="overhead", age=4, duration=10)
    melee(crossed, [attacker], 9)
    assert (crossed["clip"], attacker["clip"]) == ("stagger", "stagger")
    assert crossed["health"] == attacker["health"] == 100 and attacker["clash"][0] == 9
    assert (crossed["rebound_pose"], attacker["rebound_pose"]) == ("rebound-top", "clash-top")
    for fighter in (crossed, attacker):
        cue = fighter["rebound_pose"]
        assert (cue in pack["sounds"]) == (cue in pack["contact_clips"]) == cue.startswith("clash-")
        assert cue in pack["clips"] and cue in pack["view_swings"]
    assert blade_contact(((0, 0, 0), (1, 0, 0)), ((0.5, 1, -1), (0.5, 1, 1))) is None
    attacker.update(clip="overhead", age=4, duration=10, landed=[])
    defender = actor(1, 0, 0, 0)
    defender.update(clip="guard", age=2, duration=10)
    melee(attacker, [defender], 10)
    assert attacker["clip"] == "stagger" and defender["health"] == 100
    assert defender["contact_kind"] == "guard" and len(contact_traces(defender, 10)) == 6
    assert defender["riposte_until"] == 17
    for cut, side in (("cut-left", "right"), ("cut-right", "left"), ("overhead", "top")):
        striker = actor(3, 0, 1, 0)
        striker.update(yaw=math.pi, clip=cut, age=4, duration=10)
        guard = actor(1, 0, 0, 0)
        guard.update(clip="guard", age=2, duration=10)
        melee(striker, [guard], 10)
        blocked = "blocked-" + side
        assert striker["rebound_pose"] == blocked and striker["duration"] == 7
        assert blocked in pack["sounds"] and blocked not in pack["contact_clips"]
        assert guard["health"] == 100 and guard["riposte_until"] == 17
        for tick in (10, 11, 12):
            duel["clock"] = tick
            pursue(duel, guard, [striker])
            assert guard["clip"] == ("guard" if tick < 12 else "cut-left")
    for offset, clip in ((-0.25, "parry-right"), (0, "parry-top"), (0.25, "parry-left")):
        foe = actor(3, offset, 1, 0)
        foe.update(yaw=math.pi, clip="overhead", age=4, duration=10)
        guard = actor(1, 0, 0, 0)
        guard.update(clip="guard", age=2, duration=10)
        melee(foe, [guard], 10)
        match = fresh()
        match.update(clock=10, players={"1": guard})
        _, frame = step(match, [dict(slot=1, epoch=0, inputs=[])], k)
        pose = frame["actors"][0]
        assert (pose["clip"], pose["serial"], pose["elapsed_ms"]) == (clip, 10, 50)
        assert pose["health"] == 100 and guard["clip"] == "guard"
    duel["clock"] = 11
    seat = dict(
        slot=1,
        epoch=0,
        inputs=[dict(forward=1, side=0, turn=0, look=0, pitch=0, button="attack")],
    )
    controls(duel, defender, seat, k)
    assert (
        defender["clip"] == "up-right"
        and "parry_pose" not in defender
        and defender["duration"] == 6
        and defender["vz"] > 6
    )
    defender.update(age=4)
    melee(defender, [attacker], 12)
    assert attacker["health"] == 56
    defender.update(age=5)
    seat["inputs"][0]["forward"] = 0
    controls(duel, defender, seat, k)
    assert defender["clip"] == "down-left" and defender["duration"] == 8
    attacker.update(clip="overhead", duration=10, age=4, landed=[], x=0, z=1)
    defender.update(clip="guard", age=1, yaw=math.pi, x=0, z=0)
    melee(attacker, [defender], 14)
    assert defender["health"] < 100
    print(
        "PASS duelist spawn, frontal parry, forward riposte, return cut, rear guard exposure"
    )
    p.update(health=0, clip="down")
    s["players"] = {"1": p}
    s, _ = step(s, [dict(slot=1, epoch=2, seq=0, inputs=[])], k)
    assert s["phase"] == "defeat"
    print(
        "PASS JK2 movement, wall clipping, directional saber volumes, Bryar scaling, projectile sweep and party defeat",
        flush=True,
    )


class Seat:
    def __init__(self, profile):
        self.client = Client(profile)
        self.enter()

    def enter(self):
        self.client.call("enter", ["jk2-survivor-v1"])
        self.epoch = next(r["epoch"] for r in self.rows() if r["mine"])
        self.seq = 0

    def rows(self):
        return self.client.rows("action_scene")

    def send(self, button="", forward=0, side=0, look=0, pitch=0):
        self.client.call(
            "input", [self.epoch, self.seq + 1, forward, side, 0, look, pitch, button]
        )
        self.seq += 1

    def leave(self):
        self.client.call("leave", [self.epoch])


def denied(call):
    try:
        call()
    except HTTPError as e:
        if 400 <= e.code < 600:
            return
        raise
    raise AssertionError("Expected server rejection")


def verify_staff_publication(worker, a, b, kernel):
    # Publish a sequence of real simulated frames through the authority boundary.
    # Place the target at staff range to exercise both attacks in a short test.
    state = fresh()
    raider = actor(4, -0.55, 1.65, 2)
    state.update(phase="fighting", wave=1, enemies=[raider])
    seen = set()
    saw_parry = False
    for _ in range(56):
        enemy = state["enemies"][0]
        guarding = (("staff-sweep", 69) in seen and enemy["clip"] == "overhead"
                    and enemy["age"] == 5 and not saw_parry)
        a.send("guard" if guarding else "")
        b.send()
        job = json.loads(worker.rows("simulation_work")[0]["snapshot"])
        state, frame = step(state, job["seats"], kernel)
        time.sleep(0.05)
        seq = [0, 0]
        for seat in job["seats"]:
            seq[seat["slot"] - 1] = seat["seq"]
        worker.call("commit_frame", [job["revision"], seq, json.dumps(state), json.dumps(frame)])
        observations = []
        for seat in (a, b):
            rows = seat.rows()
            raider_row = next(r for r in rows if r["slot"] == 4)
            target = next(r for r in rows if r["slot"] == 1)
            observations.append((raider_row["clip"], target["health"], target["contact_point"], target["clip"]))
        assert observations[0] == observations[1]
        clip, health, point, target_clip = observations[0]
        if clip == "stagger" and target_clip == "parry-top" and health == 69 and point:
            assert math.isclose(point[1] - target["y"], 1.45, abs_tol=1e-4)
            assert 0.45 < math.hypot(point[0] - target["x"], point[2] - target["z"]) <= 0.601
            saw_parry = True
        if point:
            seen.add((clip, health))
    assert ("staff-strike", 83) in seen and ("staff-sweep", 69) in seen, seen
    assert saw_parry, seen
    print("PASS both identities observe Tusken lunge, sweep and timed overhead parry")


def verify_contact_publication(worker, a, b, kernel):
    # Resolve a real blade clash before stepping the normal scene publisher.
    state = fresh()
    state["clock"] = 9
    target = actor(3, 0, 1, 0)
    target.update(yaw=math.pi, clip="overhead", age=4, duration=10)
    striker = actor(1, 0, 0, 0)
    striker.update(clip="overhead", age=4, duration=10)
    melee(striker, [target], state["clock"])
    tick, *point = target["clash"]
    state["enemies"] = [target]
    state.update(phase="fighting", bolts=[dict(pos=[3., 1.4, 0.], dir=[1., 0., 0.], damage=14, life=15)])
    for age in (1, 2, 5):
        state["clock"] = tick + age - 1
        job = json.loads(worker.rows("simulation_work")[0]["snapshot"])
        state, frame = step(state, job["seats"], kernel)
        time.sleep(0.05)
        if age == 1:
            invalid = dict(frame, contacts=[dict(frame["contacts"][0], slot=32)])
            denied(lambda: worker.call("commit_frame", [
                job["revision"], [0, 0], json.dumps(state), json.dumps(invalid)
            ]))
        worker.call("commit_frame", [job["revision"], [0, 0], json.dumps(state), json.dumps(frame)])
        for seat in (a, b):
            rows = seat.rows()
            owner = next(r for r in rows if r["mine"])
            assert owner["bolt_trail_count"] == 1 and owner["trails"][0][0] >= 3.
            row = next(r for r in rows if r["slot"] == 3)
            assert row["bolt_trail_count"] == 0
            if age < 5:
                assert len(owner["trails"]) == 7
                assert row["contact_tick"] == tick and len(row["contact_point"]) == 3
                assert math.dist(row["contact_point"], point) < 1e-5
            else:
                assert row["contact_point"] == []
    job = json.loads(worker.rows("simulation_work")[0]["snapshot"])
    state, frame = step(fresh(), job["seats"], kernel)
    time.sleep(0.05)
    worker.call("commit_frame", [job["revision"], [0, 0], json.dumps(state), json.dumps(frame)])
    print("PASS two players receive the resolved blade crossing and its expiry", flush=True)


def live(endpoint, wasm, kernel):
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp) / "profiles"
        db = provision(endpoint, wasm, directory)

        def profile(n):
            return json.loads((directory / (n + ".json")).read_bytes())

        a, b = Seat(profile("player-1")), Seat(profile("player-2"))
        worker = Client(profile("worker"))
        outsider = Client(
            dict(
                endpoint=endpoint,
                database=db,
                token=http(endpoint + "/v1/identity")["token"],
            )
        )
        assert (
            outsider.rows("action_scene") == []
            and a.client.rows("simulation_work") == []
        )
        denied(lambda: outsider.call("enter", ["jk2-survivor-v1"]))
        denied(lambda: a.client.call("commit_frame", [0, [0, 0], "{}", "{}"]))
        denied(lambda: outsider.rows("arena"))
        denied(lambda: outsider.rows("contact"))
        denied(lambda: a.client.rows("contact"))
        old = b.epoch
        b.enter()
        denied(lambda: b.client.call("input", [old, 1, 0, 0, 0, 0, 0, "attack"]))
        denied(lambda: a.client.call("input", [a.epoch, 7, 0, 0, 0, 0, 0, "attack"]))

        def launch():
            return subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "server.py"),
                    "serve",
                    "--profile",
                    str(directory / "worker.json"),
                    "--kernel",
                    str(kernel.resolve()),
                ]
            )

        verify_staff_publication(worker, a, b, Kernel(kernel.resolve()))
        verify_contact_publication(worker, a, b, Kernel(kernel.resolve()))
        staged_revision = json.loads(worker.rows("simulation_work")[0]["snapshot"])["revision"]
        proc = launch()
        try:
            # The contact checks already published a Wave caption. Wait for
            # the live worker to advance the realm before timing the jump.
            ready = time.monotonic() + 8
            while json.loads(worker.rows("simulation_work")[0]["snapshot"])["revision"] == staged_revision:
                assert proc.poll() is None and time.monotonic() < ready, (
                    "worker startup"
                )
                a.send()
                b.send()
                time.sleep(0.05)
            b.send("jump", pitch=500)
            aim_time = time.monotonic() + 2
            while time.monotonic() < aim_time:
                peer = next((r for r in a.rows() if r["slot"] == 2), None)
                if peer and peer["y"] > 0.05 and peer["pitch"] < -0.05:
                    break
                time.sleep(0.025)
            assert peer and peer["y"] > 0.05 and peer["pitch"] < -0.05
            assert peer["lower_clip"] == "jump"
            takeoff = peer["lower_serial"]
            b.send("attack", pitch=-500)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                peer = next(r for r in a.rows() if r["slot"] == 2)
                if peer["clip"] in CUTS:
                    break
                time.sleep(0.025)
            assert peer["clip"] in CUTS and peer["lower_serial"] == takeoff
            assert peer["lower_clip"] == "jump" and 0 < peer["lower_elapsed_ms"] < peer["lower_duration_ms"]
            print("PASS peer-visible pitch and independent jump phase during a saber cut", flush=True)
            from engine.navigation import Navigation

            entrances = Navigation(ROOT / ".local/navigation.json").entrances
            seen = set()
            deadline = time.monotonic() + 5
            while len(seen) < 3 and time.monotonic() < deadline:
                for foe in a.rows():
                    slot = foe["slot"]
                    if slot in (3, 4, 5) and slot not in seen:
                        entrance = entrances[(slot - 3) % 2]
                        assert (
                            math.hypot(
                                foe["x"] - entrance["x"], foe["z"] - entrance["z"]
                            )
                            < 0.8
                        )
                        seen.add(slot)
                a.send()
                b.send()
                time.sleep(0.04)
            assert seen == {3, 4, 5}
            print(
                "PASS both doorway origins and staggered reinforcement replicated",
                flush=True,
            )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                b.send(forward=1)
                a.send()
                peer = next(r for r in a.rows() if r["slot"] == 2)
                if peer["z"] > 4.0:
                    break
                time.sleep(0.05)
            assert peer["z"] > 4.0
            while time.monotonic() < deadline:
                b.send(forward=-1)
                a.send()
                if next(r for r in a.rows() if r["slot"] == 2)["z"] < 1.0:
                    break
                time.sleep(0.05)
            b.send()
            print(
                "PASS second identity observed traversing the main-room doorway",
                flush=True,
            )
            start = time.monotonic()
            switched = False
            reached = False
            resumed = False
            seen_hit = False
            seen_trail = False
            while time.monotonic() - start < 100:
                rows = a.rows()
                me = next(r for r in rows if r["mine"])
                peer = next((r for r in rows if r["slot"] == 2), None)
                if peer:
                    assert not peer["mine"] and peer["epoch"] == 0
                foes = [r for r in rows if r["slot"] >= 3 and r["health"]]
                if "Wave 2 " in me["caption"]:
                    assert "Score 300" in me["caption"]
                    assert "Wave 2 " in next(
                        r["caption"] for r in b.rows() if r["mine"]
                    )
                    reached = True
                    break
                button = ""
                turn = 0
                if not switched:
                    button = "switch"
                    switched = True
                elif foes:
                    target = min(
                        foes,
                        key=lambda e: math.hypot(e["x"] - me["x"], e["z"] - me["z"]),
                    )
                    angle = math.atan2(target["x"] - me["x"], target["z"] - me["z"])
                    delta = math.remainder(me["yaw"] - angle, math.tau)
                    turn = round(max(-1800, min(1800, math.degrees(delta) * 100)))
                    if abs(delta) < 0.2:
                        button = "attack"
                    seen_hit |= any(e["health"] < 25 for e in foes)
                a.send(button, look=turn)
                b.send("attack" if foes else "")
                seen_trail |= bool(me["trails"])
                if foes and seen_hit and not resumed:
                    proc.terminate()
                    proc.wait(timeout=4)
                    before = json.loads(worker.rows("simulation_work")[0]["snapshot"])
                    assert json.loads(before["state"])["wave"] == 1
                    proc = launch()
                    resumed = True
                time.sleep(0.045)
            assert reached and resumed and seen_hit and seen_trail, (
                reached,
                resumed,
                seen_hit,
                seen_trail,
                me,
            )
            print(
                "PASS two identities, access limits, stale input rejection, live blaster hits, shared wave and score, worker restart",
                flush=True,
            )
            # Let both seats fall, then request a clean match using only player controls.
            end = time.monotonic() + 140
            while time.monotonic() < end:
                a.send()
                b.send()
                own = next(r for r in a.rows() if r["mine"])
                if "Party defeated" in own["caption"]:
                    break
                time.sleep(0.12)
            assert "Party defeated" in own["caption"]
            a.send("restart")
            time.sleep(0.2)
            own = next(r for r in b.rows() if r["mine"])
            assert own["health"] == 100 and "Score 0" in own["caption"]
            a.send("attack", side=1)
            end = time.monotonic() + 1.5
            opener = None
            while time.monotonic() < end:
                peer = next(r for r in b.rows() if r["slot"] == 1)
                if (
                    peer["clip"] == "cut-right"
                    and peer["elapsed_ms"] >= 250
                    and opener is None
                ):
                    opener = peer["serial"]
                    a.send("attack", side=-1)
                if (
                    opener is not None
                    and peer["serial"] > opener
                    and peer["clip"] == "cut-left"
                ):
                    break
                time.sleep(0.012)
            else:
                raise AssertionError(
                    "Peer did not observe the strafe cut and buffered return"
                )
            print(
                "PASS second player observed right strafe cut into left return",
                flush=True,
            )
            previous = peer["serial"]
            ordered = False
            saw_ready = False
            end = time.monotonic() + 1.2
            while time.monotonic() < end:
                peer = next(r for r in b.rows() if r["slot"] == 1)
                if (
                    peer["serial"] == previous
                    and peer["elapsed_ms"] >= 150
                    and not ordered
                ):
                    a.send("attack", side=-1)
                    ordered = True
                saw_ready |= peer["clip"] == "ready-cut-left"
                if (
                    saw_ready
                    and peer["clip"] == "cut-left"
                    and peer["serial"] > previous
                ):
                    break
                time.sleep(0.012)
            else:
                raise AssertionError(
                    "Peer did not see the blade recover before the repeated cut"
                )
            print("PASS peer-visible blade recovery and repeated left cut", flush=True)
            previous = peer["serial"]
            ordered = redirected = False
            end = time.monotonic() + 1.4
            while time.monotonic() < end:
                peer = next(r for r in b.rows() if r["slot"] == 1)
                if (
                    not ordered
                    and peer["serial"] == previous
                    and peer["elapsed_ms"] >= 150
                ):
                    a.send("attack", side=-1)
                    ordered = True
                elif not redirected and peer["clip"] == "ready-cut-left":
                    a.send(side=1)
                    redirected = True
                elif redirected and peer["clip"] == "cut-right":
                    break
                time.sleep(0.008)
            else:
                raise AssertionError("Peer did not observe the movement-only redirect")
            print("PASS peer-visible mid-combo strafe reversal", flush=True)
            b.leave()
            assert b.rows() == []
            assert not any(r["slot"] == 2 for r in a.rows())
            proc.terminate()
            proc.wait(timeout=4)
            time.sleep(5.6)
            assert a.rows() == [] and worker.rows("simulation_work") == []
            print(
                "PASS party wipe, peer-visible restart, leave and database-enforced expiry with worker stopped",
                flush=True,
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=4)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kernel", type=Path, required=True)
    ap.add_argument("--wasm", type=Path)
    ap.add_argument("--endpoint", default="http://127.0.0.1:38431")
    args = ap.parse_args()
    rules(args.kernel)
    if args.wasm:
        live(args.endpoint, args.wasm, args.kernel)
