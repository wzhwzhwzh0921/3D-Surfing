"""Evidence-based v5 acceptance does not inherit v4 object-class cutoffs."""
from dataclasses import replace
from pathlib import Path
import unittest

from scene_factory.screening.judge import ScreenConfig
from scene_factory.screening.rich import validate_decision


class ExplorationScreeningTests(unittest.TestCase):
    def setUp(self):
        self.config=ScreenConfig(captures=Path('/unused'),output=Path('/unused'),model='test',
                                url='https://test.invalid',version='v5',prompt='test')
        self.known={'az_045_closer':{},'inspect_1_1':{}}
        views=['inspect_1_1']
        self.decision=dict(uid='scene',action='decide',decision='keep',reason='已有多种可观察信息',
            environment='outdoor',scene_quality='usable',object_count_lower_bound=12,
            categories=[dict(name='工具',count_lower_bound=6,views=views),dict(name='容器',count_lower_bound=6,views=views)],
            regions=[dict(name=name,contents=['实际物件'],views=views) for name in ('工作台','储物区')],
            exploration_evidence=[dict(description='工作台背面有独立工具',views=views)],
            opportunities=[dict(question=f'目标{i}的可观察属性是什么？',answer='图像答案',target=f'目标{i}',
                                question_type=kind,exploration_requirement='需要看后方',views=views)
                           for i,kind in enumerate(('color','identity','spatial'))])

    def test_useful_outdoor_scene_can_pass_without_twenty_objects_or_eight_classes(self):
        result=validate_decision(self.decision,'scene',self.known,self.config)
        self.assertEqual(result['decision'],'keep')
        self.assertEqual(result['environment'],'outdoor')
        with self.assertRaises(ValueError):
            validate_decision(self.decision,'scene',self.known,replace(self.config,version='v4'))

    def test_same_target_three_times_does_not_prove_scene_richness(self):
        for question in self.decision['opportunities']:question['target']='同一个杯子'
        with self.assertRaises(ValueError):validate_decision(self.decision,'scene',self.known,self.config)

    def test_broken_scene_and_unknown_environment_cannot_pass(self):
        for field,value in (('scene_quality','unusable'),('environment','unknown')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_decision({**self.decision,field:value},'scene',self.known,self.config)

    def test_invented_evidence_is_not_accepted(self):
        self.decision['opportunities'][0]['views']=['unrendered_backside']
        with self.assertRaises(ValueError):validate_decision(self.decision,'scene',self.known,self.config)

    def test_sparse_scene_can_be_rejected_without_fabricating_questions(self):
        self.decision.update(decision='reject',scene_quality='unusable',opportunities=[])
        self.assertEqual(validate_decision(self.decision,'scene',self.known,self.config)['decision'],'reject')
