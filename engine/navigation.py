"""Queries host-local walkable surfaces and passable interior portals."""

import json
import math
from collections import defaultdict, deque


class Navigation:
    def __init__(self, path):
        data = json.loads(path.read_bytes())
        self.bins = defaultdict(list)
        self.doors = defaultdict(list)
        for cell, points in data["floors"]:
            a, b, c = points
            det = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
            if abs(det) < 1e-6:
                continue
            xs, _, zs = zip(*points)
            for x in range(math.floor(min(xs) / 2), math.floor(max(xs) / 2) + 1):
                for z in range(math.floor(min(zs) / 2), math.floor(max(zs) / 2) + 1):
                    self.bins[x, z].append((cell, a, b, c, det))
        for a, b, vertices in data["portals"]:
            xs, _, zs = zip(*vertices)
            bounds = min(xs), max(xs), min(zs), max(zs)
            self.doors[a].append((b, bounds))
            self.doors[b].append((a, bounds))

    def locate(self, x, z, cell=None, height=0.0):
        options = []
        for candidate, a, b, c, det in self.bins.get(
            (math.floor(x / 2), math.floor(z / 2)), ()
        ):
            if (
                cell
                and candidate != cell
                and not any(
                    dest == candidate
                    and lo - 0.35 <= x <= hi + 0.35
                    and near - 0.35 <= z <= far + 0.35
                    for dest, (lo, hi, near, far) in self.doors[cell]
                )
            ):
                continue
            u = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / det
            v = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / det
            if min(u, v, 1 - u - v) >= -0.00001:
                y = u * a[1] + v * b[1] + (1 - u - v) * c[1]
                if not cell or abs(y - height) <= 0.45:
                    options.append((candidate != cell, abs(y - height), candidate, y))
        if options:
            return min(options)[2:]

    def slide(self, actor, dx, dz):
        cell, ground = actor.get("cell"), actor.get("ground", 0.0)
        moved = []
        for axis, delta in (("x", dx), ("z", dz)):
            if delta == 0 and cell is not None:
                moved.append(True)
                continue
            x = actor["x"] + (delta if axis == "x" else 0)
            z = actor["z"] + (delta if axis == "z" else 0)
            spot = self.locate(x, z, cell, ground)
            if spot and all(
                self.locate(x + i, z + j, spot[0], spot[1])
                for i, j in ((0.19, 0), (-0.19, 0), (0, 0.19), (0, -0.19))
            ):
                actor[axis] += delta
                cell, ground = spot
                actor.update(cell=cell, ground=ground)
                moved.append(True)
            else:
                moved.append(False)
        return moved

    def clear(self, actor, target):
        dx, dz = target["x"] - actor["x"], target["z"] - actor["z"]
        count = max(1, math.ceil(math.hypot(dx, dz) / 0.12))
        cell, ground = actor.get("cell"), actor.get("ground", 0.0)
        for step in range(1, count + 1):
            spot = self.locate(
                actor["x"] + dx * step / count,
                actor["z"] + dz * step / count,
                cell,
                ground,
            )
            if not spot:
                return False
            cell, ground = spot
        return True

    def waypoint(self, actor, target):
        source, goal = actor.get("cell"), target.get("cell")
        if not source or not goal or source == goal:
            return target["x"], target["z"]
        queue = deque([(source, None)])
        seen = {source}
        while queue:
            cell, first = queue.popleft()
            for dest, (lo, hi, near, far) in self.doors[cell]:
                if dest in seen:
                    continue
                point = first or ((lo + hi) / 2, (near + far) / 2)
                if dest == goal:
                    dx, dz = point[0] - actor["x"], point[1] - actor["z"]
                    if math.hypot(dx, dz) < 0.25:
                        # Pick a point just inside the next cell.
                        for ox, oz in ((0.32, 0), (-0.32, 0), (0, 0.32), (0, -0.32)):
                            at = self.locate(
                                point[0] + ox, point[1] + oz, source, actor["ground"]
                            )
                            if at and at[0] != source:
                                return point[0] + ox, point[1] + oz
                    return point
                seen.add(dest)
                queue.append((dest, point))
        return actor["x"], actor["z"]
