# Survivor Arena

A one- or two-player cooperative FPS combat mod set in an SWG Tatooine cantina. Fight waves of Tusken raiders with a lightsaber and a blaster. A red-bladed duelist leads the raiders. Watch the windup, guard as the blade arrives and attack during the opening. Clear a wave to recover health and bring downed teammates back. If the party falls, either player can start a fresh run. The server owns movement, projectile and saber hits, enemy AI, wave progression and score.

The server runs compiled combat and movement routines from Jedi Outcast 1.04. The client renders locally owned SWG assets. The mod includes the selected JK2 gameplay functions, a survivor simulation, an independent SpacetimeDB adapter and a data-only client pack. [Source map and licenses](NOTICE.md)

## Play on Windows

Use a Bevy client with action-pack schema 2 and indoor_arena, chase camera, layered_locomotion, sounds and view_swings support, a player-owned pre-CU SWG content pack, Python 3.10+, PowerShell 7, CMake with a C compiler, Rustup and SpacetimeDB standalone 2.10.0. From this repository run:

```powershell
./Start-Arena.ps1 -Standalone '<path to spacetimedb-standalone.exe>' -ClientRoot '<game client root>'
```

The launcher builds the GPL combat library and the separate realm WASM, starts a dedicated loopback host on port 38431, provisions two player identities on first use, installs the selected pack and profiles, and runs the simulation. Start the compatible client and choose **Custom â†’ Survivor Arena Â· Player 1**. A second client can choose **Player 2**. Escape leaves the arena. The host persists the match in `.local/arena`. Reuse the same state directory and port to resume. Ctrl+C ends the hosting session.

For two computers, use `-Seats 1` on the host. Give player 2 only their own `player-2.json` file privately. On their machine, run `python install.py "<client root>" "<path to player-2.json>"` and forward local port 38431 to the host's loopback port over an authenticated tunnel. Each machine prepares its own SWG content. The operator and worker profiles and host signing key stay on the server. The host reads its own walkable floors and passable doorways from `.local/navigation.json`. The launcher generates this file from the host’s SWG installation using the client checkout on first run. For a headless host, supply the generated file with `-Navigation`. Keep this retail-derived file local.

| Control | Action |
| --- | --- |
| WASD, mouse | Run and aim |
| V | Switch between the initial chase view and first person |
| Left mouse | Swing a saber or fire the blaster. Hold attack and change movement direction to steer follow-up cuts. Neutral follow-ups reverse across the body. |
| Right mouse with saber | Guard against a frontal attack. Blocking in the first 200 ms breaks the attacker’s swing. Click attack immediately for a riposte, holding W to lunge forward. |
| Right mouse with blaster | Start charging. Left mouse fires the charged bolt. |
| X, Space | Switch weapon, jump |
| R | Restart after party defeat |

## Build and verify independently

The public repository builds without the client or any retail files. The worker and the WASM communicate through an authenticated server interface. The default wave contains three raiders. Each cleared wave adds pressure up to twelve simultaneous enemies. Players share a score and deal no friendly fire.

```powershell
cmake -S engine -B build
cmake --build build --config Release
cargo build --release --locked --target wasm32-unknown-unknown --manifest-path realm/Cargo.toml --target-dir target
python verify.py --kernel build/Release/jk2_arena.dll
```

For the live regression, start a dedicated standalone host with `./Start-Realm.ps1 -Standalone '<path>'`. Then run:

```powershell
python verify.py --kernel build/Release/jk2_arena.dll --wasm target/wasm32-unknown-unknown/release/survivor_realm.wasm
```

The verifier provisions a fresh database and exercises two players, an outsider, worker restart, wave completion, team defeat, restart and lease expiry. For manual operations use `python server.py provision --help` and `python server.py serve --help`. Linux builds use the shared library produced by CMake in place of the Windows DLL.

## Integration

The client pack requests up to 14 cm of collision-limited movement preview while the next realm frame is in flight. The worker resolves player positions, contacts and damage. Directional saber attacks use the movement keys held when the attack was pressed, including buffered follow-up cuts. First-person swings advance on rendered frames between realm updates.

Weapon swings, impacts, clashes and blaster shots use WAV samples in the local SWG content pack. The standalone Windows client uses the same ABI 10 swg_juce_audio.dll that ships beside the standard client executable.

The client discovers `mods/swg-jk2-survivor-arena/action-pack.json`. Schema 2 adds first-person aim, per-style SWG appearances, multiple visible actors, short beam traces and an authoritative status line. Each view_swings entry names a clip and specifies windup and follow-through orientations in degrees (yaw, pitch, roll). The client interpolates these poses for the first-person weapon. A private seat profile supplies the endpoint and credential. `enter` opens an epoch, `input` accepts sequenced controls and `leave` closes it. The caller-specific `action_scene` supplies poses and presentation. Only the worker reads pending input and the recoverable checkpoint or submits a simulation frame. SpacetimeDB atomically fences stale commits, acknowledges consumed input and expires inactive seats independently of the worker.

To uninstall, close the mode and remove its installed presentation and seat profiles. If the mod is installed as a Git submodule, deinitialize that submodule. Host state can be retained for a later session. The private client, its Git history and game assets are outside this repository.
