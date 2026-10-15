# MONIX Mǒna production adapter

A Python 3.10+ local tool for the C# **Sorcerer of Two Covens** starter. It reads the game's existing dialogue and a production profile, validates finite dialogue branches, and produces a new portable MONIX review project. No third-party Python packages, server, account, GPU job or network request is needed.

This is a production adapter, not a gameplay runtime. Godot .NET/C# owns gameplay. Native Studio opening is a separate Mac acceptance gate. ENGYN rendering and MONIX Live are not involved.

## Commands

Run these commands from this repository's root. The game and its production profile come from the separate production kit; replace `/path/to/kit/game` and `/path/to/review-next` with your local paths.

```bash
python3 tools/mona-production/mona.py check --game /path/to/kit/game
python3 tools/mona-production/mona.py prepare --game /path/to/kit/game --out /path/to/review-next
python3 tools/mona-production/mona.py verify --review /path/to/review-next --game /path/to/kit/game
python3 tools/mona-production/mona.py doctor --game /path/to/kit/game --godot /absolute/path/to/Godot
python3 tools/mona-production/mona.py check-game --game /path/to/kit/game --godot /absolute/path/to/Godot
```

`prepare` requires a new output directory **outside** the source game. It never overwrites a review, changes game code or writes to an existing MONIX project. Open `review-next/review.html` directly in a browser. On a Mac, try opening the `review-next` folder in MONIX Studio; text and optional media assets use the current native asset index shape.

`verify --game` reconstructs every output byte from the input files and the current adapter. Without `--game`, it checks snapshot integrity only. This is a checksum check, not a digital signature. Editing the snapshot, including Studio saving its index, invalidates its original checksum manifest; create a fresh snapshot for the next review.

`doctor` only probes dependency versions. Exit 2 means the toolchain gate is blocked. The inherited starter pins `Godot.NET.Sdk/4.7.0` and `net8.0`; align the SDK pin with the installed .NET editor before running the gate. Nothing installs or upgrades an editor automatically.

`check-game` runs `dotnet build`, Godot `--headless --import`, and a 60-frame startup check in order, with finite timeouts. The first .NET restore can access NuGet; use `--no-restore` when dependencies are already restored. Compiler errors, detected engine errors and timeouts fail the gate. Successful startup does **not** certify completing the rescue, actual audio playback, FPS or export packaging. These remain manual acceptance checks.

## What is checked

- Stable cue IDs, nonempty speech and choices, valid links, known effect names, no cycles or unreachable beats.
- Every path through each configured rescue conversation applies its required quest effect. These are dialogue-data proofs; C# state transitions and spatial interactions are not simulated.
- Every cue belongs to one shot group. Shot groups are production proposals, not additions to the World Bible.
- The profile pins the reviewed `GameDirector.cs` hash. If it changes, review entrypoints/effects against the new game code before updating the hash.
- Input paths remain inside the source game; symlinks, traversal, ambiguous voice takes and empty assets are rejected.
- Optional files are copied and SHA-256 tracked. Presence is always `present-unreviewed`, never automatic creative approval or codec certification.
- Duplicate JSON keys and non-finite numbers are rejected. HTML is escaped; CSV cells that resemble spreadsheet formulas are quoted defensively. Source dialogue remains unchanged.

## Production profile and voice workflow

The default profile is `production/two-covens.production.json` relative to the `--game` directory; use `--profile relative/path.json` for another location inside the game. It supplies the project UUID, editorial snapshot timestamp, dialogue path, runtime contract hash, dialogue entrypoints/effects, shot-to-cue mapping and an asset queue.

Keep the project UUID stable across snapshots. Update `snapshot_at_utc` intentionally for a new editorial revision; timestamps do not come from the wall clock, so identical inputs yield identical bytes. Canon remains `PROPOSED_NOT_APPROVED`. Canon acceptance requires a separate review against authoritative lore.

To review a voice take, put one Ogg **or** WAV at `game/Audio/Voice/<cue_id>.ogg` or `.wav`. Keep raw masters elsewhere. Rebuild to a new output folder. The HTML review can play the copied take. The supplied C# game has no voice playback implementation; this adapter does not add one.

For an art/audio asset, replace its profile `path: null` with the relative path of the selected review file. A null path means the asset is still needed. Each file is limited to 64 MiB for review packaging. Avoid video masters and large working scenes. Only listed media is copied; arbitrary source directories are never swept into the output.

The neutral handoff keeps the existing `mona.production-handoff.v1` identifier for metadata. The wrapper is a separate native `monix/project/2.0` project. Its scene/runtime references are null because it does not contain an ENGYN game. `OTHER` maps the neutral handoff without changing MONIX's schema.

## Repository integration

The entire tool lives under `tools/mona-production/` in `kadiverstudio/MONIX-REPO`. The game, profile, raw media, generated review project and verification logs stay outside the repository. Tests use synthetic data, not Mǒna lore.

Contract baseline: `kadiverstudio/DUMONIX-REPO` commit `24f0a43addaefee0662d212d334f3d188251af1c`, inspected 2026-10-14. The adapter follows `monix/specs/platform-foundation/local-project-v2.schema.json`, `MonixProject.swift`, `ProjectService.swift`, `MonixAsset.swift` and `MonixAssetIndex.swift` from that commit. Native open/decode still needs Mac validation.

```bash
python3 -m unittest discover -s tools/mona-production/tests -v
```

Official command references:
- <https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html>
- <https://docs.godotengine.org/en/stable/tutorials/scripting/c_sharp/c_sharp_basics.html>

Existing MONIX build systems and DUMO boundaries remain in effect. This repository does not yet contain the full engine or native applications. This tool has no dependencies on DUMONIX-REPO PRs #56–#58 and makes no changes to those PRs, Forge runtime data or native Studio code.
