#!/usr/bin/env python3
"""Offline dialogue-to-MONIX review adapter. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import html
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

VERSION = "0.1.0"
PROFILE_SCHEMA = "mona.production-profile.v1"
BUILD_SCHEMA = "mona.production-build.v1"
ID = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_FILE = 64 * 1024 * 1024


class Invalid(ValueError):
    """An input cannot be safely or meaningfully converted."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj


def decode(data):
    def invalid_number(value):
        raise Invalid(f"Non-finite JSON value: {value}")
    return json.loads(data, object_pairs_hook=object_pairs, parse_constant=invalid_number)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local_path(root, relative):
    require(isinstance(relative, str) and relative, "A nonempty relative path is required")
    require(not any(ord(c) < 32 for c in relative), "Control character in path")
    require(not relative.startswith("/") and "\\" not in relative and ":" not in relative,
            f"Not a portable relative path: {relative}")
    require(all(p not in ("", ".", "..") for p in relative.split("/")), f"Unsafe path: {relative}")
    root = Path(root).resolve()
    path = root
    for part in relative.split("/"):
        path = path / part
        require(not path.is_symlink(), f"Symlink is not an input asset: {relative}")
    require(path.resolve().is_relative_to(root), f"Path escapes source project: {relative}")
    return path


def read(root, relative):
    path = local_path(root, relative)
    require(path.is_file(), f"Missing file: {relative}")
    require(path.stat().st_size <= MAX_FILE, f"File exceeds 64 MiB review limit: {relative}")
    return path.read_bytes()


def text(value, field):
    require(isinstance(value, str) and value.strip(), f"Missing text: {field}")
    return value


def identifier(value, field):
    require(isinstance(value, str) and ID.fullmatch(value), f"Invalid ID: {field}")
    return value


def validate_dialogue(beats, profile):
    require(isinstance(beats, dict) and 0 < len(beats) <= 2000, "Expected 1–2000 dialogue beats")
    allowed = profile["allowed_effects"]
    require(isinstance(allowed, list) and all(isinstance(e, str) for e in allowed), "Invalid allowed_effects")
    for cue, beat in beats.items():
        identifier(cue, cue)
        require(isinstance(beat, dict), f"Invalid beat: {cue}")
        text(beat.get("speaker"), f"{cue}.speaker")
        text(beat.get("text"), f"{cue}.text")
        require(beat.get("effect", "") in allowed, f"Unknown gameplay effect in {cue}")
        choices = beat.get("choices")
        require(isinstance(choices, list) and 1 <= len(choices) <= 4, f"{cue} needs 1–4 choices")
        for choice in choices:
            require(isinstance(choice, dict), f"Invalid choice in {cue}")
            text(choice.get("text"), f"{cue}.choice.text")
            nxt = choice.get("next")
            require(isinstance(nxt, str) and (nxt == "" or nxt in beats), f"Dangling choice in {cue}: {nxt}")

    # Iterative DFS avoids recursion limits on long user-authored scenes.
    visited, active, order = set(), set(), []
    for start in beats:
        stack = [(start, False)]
        while stack:
            cue, done = stack.pop()
            if done:
                active.remove(cue)
                visited.add(cue)
                order.append(cue)
                continue
            if cue in visited:
                continue
            require(cue not in active, f"Dialogue cycle at {cue}; this adapter requires finite conversations")
            active.add(cue)
            stack.append((cue, True))
            stack.extend((c["next"], False) for c in beats[cue]["choices"] if c["next"])

    entries = profile["entrypoints"]
    require(isinstance(entries, list) and entries and all(c in beats for c in entries), "Unknown dialogue entrypoint")
    reachable, pending = set(), list(entries)
    while pending:
        cue = pending.pop()
        if cue in reachable:
            continue
        reachable.add(cue)
        pending.extend(c["next"] for c in beats[cue]["choices"] if c["next"])
    require(reachable == set(beats), f"Beats unreachable from configured entrypoints: {sorted(set(beats)-reachable)}")

    # Proves the authored conversation segments preserve their required effects.
    # It does not simulate C# state, physics, interaction range, or UI input.
    routes = []
    segments = profile["rescue_segments"]
    require(isinstance(segments, list) and segments, "At least one rescue segment is required")
    for segment in segments:
        root, effect = segment["entry"], segment["required_effect"]
        require(root in beats and effect in allowed and effect, "Invalid rescue segment")
        counts, guarantees = {}, {}
        for cue in order:
            successors = [c["next"] for c in beats[cue]["choices"]]
            counts[cue] = sum(counts[n] if n else 1 for n in successors)
            guarantees[cue] = beats[cue].get("effect", "") == effect or all(
                bool(n) and guarantees[n] for n in successors)
        require(guarantees[root], f"A branch from {root} can end without {effect}")
        routes.append({"entry": root, "required_effect": effect, "terminal_paths": counts[root]})
    return routes


