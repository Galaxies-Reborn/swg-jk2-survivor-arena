"""Build private Survivor Arena saber motion from a player's JK2 installation."""

import argparse
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from zipfile import ZipFile

# These are action identifiers from the JK2 multiplayer fast saber style.
ACTIONS = {
    "cut-right": "BOTH_A1__L__R",
    "cut-left": "BOTH_A1__R__L",
    "up-right": "BOTH_A1_BL_TR",
    "up-left": "BOTH_A1_BR_TL",
    "down-right": "BOTH_A1_TL_BR",
    "down-left": "BOTH_A1_TR_BL",
    "overhead": "BOTH_A1_T__B_",
    "ready-cut-right": "BOTH_T1__R__L",
    "ready-cut-left": "BOTH_T1__L__R",
}
BONES = [
    "rhumerus",
    "rradius",
    "rhand",
    "r_d2_j1",
    "r_d5_j1",
    "lhumerus",
    "lradius",
    "lhand",
    "thoracic",
]


def product(a, b):
    return [
        [
            sum(a[i][k] * b[k][j] for k in range(3)) + (a[i][3] if j == 3 else 0)
            for j in range(4)
        ]
        for i in range(3)
    ]


def decode(data, cfg):
    header = struct.Struct("<4si64sf6i")
    ident, version, _, _, frames, indices, bones, pool, _, end = header.unpack_from(
        data
    )
    if (ident, version) != (b"2LGA", 6) or not (
        0 < bones <= 256
        and end == len(data)
        and header.size + 4 * bones <= indices < pool <= end
        and indices + frames * bones * 3 <= pool
    ):
        raise ValueError("Unsupported humanoid GLA")
    skeleton = []
    names = {}
    for i in range(bones):
        off = header.size + struct.unpack_from("<i", data, header.size + 4 * i)[0]
        if not header.size + 4 * bones <= off <= indices - 168:
            raise ValueError("Invalid skeleton offset")
        raw, _, parent = struct.unpack_from("<64sii", data, off)
        name = raw.split(b"\0")[0].decode("ascii")
        if parent < -1 or parent >= bones or name in names:
            raise ValueError("Invalid bone")
        names[name] = i
        skeleton.append(
            (
                parent,
                [
                    list(struct.unpack_from("<4f", data, off + 72 + 16 * j))
                    for j in range(3)
                ],
            )
        )

    def pose(frame):
        cache = {}

        def skin(i, depth=0):
            if i in cache:
                return cache[i]
            if depth >= bones:
                raise ValueError("Cyclic skeleton")
            addr = indices + 3 * (frame * bones + i)
            index = int.from_bytes(data[addr : addr + 3], "little")
            qw, qx, qy, qz, tx, ty, tz = struct.unpack_from(
                "<7H", data, pool + 14 * index
            )
            w, x, y, z = [v / 16383.0 - 2 for v in (qw, qx, qy, qz)]
            local = [
                [
                    1 - 2 * (y * y + z * z),
                    2 * (x * y - z * w),
                    2 * (x * z + y * w),
                    tx / 64.0 - 512,
                ],
                [
                    2 * (x * y + z * w),
                    1 - 2 * (x * x + z * z),
                    2 * (y * z - x * w),
                    ty / 64.0 - 512,
                ],
                [
                    2 * (x * z - y * w),
                    2 * (y * z + x * w),
                    1 - 2 * (x * x + y * y),
                    tz / 64.0 - 512,
                ],
            ]
            parent = skeleton[i][0]
            cache[i] = product(skin(parent, depth + 1), local) if parent >= 0 else local
            return cache[i]

        points = {}
        for name in BONES:
            i = names[name]
            mat = product(skin(i), skeleton[i][1])
            x, y, z = [row[3] for row in mat]
            points[name] = (x, z, -y)  # model left, up, forward

        def diff(a, b):
            return [points[a][i] - points[b][i] for i in range(3)]

        def unit(a, b):
            d = diff(a, b)
            n = math.hypot(*d)
            if n < 0.001:
                raise ValueError("Degenerate arm segment")
            return [round(x / n, 6) for x in d]

        return dict(
            arms=[
                unit("rradius", "rhumerus"),
                unit("rhand", "rradius"),
                unit("lradius", "lhumerus"),
                unit("lhand", "lradius"),
            ],
            blade=unit("r_d2_j1", "r_d5_j1"),
            hand=[round(x / 40, 6) for x in diff("rhand", "thoracic")],
        )

    entries = {
        parts[0]: parts[1:]
        for line in cfg.decode("latin1").splitlines()
        if len(parts := line.split()) >= 5
    }
    clips = {}
    for clip, name in ACTIONS.items():
        first, count, _, fps = map(float, entries[name][:4])
        if not (
            first.is_integer()
            and count.is_integer()
            and first >= 0
            and 1 < count <= 128
            and first + count <= frames
            and 0 < abs(fps) <= 120
        ):
            raise ValueError(f"Unsupported frame range for {name}")
        track = [pose(i) for i in range(int(first), int(first + count))]
        clips[clip] = dict(fps=abs(fps), frames=track if fps > 0 else track[::-1])
    return clips


def prepare(game, client):
    base = game / "GameData" / "base"
    assets = {}
    paths = [
        "models/players/_humanoid/_humanoid.gla",
        "models/players/_humanoid/animation.cfg",
    ]
    for archive in sorted(base.glob("assets*.pk3")):
        with ZipFile(archive) as z:
            for path in paths:
                if path in z.namelist():
                    if z.getinfo(path).file_size > 32 * 1024 * 1024:
                        raise ValueError("Retail input exceeds budget")
                    assets[path] = z.read(path)
    if len(assets) != 2:
        raise ValueError("JK2 humanoid animation assets missing")
    clips = decode(*(assets[path] for path in paths))
    dest = client / ".local" / "action-motion" / "jk2-survivor-v1.json"
    dest.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, delete=False, mode="w", encoding="utf8"
    ) as f:
        json.dump(clips, f, separators=(",", ":"), allow_nan=False)
    os.replace(f.name, dest)
    print(f"Prepared {len(clips)} retail saber tracks at {dest}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("game", type=Path)
    p.add_argument("client", type=Path)
    args = p.parse_args()
    prepare(args.game, args.client)
