# License and source provenance

The simulation worker, native combat library, action manifest, build and installation scripts, tests and documentation in this repository are distributed under GNU GPL version 2 only. The independently authored SpacetimeDB adapter in `realm/` uses the MIT License. Third-party dependencies retain their respective licenses. [GPLv2](COPYING) Â· [Adapter license](realm/LICENSE)

Activision and Raven released the Jedi Outcast source under GPLv2. The release attributes the code to Activision, 2002. The selected movement and weapon source files also credit Id Software, 1999â€“2000. Upstream revision `65ac6562d83e3080f392d75c585263b45c044f03`, Jedi Outcast multiplayer version 1.04. The accompanying COPYING is the original release's LICENSE.txt. [Release notice](https://github.com/TriForceX/JK2SourceCode/blob/65ac6562d83e3080f392d75c585263b45c044f03/jk2-1.04-src/README.txt) Â· [Upstream license](https://github.com/TriForceX/JK2SourceCode/blob/65ac6562d83e3080f392d75c585263b45c044f03/jk2-1.04-src/LICENSE.txt)

Changes made November 26, 2026, in `engine/jk2.c`:

| Upstream source | Runtime use and changes |
| --- | --- |
| [bg_pmove.c](https://github.com/TriForceX/JK2SourceCode/blob/65ac6562d83e3080f392d75c585263b45c044f03/jk2-1.04-src/CODE-mp/game/bg_pmove.c) | Extracted PM_ClipVelocity and the active PM_Accelerate branch. Passed the velocity and timestep explicitly. The worker calls these functions for player movement and arena wall contact. |
| [bg_saber.c](https://github.com/TriForceX/JK2SourceCode/blob/65ac6562d83e3080f392d75c585263b45c044f03/jk2-1.04-src/CODE-mp/game/bg_saber.c) | Adapted the grounded directional selection from PM_SaberAttackForMovement. The arena supplies rear-target detection, swing volumes, timing and animation keys. |
| [g_weapon.c](https://github.com/TriForceX/JK2SourceCode/blob/65ac6562d83e3080f392d75c585263b45c044f03/jk2-1.04-src/CODE-mp/game/g_weapon.c) | Adapted the capped Bryar charge damage computation in WP_FireBryarPistol. The arena supplies the projectile path, range, hit detection and SWG presentation. |

The published tree contains source and asset path names. Players obtain art, skeletal animation, weapons and scenery from their own SWG installation. Neither title's retail material is licensed by this repository.

## Distribution boundary

The trusted Python worker loads the GPLv2 native kernel on the server. The separately built realm authenticates callers, queues inputs and commits an authorized simulation checkpoint over HTTP. The client receives public scene data for its admitted identity. Neither the realm WASM nor the client loads the combat library. The realm's dependency lockfile pins the Apache-licensed SpacetimeDB SDK, which is excluded from the GPLv2 library build. [Apache compatibility guidance](https://www.apache.org/licenses/GPL-compatibility.html)

Preserve notices and the GPLv2 text with redistributed GPL-covered source. If distributing compiled GPL code, meet GPLv2 section 3. One route is to accompany the binary with its complete corresponding machine-readable source, including the applicable build and installation scripts. Preserve applicable MIT and dependency notices for an adapter binary distribution. The obligations of a particular combined product depend on how the programs interact and how they are distributed. [GPLv2 sections 1â€“3](https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html) Â· [GNU guidance on separate programs](https://www.gnu.org/licenses/gpl-faq.html#MereAggregation)
