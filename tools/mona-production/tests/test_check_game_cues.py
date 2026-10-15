import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mona = load('mona')
checker = load('check_game_cues')


def game_doc():
    return {'scriptId': 'testscript', 'cues': [
        {'id': 'entry', 'sequence': 'seq_one', 'speaker': 'TEST', 'text': 'Test opening line.',
         'choices': [{'text': 'Ask why', 'next': 'middle'},
                     {'text': 'Act now', 'next': 'pledge', 'choosesTactic': 'PathB'}]},
        {'id': 'middle', 'sequence': 'seq_one', 'speaker': 'TEST', 'text': 'Test middle line.',
         'next': 'pledge'},
        {'id': 'pledge', 'sequence': 'seq_one', 'speaker': 'TEST', 'text': 'Test pledge line.',
         'setFlags': ['test.flag'], 'advanceStage': 'StageTwo'},
        {'id': 'coda', 'sequence': 'seq_two', 'speaker': 'TEST', 'text': 'Test coda line.',
         'advanceStage': 'Done'}]}


class GameCueContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.game = self.root / 'game'
        (self.game / 'data' / 'dialogue').mkdir(parents=True)
        self.contract = self.root / 'contract.json'
        self.doc = game_doc()
        self.save()
        self.snapshot('--pin', 'pledge')

    def save(self):
        (self.game / 'data' / 'dialogue' / 'rescue_cues.json').write_bytes(mona.encoded(self.doc))

    def run_tool(self, *argv):
        return checker.main([argv[0], '--game', str(self.game), '--contract', str(self.contract),
                             *argv[1:]])

    def snapshot(self, *extra):
        self.assertEqual(self.run_tool('snapshot', *extra), 0)

    def check_failures(self):
        code = self.run_tool('check')
        contract = json.loads(self.contract.read_text(encoding='utf-8'))
        self.assertEqual(contract['schema'], checker.CONTRACT_SCHEMA)
        return code

    def test_snapshot_then_check_passes(self):
        self.assertEqual(self.check_failures(), 0)
        contract = json.loads(self.contract.read_text(encoding='utf-8'))
        self.assertEqual([c['id'] for c in contract['cues']], ['entry', 'middle', 'pledge', 'coda'])
        self.assertEqual(contract['entrypoints'], ['entry', 'coda'])
        self.assertEqual(contract['verbatim_pins'], {'pledge': 'Test pledge line.'})
        self.assertNotIn('Test opening line.', self.contract.read_text(encoding='utf-8'))

    def test_missing_and_extra_cues_are_drift(self):
        removed = self.doc['cues'].pop(1)
        self.doc['cues'][0]['choices'][0]['next'] = 'pledge'
        self.doc['cues'].append({'id': 'orphan_entry', 'sequence': 'seq_two', 'speaker': 'TEST',
                                 'text': 'Test new line.', 'next': 'pledge'})
        self.save()
        self.assertEqual(self.check_failures(), 1)
        self.assertEqual(removed['id'], 'middle')

    def test_text_drift_is_reported_without_breaking_structure(self):
        self.doc['cues'][1]['text'] = 'Test revised middle line.'
        self.save()
        self.assertEqual(self.check_failures(), 1)

    def test_link_drift_is_reported(self):
        self.doc['cues'][0]['choices'][0]['next'] = 'pledge'
        self.save()
        self.assertEqual(self.check_failures(), 1)

    def test_verbatim_pin_blocks_check_and_snapshot(self):
        self.doc['cues'][2]['text'] = 'Test reworded pledge.'
        self.save()
        self.assertEqual(self.check_failures(), 1)
        self.assertEqual(self.run_tool('snapshot'), 1)

    def test_unknown_field_is_rejected(self):
        self.doc['cues'][0]['mood'] = 'tense'
        self.save()
        self.assertEqual(self.run_tool('check'), 1)

    def test_both_next_and_choices_rejected(self):
        self.doc['cues'][1]['choices'] = [{'text': 'Loop', 'next': 'entry'}]
        self.save()
        self.assertEqual(self.run_tool('check'), 1)

    def test_more_than_three_choices_rejected(self):
        self.doc['cues'][0]['choices'] += [{'text': f'Extra {i}', 'next': 'middle'} for i in (1, 2)]
        self.save()
        self.assertEqual(self.run_tool('check'), 1)

    def test_cycle_is_drift(self):
        self.doc['cues'][2]['next'] = 'entry'
        self.save()
        self.assertEqual(self.check_failures(), 1)

    def test_unreachable_cue_is_drift(self):
        self.doc['cues'].append({'id': 'island', 'sequence': 'seq_two', 'speaker': 'TEST',
                                 'text': 'Test island line.', 'next': 'island_b'})
        self.doc['cues'].append({'id': 'island_b', 'sequence': 'seq_two', 'speaker': 'TEST',
                                 'text': 'Test island reply.', 'next': 'island'})
        self.save()
        # The two new cues link to each other, so neither is an entry cue: the
        # contract's entrypoints cannot reach them and their cycle is reported.
        self.assertEqual(self.check_failures(), 1)

    def test_unknown_stage_and_tactic_are_drift(self):
        self.doc['cues'][3]['advanceStage'] = 'StageNine'
        self.doc['cues'][0]['choices'][1]['choosesTactic'] = 'PathZ'
        self.doc['cues'][2]['setFlags'] = ['unlisted.flag']
        self.save()
        self.assertEqual(self.check_failures(), 1)

    def test_snapshot_carries_vocabulary_and_pins_forward(self):
        contract = json.loads(self.contract.read_text(encoding='utf-8'))
        contract['quest_stages'].append('StageReserved')
        contract['flag_glossary'].append('reserved.flag')
        self.contract.write_bytes(mona.encoded(contract))
        self.snapshot()
        contract = json.loads(self.contract.read_text(encoding='utf-8'))
        self.assertIn('StageReserved', contract['quest_stages'])
        self.assertIn('reserved.flag', contract['flag_glossary'])
        self.assertEqual(contract['verbatim_pins'], {'pledge': 'Test pledge line.'})
        self.assertEqual(self.check_failures(), 0)

    def test_emitted_beats_satisfy_the_adapter(self):
        out = self.root / 'beats.json'
        self.assertEqual(self.run_tool('emit-beats', '--out', str(out)), 0)
        beats = mona.decode(out.read_bytes())
        profile = {'allowed_effects': ['', 'advance_stage:StageTwo', 'advance_stage:Done'],
                   'entrypoints': ['entry', 'coda'],
                   'rescue_segments': [{'entry': 'entry', 'required_effect': 'advance_stage:StageTwo'},
                                       {'entry': 'coda', 'required_effect': 'advance_stage:Done'}]}
        routes = mona.validate_dialogue(beats, profile)
        self.assertEqual(routes[0]['terminal_paths'], 2)
        self.assertEqual(beats['middle']['choices'], [{'text': '(continue)', 'next': 'pledge'}])
        self.assertEqual(beats['pledge']['choices'], [{'text': '(end)', 'next': ''}])

    def test_emit_refuses_output_inside_game_or_overwrite(self):
        self.assertEqual(self.run_tool('emit-beats', '--out', str(self.game / 'beats.json')), 1)
        outside = self.root / 'beats.json'
        outside.write_text('keep')
        self.assertEqual(self.run_tool('emit-beats', '--out', str(outside)), 1)
        self.assertEqual(outside.read_text(), 'keep')

    def test_copy_of_contract_matches_snapshot_bytes(self):
        before = self.contract.read_bytes()
        self.snapshot()
        self.assertEqual(before, self.contract.read_bytes())

    def test_broken_graph_blocks_snapshot_and_emit(self):
        self.doc['cues'][1]['next'] = 'missing'
        self.save()
        self.assertEqual(self.run_tool('snapshot'), 1)
        self.assertEqual(self.run_tool('emit-beats', '--out', str(self.root / 'b.json')), 1)


class RealContractTests(unittest.TestCase):
    """The committed Two Covens contract itself stays well-formed."""

    def test_committed_contract_shape(self):
        contract = checker.load_contract(checker.DEFAULT_CONTRACT)
        self.assertEqual(contract['script_id'], 'rescue')
        self.assertEqual(len(contract['cues']), 20)
        self.assertEqual(contract['entrypoints'], ['recall_01', 'prisoner_01', 'warden_01', 'gate_01'])
        self.assertEqual(contract['sequences'], ['TheRecall', 'ThePrisoner', 'TheDiversion',
                                                 'TheCrossing', 'DividedLoyalty'])
        self.assertEqual(contract['verbatim_pins'],
                         {'gate_02': 'They can decide what to call me later. Cross.'})
        for cue in contract['cues']:
            self.assertRegex(cue['text_sha256'], r'^[a-f0-9]{64}$')


if __name__ == '__main__':
    unittest.main()
