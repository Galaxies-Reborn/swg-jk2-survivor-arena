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
    Kernel,
    controls,
    blade,
    blade_contact,
    actor,
    fresh,
    melee,
    projectiles,
    spawn_wave,
    step,
)
from server import Client, http, provision

ROOT = Path(__file__).resolve().parent


def rules(kernel):
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
    print(
        "PASS braking strafe into cut and buffered opposite/same-side links",
        returning,
        rewinding,
    )
    from engine.navigation import Navigation

    nav = Navigation(ROOT / ".local/navigation.json")
    k.navigation = nav
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
    assert e["health"] == 68
    rear = actor(4, 0, -1.0, 2)
    melee(p, [rear], 5)
    assert rear["health"] == 100
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
    assert blade_contact(((0, 0, 0), (1, 0, 0)), ((0.5, 1, -1), (0.5, 1, 1))) is None
    attacker.update(clip="overhead", age=4, duration=10, landed=[])
    defender = actor(1, 0, 0, 0)
    defender.update(clip="guard", age=2, duration=10)
    melee(attacker, [defender], 10)
    assert attacker["clip"] == "stagger" and defender["health"] == 100
    assert defender["riposte_until"] == 17
    duel["clock"] = 11
    seat = dict(
        slot=1,
        epoch=0,
        inputs=[dict(forward=1, side=0, turn=0, look=0, pitch=0, button="attack")],
    )
    controls(duel, defender, seat, k)
    assert (
        defender["clip"] == "up-right"
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

        proc = launch()
        try:
            # Wait for a published simulation frame before timing the jump.
            ready = time.monotonic() + 8
            while not any(
                r["mine"] and r["caption"].startswith("Wave ") for r in a.rows()
            ):
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
            b.send(pitch=-500)
            print("PASS peer-visible first-person pitch and jump", flush=True)
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
            while time.monotonic() - start < 32:
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
            end = time.monotonic() + 70
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
