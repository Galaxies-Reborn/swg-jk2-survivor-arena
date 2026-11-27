"""Queries host-local walkable surfaces and passable interior portals."""

import json
import math
import heapq
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
        self.entrances = [
            self._entry_point(cell, vertices)
            for cell, vertices in data.get("entrances", [])
        ]
        if len(self.entrances) != 2:
            raise ValueError(
                "Cantina navigation requires its two exterior doors. Regenerate the navigation file with action-navigation."
            )

    def _entry_point(self, cell, vertices):
        x, z = (sum(p[i] for p in vertices) / len(vertices) for i in (0, 2))
        base = min(p[1] for p in vertices)
        edge = max(
            vertices,
            key=lambda p: math.hypot(p[0] - vertices[0][0], p[2] - vertices[0][2]),
        )
        dx, dz = edge[0] - vertices[0][0], edge[2] - vertices[0][2]
        length = math.hypot(dx, dz)
        for offset in (0.32, -0.32, 0.55, -0.55, 0.85, -0.85):
            px, pz = x + offset * dz / length, z - offset * dx / length
            spot = self.locate(px, pz, height=base)
            if (
                spot
                and spot[0] == cell
                and all(
                    self.locate(px + i, pz + j, cell, spot[1])
                    for i, j in ((0.19, 0), (-0.19, 0), (0, 0.19), (0, -0.19))
                )
            ):
                return dict(
                    x=px,
                    z=pz,
                    cell=cell,
                    ground=spot[1],
                    yaw=math.atan2(px - x, pz - z),
                )
        raise ValueError(f"No walkable entrance inside cell {cell}")

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
        wx, wz = self._portal_target(actor, target)
        goal = dict(x=wx, z=wz)
        route = actor.get("detour")
        if (
            not route
            or route["cell"] != actor.get("cell")
            or math.hypot(route["x"] - wx, route["z"] - wz) > 1.5
        ):
            if self._wide_clear(actor, goal):
                actor.pop("detour", None)
                return wx, wz
            route = dict(
                cell=actor.get("cell"),
                x=wx,
                z=wz,
                points=self._floor_route(actor, goal),
            )
            actor["detour"] = route
        points = route["points"]
        while (
            points
            and math.hypot(actor["x"] - points[0][0], actor["z"] - points[0][1]) < 0.26
        ):
            points.pop(0)
        return points[0] if points else (wx, wz)

    def _wide_clear(self, actor, target):
        dx, dz = target["x"] - actor["x"], target["z"] - actor["z"]
        length = math.hypot(dx, dz)
        if length < 0.01:
            return True
        for radius in (0, 0.22, -0.22):
            ox, oz = radius * dz / length, -radius * dx / length
            if not self.clear(
                dict(actor, x=actor["x"] + ox, z=actor["z"] + oz),
                dict(x=target["x"] + ox, z=target["z"] + oz),
            ):
                return False
        return True

    def _floor_route(self, actor, goal):
        # Search only the current room, on its native floor. Save the turning
        # points in the authoritative checkpoint so reconnects keep the route.
        sx, sz = actor["x"], actor["z"]
        cell = actor["cell"]
        todo = [(0.0, 0.0, 0, 0, actor["ground"])]
        previous = {(0, 0): None}
        costs = {(0, 0): 0.0}
        samples = {}

        def floor(i, j, height):
            key = (i, j, round(height * 2))
            if key not in samples:
                x, z = sx + i * 0.6, sz + j * 0.6
                at = self.locate(x, z, cell, height)
                samples[key] = (
                    at
                    if at
                    and at[0] == cell
                    and all(
                        self.locate(x + ox, z + oz, cell, at[1])
                        for ox, oz in ((0.22, 0), (-0.22, 0), (0, 0.22), (0, -0.22))
                    )
                    else None
                )
            return samples[key]

        while todo:
            _, cost, i, j, height = heapq.heappop(todo)
            if cost != costs[i, j]:
                continue
            x, z = sx + i * 0.6, sz + j * 0.6
            here = dict(x=x, z=z, cell=cell, ground=height)
            if math.hypot(x - goal["x"], z - goal["z"]) < 1.6 and self._wide_clear(
                here, goal
            ):
                path = []
                node = (i, j)
                while previous[node] is not None:
                    path.append([sx + node[0] * 0.6, sz + node[1] * 0.6])
                    node = previous[node]
                return path[::-1]
            for di, dj in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
                (1, 1),
                (1, -1),
                (-1, 1),
                (-1, -1),
            ):
                node = (i + di, j + dj)
                nx, nz = sx + node[0] * 0.6, sz + node[1] * 0.6
                dist = cost + 0.6 * math.hypot(di, dj)
                if dist >= costs.get(node, math.inf):
                    continue
                at = floor(*node, height)
                if not at or not floor(i + di / 2, j + dj / 2, height):
                    continue
                costs[node] = dist
                previous[node] = (i, j)
                heapq.heappush(
                    todo,
                    (
                        dist + math.hypot(nx - goal["x"], nz - goal["z"]),
                        dist,
                        *node,
                        at[1],
                    ),
                )
        return []

    def _portal_target(self, actor, target):
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