def inspect(game, profile_relative):
    game = Path(game).resolve()
    profile_bytes = read(game, profile_relative)
    profile = decode(profile_bytes)
    require(isinstance(profile, dict) and profile.get("schema") == PROFILE_SCHEMA, "Unsupported production profile")
    require(profile["canon_status"] == "PROPOSED_NOT_APPROVED", "Canon approval is outside this adapter")
    require(isinstance(profile["project_id"], str) and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", profile["project_id"]),
        "project_id must be a lowercase UUID with a supported version and variant")
    text(profile["title"], "title")
    require(len(profile["title"]) <= 480, "Title is too long for the native project display name")
    identifier(profile["slug"], "slug")
    stamp = profile["snapshot_at_utc"]
    require(isinstance(stamp, str) and stamp.endswith("Z"), "Snapshot timestamp must end in Z")
    datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    require(isinstance(profile["confirmed_direction"], list), "confirmed_direction must be a list")
    for item in profile["confirmed_direction"]:
        text(item, "confirmed_direction")
    inputs = {profile_relative: profile_bytes}
    source = profile["dialogue_path"]
    inputs[source] = read(game, source)
    beats = decode(inputs[source])
    routes = validate_dialogue(beats, profile)
    shot_for, shots = {}, profile["shots"]
    require(isinstance(shots, list) and shots, "Missing shot plan")
    shot_ids = set()
    for shot in shots:
        sid = identifier(shot["id"], "shot.id")
        require(sid not in shot_ids, f"Duplicate shot: {sid}")
        shot_ids.add(sid)
        text(shot["title"], "shot.title")
        text(shot["purpose"], "shot.purpose")
        require(isinstance(shot["cues"], list) and shot["cues"], f"Empty shot: {sid}")
        for cue in shot["cues"]:
            require(cue in beats and cue not in shot_for, f"Unknown or repeated shot cue: {cue}")
            shot_for[cue] = sid
    require(set(shot_for) == set(beats), "Every dialogue cue needs exactly one shot assignment")

    records, media = [], {}
    voice_dir = profile["voice_directory"]
    local_path(game, voice_dir)
    for cue, beat in beats.items():
        candidates = [f"{voice_dir}/{cue}{ext}" for ext in (".ogg", ".wav")]
        found = [p for p in candidates if local_path(game, p).exists()]
        require(len(found) <= 1, f"Choose one review take, Ogg or WAV, for {cue}")
        record = {"id": f"vo:{cue}", "kind": "voice-cue", "source_id": cue,
                  "shot": shot_for[cue], "speaker": beat["speaker"], "text": beat["text"],
                  "state": "needs-recording", "relative_path": None, "sha256": None}
        if found:
            src = found[0]
            data = read(game, src)
            require(bool(data), f"Empty voice take: {src}")
            inputs[src] = data
            target = f".monixproject/assets/voice/{cue}{Path(src).suffix}"
            media[target] = data
            record.update(state="present-unreviewed", relative_path=target, sha256=digest(data))
        records.append(record)
    asset_ids = set()
    for asset in profile["assets"]:
        aid = identifier(asset["id"], "asset.id")
        require(aid not in asset_ids, f"Duplicate asset: {aid}")
        asset_ids.add(aid)
        text(asset["name"], "asset.name")
        text(asset["brief"], "asset.brief")
        require(asset["kind"] in ("image", "audio", "model", "material", "text"), "Unknown asset kind")
        record = {"id": f"asset:{aid}", "kind": asset["kind"], "name": asset["name"],
                  "brief": asset["brief"], "state": "needed", "relative_path": None, "sha256": None}
        if asset["path"] is not None:
            src = asset["path"]
            data = read(game, src)
            require(bool(data), f"Empty production asset: {src}")
            ext = Path(src).suffix.lower()
            require(re.fullmatch(r"\.[a-z0-9]{1,10}", ext), f"Unsupported asset extension: {src}")
            target = f".monixproject/assets/art/{aid}{ext}"
            inputs[src], media[target] = data, data
            record.update(state="present-unreviewed", relative_path=target, sha256=digest(data))
        records.append(record)
    for shot in shots:
        records.append({"id": f"shot:{shot['id']}", "kind": "shot", "shot": shot["id"],
                        "name": shot["title"], "purpose": shot["purpose"], "state": "blocking-needed"})
    contract = profile["runtime_contract"]
    for key in ("path", "sha256"):
        text(contract[key], f"runtime_contract.{key}")
    require(SHA.fullmatch(contract["sha256"]), "Invalid runtime contract digest")
    runtime_bytes = read(game, contract["path"])
    inputs[contract["path"]] = runtime_bytes
    require(digest(runtime_bytes) == contract["sha256"],
            "GameDirector changed: review entrypoints/effects and update the runtime contract hash")
    inputs["project.godot"] = read(game, "project.godot")
    csprojs = list(game.glob("*.csproj"))
    require(len(csprojs) == 1, "Expected one root C# .csproj")
    inputs[csprojs[0].name] = read(game, csprojs[0].name)
    summary = {"dialogue_cues": len(beats), "shots": len(shots), "rescue_segments": routes,
               "voice_takes_present": sum(r["state"] == "present-unreviewed" for r in records if r["kind"] == "voice-cue"),
               "art_assets_present": sum(r["state"] == "present-unreviewed" for r in records if r["id"].startswith("asset:")),
               "art_assets_total": len(asset_ids),
               "art_assets_needed": sum(r["state"] == "needed" for r in records if r["id"].startswith("asset:")),
               "canon_status": profile["canon_status"],
               "dialogue_graph": "passed", "game_runtime": "not-tested", "studio_open": "not-tested"}
    return profile, beats, records, inputs, media, summary


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    # Spreadsheet formula protection changes only exports, never source dialogue.
    writer.writerows([["'" + x if isinstance(x, str) and x.lstrip().startswith(("=", "+", "-", "@")) else x
                       for x in row] for row in rows])
    return stream.getvalue().encode()


