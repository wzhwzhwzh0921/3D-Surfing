"""Frozen originals and all-or-nothing review gates for user-request extensions."""
import copy
import unittest

from scene_factory.followup import validate_plan, validate_answers, review_accepted, validated_call


def plan(mode="bundle"):
    reqs = [{"id":"original", "question":"木箱中是什么？", "target":"木箱", "type":"identity",
             "views":["front"], "blocked_by":"房屋"}]
    reqs += [{"id":k,"question":"追加要求","target":k,"type":"color","views":["back"],"blocked_by":"围墙"}
             for k in (["extra_1","extra_2"] if mode=="bundle" else ["final"])]
    hops = []
    if mode=="chain":
        reqs[-1]["depends_on"]="h2"
        hops=[{"id":k,"depends_on":p,"instruction":"比较上一目标旁的候选","candidates":["A","B"],"views":["front"]}
              for k,p in [("h1","original"),("h2","h1")]]
    return {"action":"propose","mode":mode,"followup":"还请完成这些要求。","requirements":reqs,"hops":hops}


def answers(p):
    return {"answers":[{"id":r["id"],"answer":"视觉答案","clear":True,"evidence_views":["front"]}
                       for r in p["requirements"]],
            "hop_answers":[{"id":h["id"],"selected":"实际选中的物件","evidence_views":["back"]} for h in p["hops"]]}


def review(p):
    return {"pass":True,"followup_natural":True,"original_preserved":True,"additional_work":True,
            "cross_view_required":True,"reason":"全部有证据",
            "checks":[{"id":r["id"],"pass":True,"start_hidden":True,"observed_answer":"视觉答案","reason":"目标被遮挡"}
                      for r in p["requirements"]],
            "hop_checks":[{"id":h["id"],"pass":True,"linked_to_previous":True,"ablation_keeps_ambiguous":True,
                           "candidate_count":2,"reason":"两个真实候选，必须用上一跳定位"} for h in p["hops"]]}


class FollowupTests(unittest.TestCase):
    def test_original_cannot_be_replaced_by_a_new_question(self):
        p=plan();p["requirements"][0]["question"]="改写后的原题"
        with self.assertRaisesRegex(ValueError,"cannot be rewritten"):
            validate_plan(p,"木箱中是什么？","bundle",{"front":{},"back":{}})

    def test_one_wrong_missing_or_unknown_answer_cannot_be_averaged_away(self):
        p=plan();a=answers(p);j=review(p)
        self.assertTrue(review_accepted(j,p,a))
        bad=copy.deepcopy(j);bad["checks"][2]["pass"]=False
        self.assertFalse(review_accepted(bad,p,a))
        unknown=copy.deepcopy(a);unknown["answers"][1]["clear"]=False
        self.assertFalse(review_accepted(j,p,unknown))
        missing=copy.deepcopy(a);missing["answers"].pop()
        with self.assertRaises(ValueError):validate_answers(missing,p,{"front":{},"back":{}})

    def test_decorative_or_disconnected_hops_are_not_accepted(self):
        p=plan("chain");a=answers(p);j=review(p)
        validate_plan(p,"木箱中是什么？","chain",{"front":{},"back":{}})
        self.assertTrue(review_accepted(j,p,a))
        bad=copy.deepcopy(j);bad["hop_checks"][0]["ablation_keeps_ambiguous"]=False
        self.assertFalse(review_accepted(bad,p,a))
        bad=copy.deepcopy(j);bad["hop_checks"][1]["candidate_count"]=1
        self.assertFalse(review_accepted(bad,p,a))
        bad=copy.deepcopy(p);bad["hops"][1]["depends_on"]="original"
        with self.assertRaisesRegex(ValueError,"previous selection"):
            validate_plan(bad,"木箱中是什么？","chain",{"front":{},"back":{}})

    def test_fabricated_evidence_is_rejected(self):
        p=plan();p["requirements"][2]["views"]=["nonexistent"]
        with self.assertRaises(ValueError):validate_plan(p,"木箱中是什么？","bundle",{"front":{},"back":{}})

    def test_valid_spatial_chain_may_fit_one_reveal_while_bundle_requires_distinct_views(self):
        p=plan("chain");j=review(p);j["cross_view_required"]=False
        self.assertTrue(review_accepted(j,p,answers(p)))
        p=plan("bundle");j=review(p);j["cross_view_required"]=False
        self.assertFalse(review_accepted(j,p,answers(p)))

    def test_format_repair_preserves_first_response_and_uses_new_request_folder(self):
        from pathlib import Path
        calls = []
        p = plan()
        class Model:
            def call(self, folder, prompt, images, feedback=""):
                calls.append((folder, feedback))
                return {} if len(calls) == 1 else answers(p)
        result = validated_call(Model(), Path("answer"), "prompt", [],
            lambda v: validate_answers(v, p, {"front": {}, "back": {}}))
        self.assertEqual(result, answers(p))
        self.assertEqual(calls[0], (Path("answer"), ""))
        self.assertEqual(calls[1][0], Path("answer_format_1"))
        self.assertTrue(calls[1][1])


if __name__=="__main__":unittest.main()
