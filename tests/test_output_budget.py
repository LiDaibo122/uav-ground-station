import copy
import json
from pathlib import Path
import tempfile
import unittest

from agent_fixture import FixtureSession,environment
from packages.ground_core.agent import Agent


class OutputBudgetTests(unittest.TestCase):
    def test_generation_limit_is_distinct_from_context_and_telemetry_updates(self):
        replies=iter([
            {'message':{'role':'assistant','content':'','thinking':'inspect current state',
                'tool_calls':[{'function':{'name':'get_fleet_state','arguments':{}}}]},
             'prompt_eval_count':2360,'eval_count':220,'done_reason':'stop'},
            {'message':{'role':'assistant','content':'','thinking':'x'*100},
             'prompt_eval_count':3248,'eval_count':4096,'done_reason':'length'}])
        requests=[];session=FixtureSession()
        def model(payload):requests.append(copy.deepcopy(payload));return next(replies)
        with tempfile.TemporaryDirectory() as directory:
            agent=Agent(directory,model,'test',environment(session))
            result=agent.chat({'message':'查询并说明状态','mode':'live','conversation_id':'output-budget-test'})
            self.assertEqual(result['stop_reason'],'model_output_limit')
            self.assertIn('不表示输入上下文已满',result['reply'])
            self.assertEqual(sum(u['eval_count'] for u in result['model_usage']),4316)
            self.assertEqual(result['model_usage'][-1]['thinking_characters'],100)
            self.assertEqual(result['model_usage'][-1]['answer_characters'],0)
            self.assertEqual(result['model_usage'][-1]['tool_calls_returned'],0)
            self.assertEqual(len(result['tool_calls']),1)
            self.assertEqual(len(requests),2)
            self.assertFalse(session.calls)

    def test_in_turn_reasoning_is_returned_to_model_but_not_replayed_as_history(self):
        reasoning='bounded reasoning from external test model'
        replies=iter([
            {'message':{'role':'assistant','content':'','thinking':reasoning,
                'tool_calls':[{'function':{'name':'get_fleet_state','arguments':{}}}]}},
            {'message':{'role':'assistant','content':'已取得状态。'}},
            {'message':{'role':'assistant','content':'可以继续讨论。'}}])
        requests=[]
        def model(payload):requests.append(copy.deepcopy(payload));return next(replies)
        with tempfile.TemporaryDirectory() as directory:
            agent=Agent(directory,model,'test',environment(FixtureSession()))
            agent.chat({'message':'先查状态','mode':'live','conversation_id':'current-thinking-test'})
            self.assertTrue(any(m.get('thinking')==reasoning for m in requests[1]['messages']))
            saved=json.loads((Path(directory)/'runs/conversations/current-thinking-test.json').read_text())
            self.assertNotIn(reasoning,json.dumps(saved))
            agent.chat({'message':'继续讨论','mode':'live','conversation_id':'current-thinking-test'})
            self.assertFalse(any('thinking' in m for m in requests[2]['messages']))


if __name__=='__main__':unittest.main()
