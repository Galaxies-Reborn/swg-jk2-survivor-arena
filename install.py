"""Install the data-only pack and a private seat profile into a compatible Bevy client."""

import argparse
import json
import shutil
from pathlib import Path


def install(client, profile):
    if not client.is_dir():
        raise ValueError("Client root must exist")
    if profile.name not in ("player-1.json", "player-2.json"):
        raise ValueError("Select a player-N.json profile")
    credential = json.loads(profile.read_bytes())
    if set(credential) != {"token", "database", "endpoint"}:
        raise ValueError("Malformed player profile")
    pack = client / "mods" / "swg-jk2-survivor-arena"
    private = client / ".local" / "action-realms" / pack.name
    pack.mkdir(exist_ok=True, parents=True)
    private.mkdir(mode=0o700, exist_ok=True, parents=True)
    target = private / profile.name
    if target.exists() and target.read_bytes() != profile.read_bytes():
        raise FileExistsError(f"A different seat is already installed at {target}")
    if Path(__file__).resolve().parent != pack.resolve():
        shutil.copyfile(Path(__file__).with_name("action-pack.json"), pack / "action-pack.json")
    if not target.exists():
        with target.open("xb") as f:
            f.write(profile.read_bytes())
    print(f"Installed Survivor Arena for {profile.stem}. Choose it under Custom.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("client", type=Path)
    ap.add_argument("profile", type=Path)
    args = ap.parse_args()
    install(args.client, args.profile)
