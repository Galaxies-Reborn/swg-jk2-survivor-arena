"""Provision the arena or run its trusted 20 Hz simulation process. GPL-2.0-only."""

import argparse
import hashlib
import ipaddress
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from engine.arena import Kernel, step


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def endpoint(url):
    v = urlsplit(url)
    if (
        v.scheme != "http"
        or v.username
        or v.password
        or v.path
        or v.query
        or v.fragment
        or not ipaddress.ip_address(v.hostname).is_loopback
    ):
        raise ValueError("Expected HTTP loopback IP and port")
    return url


def http(url, body=b"", token=None, media="application/json", method="POST"):
    headers = {"Content-Type": media}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(url, data=body, headers=headers, method=method)
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=4) as r:
        data = r.read(512 * 1024)
    return json.loads(data) if data else None


class Client:
    def __init__(self, profile):
        db = profile["database"]
        if not db or not all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in db):
            raise ValueError("Invalid database name")
        self.base = endpoint(profile["endpoint"]) + "/v1/database/" + db
        self.token = profile["token"]

    def call(self, name, args):
        return http(self.base + "/call/" + name, json.dumps(args).encode(), self.token)

    def rows(self, name):
        (table,) = http(
            self.base + "/sql?confirmed=true",
            ("SELECT * FROM " + name).encode(),
            self.token,
            "text/plain",
        )
        fields = [c["name"]["some"] for c in table["schema"]["elements"]]
        return [dict(zip(fields, r, strict=True)) for r in table["rows"]]


def provision(url, wasm, directory):
    endpoint(url)
    data = wasm.read_bytes()
    if directory.exists():
        raise FileExistsError(f"Reuse the profiles in {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    pending = Path(tempfile.mkdtemp(dir=directory.parent, prefix=".pending-survivor-"))
    db = "survivor-" + uuid.uuid4().hex[:16]
    accounts = {}
    try:
        for role in ("operator", "worker", "player-1", "player-2"):
            account = http(url + "/v1/identity")
            accounts[role] = account
            with (pending / (role + ".json")).open("x", encoding="utf-8") as f:
                json.dump(dict(endpoint=url, database=db, token=account["token"]), f)
        op = Client(json.loads((pending / "operator.json").read_bytes()))
        receipt = http(
            op.base + "?clear=false", data, op.token, "application/wasm", "PUT"
        )
        if "Success" not in receipt:
            raise ValueError("Publication rejected")
        op.call(
            "admit",
            [
                ["0x" + accounts[n]["identity"]]
                for n in ("player-1", "player-2", "worker")
            ],
        )
        (pending / "module.sha256").write_text(
            hashlib.sha256(data).hexdigest(), encoding="ascii"
        )
        pending.rename(directory)
    except Exception as e:
        raise RuntimeError(
            f"Provisioning {db} stopped. Recovery credentials are in {pending}"
        ) from e
    print(f"Provisioned {db} with WASM {hashlib.sha256(data).hexdigest()}")
    return db


def tick(client, kernel):
    rows = client.rows("simulation_work")
    if not rows:
        return False
    job = json.loads(rows[0]["snapshot"])
    state, scene = step(json.loads(job["state"]), job["seats"], kernel)
    acks = [0, 0]
    for seat in job["seats"]:
        acks[seat["slot"] - 1] = seat["seq"]
    client.call(
        "commit_frame", [job["revision"], acks, json.dumps(state), json.dumps(scene)]
    )
    return True


def serve(profile, library, navigation):
    from engine.navigation import Navigation

    client = Client(json.loads(profile.read_bytes()))
    kernel = Kernel(library, Navigation(navigation))
    deadline = time.monotonic()
    failures = 0
    while True:
        try:
            active = tick(client, kernel)
            failures = 0
        except (HTTPError, URLError, TimeoutError) as e:
            detail = (
                e.read(1024).decode(errors="replace")
                if isinstance(e, HTTPError)
                else ""
            )
            if isinstance(e, HTTPError) and "stale or early frame" not in detail:
                if e.code < 500 or "simulation identity required" in detail:
                    raise
            failures += 1
            if failures == 1:
                print("Waiting for a fresh committed frame", file=sys.stderr)
            active = True
        deadline = max(time.monotonic(), deadline + (0.05 if active else 0.20))
        time.sleep(max(0, deadline - time.monotonic()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("provision")
    setup.add_argument("wasm", type=Path)
    setup.add_argument("--profiles", required=True, type=Path)
    setup.add_argument("--endpoint", default="http://127.0.0.1:38431")
    worker = commands.add_parser("serve")
    worker.add_argument("--profile", required=True, type=Path)
    worker.add_argument("--kernel", required=True, type=Path)
    worker.add_argument(
        "--navigation",
        type=Path,
        default=Path(__file__).resolve().parent / ".local/navigation.json",
    )
    args = parser.parse_args()
    if args.command == "provision":
        provision(args.endpoint, args.wasm, args.profiles)
    else:
        serve(args.profile, args.kernel, args.navigation)