def render_review(profile, beats, records, summary):
    esc = lambda v: html.escape(str(v), quote=True)
    voices = {r["source_id"]: r for r in records if r["kind"] == "voice-cue"}
    sections = []
    for shot in profile["shots"]:
        rows = []
        for cue in shot["cues"]:
            beat, voice = beats[cue], voices[cue]
            audio = (f'<audio controls preload="none" src="{esc(voice["relative_path"])}"></audio>'
                     if voice["relative_path"] else '<span class="pending">Voice needed</span>')
            links = " · ".join(f'{esc(c["text"])} → ' + (f'<a href="#{esc(c["next"])}">{esc(c["next"])}</a>'
                                if c["next"] else "Return to world") for c in beat["choices"])
            rows.append(f'<article id="{esc(cue)}"><div class="cue"><code>{esc(cue)}</code><span>{esc(beat["speaker"])}</span></div>'
                        f'<p>{esc(beat["text"])}</p><div class="branches">{links}</div><div class="take">{audio}</div></article>')
        sections.append(f'<section id="{esc(shot["id"])}"><div class="eyebrow">{esc(shot["id"])}</div>'
                        f'<h2>{esc(shot["title"])}</h2><p class="muted">{esc(shot["purpose"])}</p>{"".join(rows)}</section>')
    assets = "".join(f'<tr><td>{esc(r["name"])}</td><td>{esc(r["brief"])}</td><td>{esc(r["state"])}</td></tr>'
                     for r in records if r["id"].startswith("asset:"))
    nav = "".join(f'<a href="#{esc(s["id"])}">{esc(s["title"])}</a>' for s in profile["shots"])
    direction = "".join(f'<li>{esc(d)}</li>' for d in profile["confirmed_direction"])
    css = Path(__file__).with_name("review.css").read_text(encoding="utf-8")
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; media-src 'self' file:; img-src 'self' file: data:; base-uri 'none'">
<title>{esc(profile['title'])} — MONIX production review</title><style>{css}</style>
<body><aside><div class="brand">MONIX <span>PRODUCTION</span></div><div class="eyebrow">WORLD OF MǑNA</div>{nav}<a href="#assets">Art &amp; sound</a><a href="#gates">Playtest gates</a></aside>
<main><header><div class="eyebrow">ACT I · RESCUE · LOCAL PRODUCTION REVIEW</div><h1>{esc(profile['title'])}</h1>
<p class="lead">An undercover sorcerer. A divided loyalty. One person to bring home.</p>
<div class="badge">Working draft · lore approval pending</div><ul>{direction}</ul></header>
<div class="metrics"><div><b>{summary['dialogue_cues']}</b>Dialogue cues</div><div><b>{summary['shots']}</b>Shot groups</div><div><b>{summary['voice_takes_present']} / {summary['dialogue_cues']}</b>Review voice takes</div><div><b>{summary['art_assets_present']} / {summary['art_assets_total']}</b>Art &amp; sound files</div></div>
<p class="notice">Dialogue links and rescue effects passed data validation. Godot gameplay and native Studio opening still require a local check. Present files require human review; the current game has no voice playback.</p>
{''.join(sections)}<section id="assets"><div class="eyebrow">PRODUCTION QUEUE</div><h2>Art &amp; sound</h2><p class="muted">Use the existing World Bible for visual decisions. These are asset briefs, not canon additions.</p><table><thead><tr><th>Asset</th><th>Brief</th><th>Status</th></tr></thead><tbody>{assets}</tbody></table></section>
<section id="gates"><div class="eyebrow">NEXT PLAYTEST</div><h2>Get the rescue running on the ROG</h2><ol><li>Run <code>check-game</code> with your Godot .NET executable.</li><li>Play the intro, approach the girl, and promise rescue.</li><li>Try both warden distractions; escort the girl to the gate.</li><li>Finish the rescue, hear the recall dialogue as subtitles, and press R to restart.</li><li>On a Mac, open this folder with MONIX Studio and inspect the text assets.</li></ol><p class="muted">This review folder does not contain a Godot executable or an ENGYN renderer.</p></section>
<footer>MONIX production adapter {VERSION} · snapshot {esc(profile['snapshot_at_utc'])} · offline</footer></main></body></html>'''.encode()


def compile_project(game, profile_relative):
    profile, beats, records, inputs, media, summary = inspect(game, profile_relative)
    stamp = profile["snapshot_at_utc"]
    outputs = dict(media)
    asset_root = ".monixproject/assets/"
    dialogue_target = asset_root + "dialogue_act01.json"
    outputs[dialogue_target] = inputs[profile["dialogue_path"]]
    outputs["Production/profile.json"] = encoded(profile)
    handoff = {"schema": "mona.production-handoff.v1", "project": profile["slug"],
               "canonical_text": dialogue_target, "canon_status": profile["canon_status"],
               "offline_first": True, "monix_project_format": False, "records": records}
    outputs["Production/mona.production-handoff.v1.json"] = encoded(handoff)
    outputs[asset_root + "voice_cues.csv"] = csv_bytes(
        [["cue_id", "speaker", "text", "shot", "status", "review_file"]] +
        [[r["source_id"], r["speaker"], r["text"], r["shot"], r["state"], r["relative_path"] or ""]
         for r in records if r["kind"] == "voice-cue"])
    outputs[asset_root + "shotlist.csv"] = csv_bytes([["shot_id", "title", "purpose", "cue_ids"]] +
        [[s["id"], s["title"], s["purpose"], ";".join(s["cues"])] for s in profile["shots"]])
    outputs[asset_root + "asset_queue.csv"] = csv_bytes([["asset_id", "name", "kind", "brief", "state"]] +
        [[r["id"], r["name"], r["kind"], r["brief"], r["state"]] for r in records if r["id"].startswith("asset:")])
    outputs[asset_root + "CANON-AND-READINESS.md"] = (
        "# " + profile["title"] + "\n\nCanon status: PROPOSED_NOT_APPROVED.\n\n"
        + "\n".join("- " + d for d in profile["confirmed_direction"])
        + "\n\nShot groupings and visual briefs are production proposals. Dialogue remains the original starter draft.\n"
        + "The World Bible was not supplied for canon verification.\n\n"
        + "C# gameplay, voice playback, native Studio opening and ROG performance are not verified by this adapter.\n"
        + "The review project contains copied text and optional media; it does not execute the game.\n").encode()
    outputs["review.html"] = render_review(profile, beats, records, summary)
    outputs["Production/readiness.json"] = encoded(summary)
    index = []
    kinds = {r["relative_path"]: ("audio" if r["kind"] == "voice-cue" else r["kind"])
             for r in records if r.get("relative_path")}
    for path in sorted(outputs):
        if not path.startswith(asset_root):
            continue
        index.append({"id": str(uuid.uuid5(uuid.UUID(profile["project_id"]), path)),
                      "name": Path(path).stem, "type": kinds.get(path, "text"),
                      "sourceJobId": None, "relativePath": path, "createdAt": stamp})
    outputs[asset_root + "monix.assets.json"] = encoded({"schema": "monix/assets-index/1.0", "updatedAt": stamp, "assets": index})
    outputs[".monixproject/project.v2.monix.json"] = encoded({
        "schema": "monix/project/2.0", "identity": {"localProjectId": profile["project_id"], "identityAuthority": "LOCAL"},
        "metadata": {"displayName": profile["title"] + " — Production review", "projectKind": "game",
                     "createdAtUtc": stamp, "updatedAtUtc": stamp},
        "compatibility": {"formatVersion": "2.0", "minimumReaderVersion": "0.1.0",
                          "createdWith": {"semanticVersion": VERSION, "buildVersion": "mona-production-adapter"},
                          "edition": {"createdWithEditionYear": None, "minimumEditionYear": None, "requiredFeatureIds": []}},
        "references": {"assetsRoot": asset_root.rstrip("/"), "assetsIndex": asset_root + "monix.assets.json",
                       "scenesIndex": None, "runtimeManifest": None, "renderOutputsIndex": None, "settingsDocument": None,
                       "mappingReferences": [{"kind": "OTHER", "path": "Production/mona.production-handoff.v1.json", "contractVersion": "1.0"}]},
        "capabilityRequirements": []})
    tool_inputs = {p: digest(Path(__file__).with_name(p).read_bytes()) for p in ("mona.py", "review.css")}
    outputs["build-manifest.json"] = encoded({"schema": BUILD_SCHEMA, "tool_version": VERSION,
        "tool_inputs": tool_inputs, "inputs": {p: digest(data) for p, data in sorted(inputs.items())},
        "outputs": {p: digest(data) for p, data in sorted(outputs.items())},
        "native_studio_open_verified": False, "godot_runtime_verified": False})
    return outputs, summary


def prepare(game, profile, output):
    outputs, summary = compile_project(game, profile)
    dest = Path(output).absolute()
    require(not dest.resolve().is_relative_to(Path(game).resolve()), "Review output must be outside the game folder")
    require(not dest.exists() and not dest.is_symlink(), "Output already exists; choose a new snapshot folder")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()  # Exclusive creation, including collision checks before any writes.
    try:
        for relative, data in outputs.items():
            path = local_path(dest, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
    except BaseException:
        shutil.rmtree(dest)
        raise
    return summary


def verify(review, game=None, profile=None):
    root = Path(review).resolve()
    manifest = decode(read(root, "build-manifest.json"))
    require(manifest.get("schema") == BUILD_SCHEMA, "Unsupported build manifest")
    expected = manifest["outputs"]
    require(isinstance(expected, dict) and expected, "Empty build manifest")
    require(".monixproject/project.v2.monix.json" in expected and "review.html" in expected, "Incomplete review manifest")
    actual = set()
    for path in root.rglob("*"):
        require(not path.is_symlink(), "Symlink in review output")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    require(actual == set(expected) | {"build-manifest.json"}, "Missing or extra review files")
    for relative, sha in expected.items():
        require(isinstance(sha, str) and SHA.fullmatch(sha), "Invalid output hash")
        require(digest(read(root, relative)) == sha, f"Output changed: {relative}")
    if game is not None:
        rebuilt, _ = compile_project(game, profile)
        require(set(rebuilt) == actual, "Source rebuild file set differs")
        for relative, data in rebuilt.items():
            require(read(root, relative) == data, f"Source rebuild differs: {relative}")
    return {"integrity": "passed", "source_rebuild": "passed" if game is not None else "not-requested",
            "files": len(actual), "game_runtime": "not-tested", "studio_open": "not-tested"}


def probe(command, timeout=15):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {"command": command, "exit_code": result.returncode,
                "output": (result.stdout + result.stderr).strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "exit_code": None, "output": str(exc)}


def doctor(game, godot=None):
    root = Path(game).resolve()
    projects = list(root.glob("*.csproj"))
    require(len(projects) == 1, "Expected one root C# .csproj")
    project = ET.fromstring(read(root, projects[0].name))
    sdk = project.attrib.get("Sdk", "")
    require(sdk.startswith("Godot.NET.Sdk/"), "Expected Godot.NET.Sdk pin")
    target = project.findtext(".//TargetFramework", default="unknown")
    godot = godot or shutil.which("godot") or shutil.which("godot4")
    dotnet = shutil.which("dotnet")
    gv = probe([str(godot), "--version"]) if godot else {"exit_code": None, "output": "Godot not found"}
    dv = probe([dotnet, "--list-sdks"]) if dotnet else {"exit_code": None, "output": ".NET SDK not found"}
    reasons = []
    if gv["exit_code"] != 0 or "mono" not in gv["output"].lower():
        reasons.append("Select the Godot .NET editor with --godot /absolute/path/to/Godot")
    if dv["exit_code"] != 0 or not dv["output"].strip():
        reasons.append("Install a .NET SDK compatible with the project target")
    pin = sdk.split("/", 1)[1]
    match = re.search(r"\b(\d+\.\d+)(?:\.(\d+))?", gv["output"])
    if match and pin != match[1] + "." + (match[2] or "0"):
        reasons.append(f"Godot editor and project SDK differ: project requires {pin}; reconcile before building")
    return {"status": "blocked" if reasons else "dependencies-detected", "godot": gv, "dotnet": dv,
            "project_sdk": sdk, "target_framework": target, "remaining": reasons,
            "game_runtime": "not-tested", "csproj": projects[0].name}


def check_game(game, godot=None, no_restore=False):
    result = doctor(game, godot)
    result["gameplay_rescue"] = "manual-check-required"
    if result["status"] == "blocked":
        return result, 2
    root = Path(game).resolve()
    dotnet = result["dotnet"]["command"][0]
    engine = result["godot"]["command"][0]
    build = [dotnet, "build", str(root / result["csproj"])]
    if no_restore:
        build.append("--no-restore")
    commands = [build, [engine, "--headless", "--path", str(root), "--import"],
                [engine, "--headless", "--path", str(root), "--quit-after", "60"]]
    result["checks"] = []
    for command in commands:
        check = probe(command, timeout=180)
        result["checks"].append(check)
        if check["exit_code"] != 0 or re.search(r"(^|\n)(?:SCRIPT )?ERROR:|Failed to load script", check["output"]):
            result["status"] = "failed"
            return result, 1
    result.update(status="build-and-startup-passed", game_runtime="startup-only")
    return result, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "prepare", "doctor", "check-game"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--game", type=Path, required=True)
        if name in ("check", "prepare"):
            cmd.add_argument("--profile", default="production/two-covens.production.json")
        if name == "prepare":
            cmd.add_argument("--out", type=Path, required=True)
        if name in ("doctor", "check-game"):
            cmd.add_argument("--godot")
        if name == "check-game":
            cmd.add_argument("--no-restore", action="store_true")
    cmd = sub.add_parser("verify")
    cmd.add_argument("--review", type=Path, required=True)
    cmd.add_argument("--game", type=Path)
    cmd.add_argument("--profile", default="production/two-covens.production.json")
    args = parser.parse_args(argv)
    try:
        code = 0
        if args.command == "check":
            result = inspect(args.game, args.profile)[-1]
        elif args.command == "prepare":
            result = prepare(args.game, args.profile, args.out)
        elif args.command == "verify":
            result = verify(args.review, args.game, args.profile)
        elif args.command == "doctor":
            result = doctor(args.game, args.godot)
            code = 2 if result["status"] == "blocked" else 0
        else:
            result, code = check_game(args.game, args.godot, args.no_restore)
        print(encoded(result).decode(), end="")
        return code
    except (Invalid, KeyError, TypeError, ValueError, OSError, ET.ParseError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
