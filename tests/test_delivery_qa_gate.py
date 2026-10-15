"""Final-review feedback and caching with metadata fixtures, without model calls."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO/'tools'),str(REPO/'experiments/sol_augment')]
import delivery_qa_gate as gate
import scene_feedback_loop as feedback
from scene_factory.automation import save


class FinalGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        native=self.root/'native';native.mkdir()
        (native/'scene.glb').write_bytes(struct.pack('<4sII',b'glTF',2,12))
        for name in ('start.png','reveal.png'):(native/name).write_bytes(b'image fixture')
        self.result=self.root/'original.json'
        self.item=dict(level='medium',glb=str(native/'scene.glb'),question='original fixture',answer='model answer',
             start_image='start.png',reveal_image='reveal.png',initial_camera=dict(position=[0,0,1]))
        save(self.result,dict(terminal=True,items=[self.item]))
        self.folder=self.root/'initial-delivery';self.folder.mkdir()
        for name in ('scene.glb','start.png','reveal.png'):shutil.copy2(native/name,self.folder/name)
        save(self.folder/'question.json',self.item);save(self.folder/'audit.json',{})
        self.row=dict(uid='fixture',folder=str(self.folder),result_path=str(self.result),
             delivery_sha256={p.name:feedback.sha(p) for p in self.folder.iterdir()})
        self.profile=dict(version='fixture-protocol',model='fixture-model');self.questions=[];self.repairs=[]

    def tearDown(self):self.temp.cleanup()

    def tools(self,decisions):
        def call(folder,prompt,images,feedback):
            self.questions.append(json.loads(feedback)['question'])
            return decisions.pop(0)
        def repair(result_path,critic_path,out):
            self.repairs.append(json.loads(Path(critic_path).read_text())['judgment']['repair_action'])
            prior=json.loads(Path(result_path).read_text());old=prior['items'][0];dest=Path(out)/'native'
            shutil.copytree(Path(old['glb']).parent,dest)
            changed={**old,'question':'repaired '+str(len(self.repairs)), 'glb':str(dest/'scene.glb')}
            result=Path(out)/'result.json';save(result,{**prior,'items':[changed]});save(Path(out)/'state.json',dict(status='complete'))
            return result
        return SimpleNamespace(call=call),SimpleNamespace(repair=repair)

    def decision(self,passing,action):
        return dict(pass_=passing) | {'pass':passing,'observed_answer':'model answer','reason':'fixture judgment','repair_action':action}

    def test_failed_question_is_repaired_then_exported_and_cached(self):
        augment,repair=self.tools([self.decision(False,'question'),self.decision(True,'none')])
        first=gate.review(self.row,self.root/'reviews',self.profile,augment,repair,feedback)
        self.assertEqual(first['status'],'keep');self.assertEqual(self.repairs,['question'])
        self.assertEqual(self.questions,['original fixture','repaired 1'])
        self.assertEqual(json.loads((Path(first['folder'])/'question.json').read_text())['question'],'repaired 1')
        again=gate.review(self.row,self.root/'reviews',self.profile,augment,repair,feedback)
        self.assertEqual(first,again);self.assertEqual(len(self.questions),2)

    def test_reject_action_is_terminal_without_unrequested_scene_changes(self):
        augment,repair=self.tools([self.decision(False,'reject')])
        result=gate.review(self.row,self.root/'reviews',self.profile,augment,repair,feedback)
        self.assertEqual(result['status'],'reject');self.assertEqual(self.repairs,[])

    def test_two_repairs_are_bounded_and_all_failed_reviews_are_preserved(self):
        augment,repair=self.tools([self.decision(False,'question') for _ in range(3)])
        result=gate.review(self.row,self.root/'reviews',self.profile,augment,repair,feedback)
        self.assertEqual(result['status'],'reject');self.assertEqual(len(self.repairs),2)
        self.assertEqual(len(result['history']),3)

    def test_schema_or_transport_failure_is_not_a_quality_reject(self):
        augment,repair=self.tools([{'pass':True}])
        result=gate.review(self.row,self.root/'reviews',self.profile,augment,repair,feedback)
        self.assertEqual(result['status'],'failed');self.assertIn('Invalid final review schema',result['error'])


if __name__=='__main__':unittest.main()
