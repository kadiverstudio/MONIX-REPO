#!/usr/bin/env python3
"""Game-side cue contract checker for the Two Covens rescue dialogue.

The game repository owns ``data/dialogue/rescue_cues.json`` in its own shape (a
cue list with ``next``/``choices``, ``setFlags`` and ``advanceStage``), while
``mona.py`` consumes a beat dictionary with a single ``effect`` per beat. This
tool bridges the two without touching the game repository:

* ``check``     validates the game file against the cue contract committed next
                to this tool, so writers, voice takes and the game cannot drift
                apart unnoticed. The contract stores structure, vocabularies and
                text hashes; dialogue text itself stays in the game repository,
                except for explicitly pinned verbatim lines.
* ``snapshot``  regenerates the contract from the game file after a deliberate
                change. Verbatim pins are carried over and re-enforced.
* ``emit-beats`` converts the game file into the beat dictionary ``mona.py``
                expects, for assembling a production-kit snapshot.

Standard library only. Reads the game repository, never writes into it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys


def _load_mona():
    """Load the sibling adapter module by path, so this tool works both as a CLI
    and when imported from the test loader (strict JSON decoding, hashing and
    the path jail come from mona.py)."""
    spec = importlib.util.spec_from_file_location("mona", Path(__file__).with_name("mona.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mona = _load_mona()

CONTRACT_SCHEMA = "mona.cue-contract.v1"
DEFAULT_CONTRACT = Path(__file__).with_name("two-covens.cues.json")
CUE_FIELDS = {"id", "sequence", "speaker", "text", "next", "choices", "setFlags", "advanceStage"}
CHOICE_FIELDS = {"text", "next", "setFlags", "choosesTactic"}


def sha(value):
    return mona.digest(value.encode("utf-8"))


def string_list(value, field):
    mona.require(isinstance(value, list) and all(isinstance(x, str) and x for x in value),
                 f"{field} must be a list of nonempty strings")
    return value


def optional_link(value, field):
    mona.require(value is None or (isinstance(value, str) and value),
                 f"{field} must be a nonempty string when present")
    return value


def parse_game_script(game, dialogue_relative):
    """Strict parse of the game-native dialogue file into a normalized cue list.

    Raises mona.Invalid for anything that is not even shaped like the game's
    DialogueScript contract; content drift is reported later as failures.
    """
    doc = mona.decode(mona.read(game, dialogue_relative))
    mona.require(isinstance(doc, dict) and set(doc) == {"scriptId", "cues"},
                 "Expected a dialogue document with exactly scriptId and cues")
    script_id = mona.text(doc["scriptId"], "scriptId")
    raw = doc["cues"]
    mona.require(isinstance(raw, list) and 0 < len(raw) <= 2000, "Expected 1-2000 cues")
    ordered, seen = [], set()
    for index, cue in enumerate(raw):
        label = f"cues[{index}]"
        mona.require(isinstance(cue, dict), f"{label} is not an object")
        unknown = set(cue) - CUE_FIELDS
        mona.require(not unknown, f"{label} has unknown fields: {sorted(unknown)}")
        cid = mona.identifier(cue.get("id"), f"{label}.id")
        mona.require(cid not in seen, f"Duplicate cue id: {cid}")
        seen.add(cid)
        choices = None
        if cue.get("choices") is not None:
            mona.require(cue.get("next") is None, f"{cid} has both next and choices")
            raw_choices = cue["choices"]
            mona.require(isinstance(raw_choices, list) and 1 <= len(raw_choices) <= 3,
                         f"{cid} needs 1-3 choices")
            choices = []
            for j, choice in enumerate(raw_choices):
                clabel = f"{cid}.choices[{j}]"
                mona.require(isinstance(choice, dict), f"{clabel} is not an object")
                unknown = set(choice) - CHOICE_FIELDS
                mona.require(not unknown, f"{clabel} has unknown fields: {sorted(unknown)}")
                choices.append({
                    "text": mona.text(choice.get("text"), f"{clabel}.text"),
                    "next": optional_link(choice.get("next"), f"{clabel}.next"),
                    "set_flags": string_list(choice.get("setFlags", []), f"{clabel}.setFlags"),
                    "chooses_tactic": optional_link(choice.get("choosesTactic"), f"{clabel}.choosesTactic"),
                })
        ordered.append({
            "id": cid,
            "sequence": mona.text(cue.get("sequence"), f"{cid}.sequence"),
            "speaker": mona.text(cue.get("speaker"), f"{cid}.speaker"),
            "text": mona.text(cue.get("text"), f"{cid}.text"),
            "next": optional_link(cue.get("next"), f"{cid}.next"),
            "set_flags": string_list(cue.get("setFlags", []), f"{cid}.setFlags"),
            "advance_stage": optional_link(cue.get("advanceStage"), f"{cid}.advanceStage"),
            "choices": choices,
        })
    return script_id, ordered


def links(cue):
    if cue["next"] is not None:
        yield cue["next"], f"{cue['id']}.next"
    for j, choice in enumerate(cue["choices"] or []):
        if choice["next"] is not None:
            yield choice["next"], f"{cue['id']}.choices[{j}].next"


def entry_cues(ordered):
    """Cues no other cue links to: the conversation starts, in authored order."""
    targets = {target for cue in ordered for target, _ in links(cue)}
    return [cue["id"] for cue in ordered if cue["id"] not in targets]


def graph_failures(ordered, entrypoints):
    failures = []
    by_id = {cue["id"]: cue for cue in ordered}
    for cue in ordered:
        for target, where in links(cue):
            if target not in by_id:
                failures.append(f"{where}: link to unknown cue '{target}'")
    for entry in entrypoints:
        if entry not in by_id:
            failures.append(f"Entry cue missing from the game: {entry}")
    reachable, pending = set(), [e for e in entrypoints if e in by_id]
    while pending:
        cid = pending.pop()
        if cid in reachable:
            continue
        reachable.add(cid)
        pending.extend(t for t, _ in links(by_id[cid]) if t in by_id)
    for cue in ordered:
        if cue["id"] not in reachable:
            failures.append(f"Cue unreachable from the entry cues: {cue['id']}")
    # mona.py only accepts finite conversations, so a cycle is kit-side drift.
    visited, active = set(), set()
    for start in by_id:
        stack = [(start, False)]
        while stack:
            cid, done = stack.pop()
            if done:
                active.discard(cid)
                visited.add(cid)
                continue
            if cid in visited:
                continue
            if cid in active:
                failures.append(f"Dialogue cycle at {cid}; the production adapter requires finite conversations")
                return failures
            active.add(cid)
            stack.append((cid, True))
            stack.extend((t, False) for t, _ in links(by_id[cid]) if t in by_id)
    return failures


def load_contract(path):
    contract = mona.decode(Path(path).read_bytes())
    mona.require(isinstance(contract, dict) and contract.get("schema") == CONTRACT_SCHEMA,
                 "Unsupported cue contract")
    return contract


def contract_failures(contract, script_id, ordered):
    failures = []
    if script_id != contract["script_id"]:
        failures.append(f"scriptId: contract {contract['script_id']!r}, game {script_id!r}")
    ids = [cue["id"] for cue in ordered]
    expected = [cue["id"] for cue in contract["cues"]]
    missing = sorted(set(expected) - set(ids))
    extra = sorted(set(ids) - set(expected))
    if missing:
        failures.append(f"Cues missing from the game: {missing}")
    if extra:
        failures.append(f"Cues not in the contract: {extra}")
    if not missing and not extra and ids != expected:
        failures.append("Cue order differs from the contract")
    if sorted(entry_cues(ordered)) != sorted(contract["entrypoints"]):
        failures.append(f"Entry cues: contract {contract['entrypoints']}, game {entry_cues(ordered)}")

    expected_by_id = {cue["id"]: cue for cue in contract["cues"]}
    for cue in ordered:
        exp = expected_by_id.get(cue["id"])
        if exp is None:
            continue
        cid = cue["id"]
        for field in ("sequence", "speaker", "next", "advance_stage", "set_flags"):
            if cue[field] != exp[field]:
                failures.append(f"{cid}.{field}: contract {exp[field]!r}, game {cue[field]!r}")
        if sha(cue["text"]) != exp["text_sha256"]:
            failures.append(f"{cid}.text changed; recorded voice takes and review copy may be stale")
        exp_choices, got_choices = exp["choices"], cue["choices"]
        if (exp_choices is None) != (got_choices is None) or \
                (exp_choices is not None and len(exp_choices) != len(got_choices)):
            failures.append(f"{cid}.choices: branch structure differs from the contract")
        elif exp_choices is not None:
            for j, (e, g) in enumerate(zip(exp_choices, got_choices)):
                for field in ("next", "chooses_tactic", "set_flags"):
                    if g[field] != e[field]:
                        failures.append(f"{cid}.choices[{j}].{field}: contract {e[field]!r}, game {g[field]!r}")
                if sha(g["text"]) != e["text_sha256"]:
                    failures.append(f"{cid}.choices[{j}].text changed")

    by_id = {cue["id"]: cue for cue in ordered}
    for cid, pinned in contract["verbatim_pins"].items():
        cue = by_id.get(cid)
        if cue is None:
            failures.append(f"Verbatim-pinned cue missing: {cid}")
        elif cue["text"] != pinned:
            failures.append(f"{cid}.text is contract-pinned and must stay verbatim: {pinned!r}")

    used_sequences = []
    for cue in ordered:
        if cue["sequence"] not in used_sequences:
            used_sequences.append(cue["sequence"])
        if cue["sequence"] not in contract["sequences"]:
            failures.append(f"{cue['id']}.sequence: unknown sequence {cue['sequence']!r}")
        if cue["advance_stage"] is not None and cue["advance_stage"] not in contract["quest_stages"]:
            failures.append(f"{cue['id']}.advanceStage: unknown quest stage {cue['advance_stage']!r}")
        for flag in cue["set_flags"]:
            if flag not in contract["flag_glossary"]:
                failures.append(f"{cue['id']}: flag not in the glossary: {flag}")
        for j, choice in enumerate(cue["choices"] or []):
            tactic = choice["chooses_tactic"]
            if tactic is not None and tactic not in contract["diversion_tactics"]:
                failures.append(f"{cue['id']}.choices[{j}]: unknown tactic {tactic!r}")
            for flag in choice["set_flags"]:
                if flag not in contract["flag_glossary"]:
                    failures.append(f"{cue['id']}.choices[{j}]: flag not in the glossary: {flag}")
    for sequence in contract["sequences"]:
        if sequence not in used_sequences:
            failures.append(f"Sequence has no cues in the game: {sequence}")
    return failures


def build_contract(script_id, ordered, dialogue_relative, previous=None, pin=()):
    def carried(field):
        values = sorted({v for v in collected[field]})
        if previous is not None:
            values = sorted(set(previous[field]) | set(values))
        return values

    collected = {
        "quest_stages": [c["advance_stage"] for c in ordered if c["advance_stage"] is not None],
        "diversion_tactics": [ch["chooses_tactic"] for c in ordered for ch in c["choices"] or []
                              if ch["chooses_tactic"] is not None],
        "flag_glossary": [f for c in ordered for f in c["set_flags"]] +
                         [f for c in ordered for ch in c["choices"] or [] for f in ch["set_flags"]],
    }
    sequences = []
    for cue in ordered:
        if cue["sequence"] not in sequences:
            sequences.append(cue["sequence"])
    pins = dict(previous["verbatim_pins"]) if previous is not None else {}
    by_id = {cue["id"]: cue for cue in ordered}
    for cid in pin:
        mona.require(cid in by_id, f"Cannot pin unknown cue: {cid}")
        pins[cid] = by_id[cid]["text"]
    for cid, pinned in pins.items():
        mona.require(cid in by_id and by_id[cid]["text"] == pinned,
                     f"{cid} is verbatim-pinned; refusing to snapshot a changed line: {pinned!r}")
    cues = []
    for cue in ordered:
        choices = None
        if cue["choices"] is not None:
            choices = [{"text_sha256": sha(ch["text"]), "next": ch["next"],
                        "set_flags": ch["set_flags"], "chooses_tactic": ch["chooses_tactic"]}
                       for ch in cue["choices"]]
        cues.append({"id": cue["id"], "sequence": cue["sequence"], "speaker": cue["speaker"],
                     "text_sha256": sha(cue["text"]), "next": cue["next"],
                     "set_flags": cue["set_flags"], "advance_stage": cue["advance_stage"],
                     "choices": choices})
    return {"schema": CONTRACT_SCHEMA, "script_id": script_id,
            "game_dialogue_path": dialogue_relative, "entrypoints": entry_cues(ordered),
            "sequences": sequences, "quest_stages": carried("quest_stages"),
            "diversion_tactics": carried("diversion_tactics"),
            "flag_glossary": carried("flag_glossary"),
            "verbatim_pins": pins, "cues": cues}


def emit_beats(ordered):
    """The beat dictionary mona.py consumes. A cue's quest-stage advance becomes
    its effect ('advance_stage:<QuestStage>'); linear and terminal cues gain the
    single explicit choice the adapter requires. choosesTactic and setFlags are
    game-runtime detail and are not carried into the review beats."""
    beats = {}
    for cue in ordered:
        if cue["choices"] is not None:
            choices = [{"text": ch["text"], "next": ch["next"] or ""} for ch in cue["choices"]]
        elif cue["next"] is not None:
            choices = [{"text": "(continue)", "next": cue["next"]}]
        else:
            choices = [{"text": "(end)", "next": ""}]
        effect = f"advance_stage:{cue['advance_stage']}" if cue["advance_stage"] else ""
        beats[cue["id"]] = {"speaker": cue["speaker"], "text": cue["text"],
                            "effect": effect, "choices": choices}
    return beats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "snapshot", "emit-beats"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--game", type=Path, required=True)
        cmd.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
        cmd.add_argument("--dialogue", help="Game-relative dialogue path; defaults to the contract's")
        if name == "snapshot":
            cmd.add_argument("--pin", action="append", default=[],
                             help="Cue id whose current text becomes a verbatim pin")
        if name == "emit-beats":
            cmd.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        previous = load_contract(args.contract) if Path(args.contract).is_file() else None
        if args.command == "check":
            mona.require(previous is not None, f"Missing cue contract: {args.contract}")
        dialogue = args.dialogue or (previous["game_dialogue_path"] if previous
                                     else "data/dialogue/rescue_cues.json")
        script_id, ordered = parse_game_script(args.game, dialogue)
        entrypoints = previous["entrypoints"] if previous is not None else entry_cues(ordered)
        failures = graph_failures(ordered, entrypoints)
        result = {"cues": len(ordered), "sequences": len({c['sequence'] for c in ordered}),
                  "dialogue": dialogue}
        if args.command == "check":
            failures += contract_failures(previous, script_id, ordered)
        elif args.command == "snapshot":
            if failures:
                raise mona.Invalid("Refusing to snapshot a broken dialogue graph: " + failures[0])
            contract = build_contract(script_id, ordered, dialogue, previous, args.pin)
            target = Path(args.contract).absolute()
            mona.require(not target.resolve().parent.is_relative_to(Path(args.game).resolve()),
                         "The cue contract belongs in MONIX-REPO, not inside the game")
            target.write_bytes(mona.encoded(contract))
            result["contract"] = str(target)
            result["verbatim_pins"] = sorted(contract["verbatim_pins"])
        else:
            if failures:
                raise mona.Invalid("Refusing to emit beats from a broken dialogue graph: " + failures[0])
            out = Path(args.out).absolute()
            mona.require(not out.resolve().parent.is_relative_to(Path(args.game).resolve()),
                         "Beat output must be outside the game folder")
            mona.require(not out.exists(), "Beat output already exists; choose a new file")
            beats = emit_beats(ordered)
            out.write_bytes(mona.encoded(beats))
            result.update(beats=str(out), entrypoints=entry_cues(ordered),
                          effects_used=sorted({b["effect"] for b in beats.values() if b["effect"]}))
        result["status"] = "drift-detected" if failures else "passed"
        if failures:
            result["failures"] = failures
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if failures else 0
    except (mona.Invalid, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
