import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1] / 'mona.py'
spec = importlib.util.spec_from_file_location('mona', TOOL)
mona = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mona)


def fixture(root):
    game = root / 'game'
    game.mkdir()
    director = b'// Synthetic test runtime contract, not game lore.\n'
    (game / 'Director.cs').write_bytes(director)
    (game / 'project.godot').write_text('config_version=5\n')
    (game / 'Test.csproj').write_text('<Project Sdk="Godot.NET.Sdk/4.7.0"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
    beat = lambda effect, target='': {'speaker':'TEST', 'text':'Test line', 'effect':effect,
                                      'choices':[{'text':'Continue', 'next':target}]}
    beats = {'entry':beat('', 'promise'), 'promise':beat('rescue_started'),
             'guard':beat('distract_warden'), 'gate':beat('complete_rescue')}
    profile = {'schema':mona.PROFILE_SCHEMA,'project_id':'3e822cef-10b1-40e9-a07b-512a3a4ca0a8',
               'title':'Test production','slug':'test_production','snapshot_at_utc':'2026-10-14T00:00:00Z',
               'canon_status':'PROPOSED_NOT_APPROVED','confirmed_direction':['Rescue is required'],
               'dialogue_path':'dialogue.json','voice_directory':'voice',
               'allowed_effects':['','rescue_started','distract_warden','complete_rescue'],
               'entrypoints':['entry','guard','gate'],
               'rescue_segments':[{'entry':r,'required_effect':e} for r,e in
                                  [('entry','rescue_started'),('guard','distract_warden'),('gate','complete_rescue')]],
               'runtime_contract':{'path':'Director.cs','sha256':mona.digest(director)},
               'shots':[{'id':'scene_one','title':'Test','purpose':'Test','cues':list(beats)}],
               'assets':[{'id':'environment','name':'Background','brief':'A test background', 'kind':'image','path':None}]}
    (game / 'profile.json').write_bytes(mona.encoded(profile))
    (game / 'dialogue.json').write_bytes(mona.encoded(beats))
    return game, profile, beats


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.game, self.profile, self.beats = fixture(self.root)

    def save(self):
        (self.game/'profile.json').write_bytes(mona.encoded(self.profile))
        (self.game/'dialogue.json').write_bytes(mona.encoded(self.beats))

    def build(self, dest='review'):
        return mona.prepare(self.game, 'profile.json', self.root/dest)

    def test_repeatable_snapshot_and_read_only_inputs(self):
        before = {p.relative_to(self.game):p.read_bytes() for p in self.game.rglob('*') if p.is_file()}
        self.build('a'); self.build('b')
        a = {p.relative_to(self.root/'a'):p.read_bytes() for p in (self.root/'a').rglob('*') if p.is_file()}
        b = {p.relative_to(self.root/'b'):p.read_bytes() for p in (self.root/'b').rglob('*') if p.is_file()}
        self.assertEqual(a,b)
        self.assertEqual(before,{p.relative_to(self.game):p.read_bytes() for p in self.game.rglob('*') if p.is_file()})
        result = mona.verify(self.root/'a', self.game, 'profile.json')
        self.assertEqual(result['source_rebuild'],'passed')

    def test_rejects_dangling_choice(self):
        self.beats['entry']['choices'][0]['next']='missing'; self.save()
        with self.assertRaisesRegex(mona.Invalid,'Dangling'): self.build()
        self.assertFalse((self.root/'review').exists())

    def test_rejects_cycle_even_with_escape_choice(self):
        self.beats['entry']['choices'].append({'text':'Loop','next':'entry'}); self.save()
        with self.assertRaisesRegex(mona.Invalid,'cycle'): self.build()

    def test_rejects_rescue_bypass_branch(self):
        self.beats['entry']['choices'].append({'text':'Skip rescue','next':''}); self.save()
        with self.assertRaisesRegex(mona.Invalid,'without rescue_started'): self.build()

    def test_both_strategies_keep_rescue_effect(self):
        self.beats['entry']['choices'].append({'text':'Alternate','next':'promise'}); self.save()
        report=self.build()
        self.assertEqual(report['rescue_segments'][0]['terminal_paths'],2)

    def test_unreachable_cue_is_an_error(self):
        self.beats['orphan']=copy.deepcopy(self.beats['gate']); self.save()
        with self.assertRaisesRegex(mona.Invalid,'unreachable'): self.build()

    def test_every_cue_requires_exactly_one_shot(self):
        self.profile['shots'][0]['cues'].append('entry'); self.save()
        with self.assertRaisesRegex(mona.Invalid,'repeated'): self.build()

    def test_existing_output_is_never_overwritten(self):
        dest=self.root/'review'; dest.mkdir(); (dest/'precious.txt').write_text('keep')
        with self.assertRaisesRegex(mona.Invalid,'already exists'): self.build()
        self.assertEqual((dest/'precious.txt').read_text(),'keep')

    def test_output_cannot_be_inside_game(self):
        with self.assertRaisesRegex(mona.Invalid,'outside'):
            mona.prepare(self.game,'profile.json',self.game/'review')

    def test_changed_runtime_requires_profile_review(self):
        (self.game/'Director.cs').write_text('// new effects')
        with self.assertRaisesRegex(mona.Invalid,'GameDirector changed'): self.build()

    def test_canon_cannot_be_silently_approved(self):
        self.profile['canon_status']='APPROVED'; self.save()
        with self.assertRaisesRegex(mona.Invalid,'Canon approval'): self.build()

    def test_traversal_absolute_and_symlink_rejected(self):
        outside=self.root/'private.txt'; outside.write_text('private')
        for bad in ['../private.txt','/etc/passwd','C:/private.txt','voice\\take.wav']:
            with self.subTest(path=bad), self.assertRaises(mona.Invalid): mona.read(self.game,bad)
        (self.game/'linked').symlink_to(outside)
        with self.assertRaisesRegex(mona.Invalid,'Symlink'): mona.read(self.game,'linked')

    def test_voice_is_missing_or_unreviewed_never_approved(self):
        self.assertEqual(self.build('empty')['voice_takes_present'],0)
        (self.game/'voice').mkdir(); (self.game/'voice/entry.ogg').write_bytes(b'OggS-test-fixture')
        self.assertEqual(self.build('audio')['voice_takes_present'],1)
        handoff=json.loads((self.root/'audio/Production/mona.production-handoff.v1.json').read_text())
        take=next(r for r in handoff['records'] if r['id']=='vo:entry')
        self.assertEqual(take['state'],'present-unreviewed')
        self.assertEqual(take['sha256'],mona.digest(b'OggS-test-fixture'))
        (self.game/'voice/entry.wav').write_bytes(b'RIFF-test')
        with self.assertRaisesRegex(mona.Invalid,'Choose one'): self.build('ambiguous')

    def test_empty_voice_is_an_error(self):
        (self.game/'voice').mkdir(); (self.game/'voice/entry.ogg').write_bytes(b'')
        with self.assertRaisesRegex(mona.Invalid,'Empty voice'): self.build()

    def test_named_art_file_is_copied_and_indexed_without_sweeping_directory(self):
        (self.game/'background.png').write_bytes(b'test placeholder')
        (self.game/'private_master.txt').write_text('Do not copy')
        self.profile['assets'][0]['path']='background.png'; self.save()
        report=self.build()
        self.assertEqual(report['art_assets_present'],1)
        self.assertEqual(report['art_assets_needed'],0)
        root=self.root/'review'
        self.assertFalse(any(p.name=='private_master.txt' for p in root.rglob('*')))
        index=json.loads((root/'.monixproject/assets/monix.assets.json').read_text())
        asset=next(a for a in index['assets'] if a['type']=='image')
        self.assertEqual((root/asset['relativePath']).read_bytes(),b'test placeholder')

    def test_tamper_and_extra_files_fail_integrity(self):
        self.build('tamper'); (self.root/'tamper/review.html').write_text('changed')
        with self.assertRaisesRegex(mona.Invalid,'changed'): mona.verify(self.root/'tamper')
        self.build('extra'); (self.root/'extra/untracked.txt').write_text('new')
        with self.assertRaisesRegex(mona.Invalid,'extra'): mona.verify(self.root/'extra')

    def test_stale_source_fails_rebuild(self):
        self.build(); self.beats['entry']['text']='Revised line'; self.save()
        with self.assertRaisesRegex(mona.Invalid,'differs'):
            mona.verify(self.root/'review',self.game,'profile.json')

    def test_json_duplicates_and_nonfinite_rejected(self):
        for value in ['{"id":1,"id":2}', '{"value":NaN}']:
            with self.assertRaises(mona.Invalid): mona.decode(value)

    def test_html_and_csv_are_data_not_executable_content(self):
        self.beats['entry']['text']='<script>alert(1)</script>'
        self.beats['promise']['text']='=IMPORTXML("https://example.test","//x")'; self.save()
        self.build()
        review=(self.root/'review/review.html').read_text()
        self.assertNotIn('<script>',review)
        self.assertIn('&lt;script&gt;',review)
        exported=(self.root/'review/.monixproject/assets/voice_cues.csv').read_text()
        self.assertIn("'=IMPORTXML",exported)
        self.assertEqual(json.loads((self.root/'review/.monixproject/assets/dialogue_act01.json').read_text()),self.beats)

    def test_native_manifest_has_real_asset_paths_and_no_fake_renderer(self):
        self.build()
        root=self.root/'review'
        project=json.loads((root/'.monixproject/project.v2.monix.json').read_text())
        self.assertEqual(project['schema'],'monix/project/2.0')
        self.assertIsNone(project['references']['runtimeManifest'])
        self.assertIsNone(project['references']['scenesIndex'])
        self.assertNotIn('cloudIdentity',project)
        index=json.loads((root/project['references']['assetsIndex']).read_text())
        for asset in index['assets']:
            self.assertTrue((root/asset['relativePath']).is_file())
            self.assertEqual(asset['type'],'text')
            self.assertIsNone(asset['sourceJobId'])

    @patch.object(mona.shutil,'which',return_value=None)
    def test_missing_toolchains_block_runtime_gate(self,_):
        result, code=mona.check_game(self.game)
        self.assertEqual(code,2)
        self.assertEqual(result['game_runtime'],'not-tested')
        self.assertEqual(result['status'],'blocked')


if __name__=='__main__':
    unittest.main()
