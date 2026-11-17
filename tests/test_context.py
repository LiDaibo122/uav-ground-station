import copy
import json
from pathlib import Path
import unittest

from packages.ground_core.context import (ContextBudgetError,build_context_messages,
    compact_tool_result,estimate_tokens,history_pair)
from packages.ground_core.agent import SYSTEM
from packages.ground_core.agent_tools import REGISTRY


ROOT=Path(__file__).resolve().parents[1]
TOOLS=[definition['wire'] for definition in REGISTRY]


def result(name,value):
    return {'role':'tool','tool_name':name,'content':json.dumps({'ok':True,'result':value},ensure_ascii=False)}


def calls(name,args=None):
    return {'role':'assistant','content':'','tool_calls':[{'function':{'name':name,'arguments':args or {}}}]}


class ContextTests(unittest.TestCase):
    def test_single_fleet_snapshot_keeps_flight_facts_without_repeated_prose(self):
        raw={'ok':True,'result':{'observed_at':123,'aircraft':[{'id':'FW-01','armed':False,'landed':True,
            'flight_mode':'AUTO.LOITER','battery_pct':100,'longitude':103.565,'latitude':31.045}],
            'planning_fleet':[{'id':'FW-01','east_m':0,'north_m':0,'battery_pct':100,'capabilities':['rgb'],'available':True}],
            'summary':'duplicate prose'*300,'sensor_text_observations':{'FW-01':{'kind':'flight_state','text':'duplicate'*100,'disaster_detections':[]}}}}
        before=copy.deepcopy(raw);packed=compact_tool_result('get_fleet_state',raw)
        self.assertEqual(raw,before)
        aircraft=packed['result']['aircraft'][0]
        self.assertFalse(aircraft['armed']);self.assertTrue(aircraft['landed'])
        self.assertEqual(aircraft['capabilities'],['rgb'])
        self.assertEqual(aircraft['longitude'],103.565)
        self.assertLess(len(json.dumps(packed)),len(json.dumps(raw))/3)
        self.assertNotIn('planning_fleet',packed['result'])

    def test_nonflight_sensor_observation_is_retained(self):
        raw={'ok':True,'result':{'aircraft':[],'sensor_text_observations':{'FW-03':{'kind':'inspection',
            'text':'河道疑似积水','disaster_detections':[{'class':'flood','confidence':.7}]}}}}
        packed=compact_tool_result('get_fleet_state',raw)
        self.assertEqual(packed['result']['sensor_observations'][0]['text'],'河道疑似积水')

    def test_historical_telemetry_is_not_replayed_as_current(self):
        turn=[{'role':'user','content':'刚才状态怎样'},calls('get_fleet_state'),
              result('get_fleet_state',{'aircraft':[{'id':'FW-01','battery_pct':2,'source':'OLD_SENSOR_MARKER'}]})]
        pair=history_pair(turn)
        self.assertNotIn('OLD_SENSOR_MARKER',json.dumps(pair))
        self.assertIn('not_current_telemetry',pair[1]['content'])
        self.assertNotIn('battery_pct',pair[1]['content'])

    def test_targets_and_execution_receipt_remain_exact(self):
        target={'frame':'local','x':1100,'y':50,'priority':'normal','sensor':'rgb'}
        plan={'id':'p123','algorithm':'cbba','draft':{'targets':[target],'aircraft_ids':['FW-01']},
              'allocation':{'algorithm_id':'cbba','assignments':{'FW-01':['T1']},'input_fleet':['large']*500},
              'execution':{'id':'exec1','status':'queued'},'executed':True}
        packed=compact_tool_result('get_plan',{'ok':True,'result':plan})['result']
        self.assertEqual(packed['targets'],[target]);self.assertEqual(packed['execution']['id'],'exec1')
        self.assertNotIn('input_fleet',packed['allocation'])

    def test_current_user_and_tool_pairs_remain_intact(self):
        user='先帮我规划，不要执行。\\\n东1100米、北50米。'
        turn=[{'role':'user','content':user},calls('get_fleet_state'),result('get_fleet_state',{'aircraft':[]})]
        messages,meta=build_context_messages(SYSTEM,{'latest_plan':None},[],turn,TOOLS)
        self.assertEqual(messages[1]['content'],user)
        self.assertEqual(messages[2]['tool_calls'],turn[1]['tool_calls'])
        self.assertEqual(messages[3]['role'],'tool')
        self.assertLessEqual(meta['estimated_input_tokens'],meta['input_budget_tokens'])
        self.assertEqual(meta['reserved_generation_tokens'],4096)

    def test_domain_anchor_survives_history_eviction(self):
        context={'latest_plan':{'id':'stable-plan','targets':[{'frame':'local','x':1100,'y':50}],
                               'algorithm':'cbba','aircraft_ids':['FW-01']},
                 'last_selected_aircraft':['FW-01'],'last_execution_id':'actual-ack'}
        old=[[{'role':'user','content':'旧问题'*4000},{'role':'assistant','content':'旧回复'}]]
        messages,meta=build_context_messages(SYSTEM,context,old,[{'role':'user','content':'就按刚才那个'}],TOOLS)
        self.assertEqual(meta['history_turns_omitted'],1)
        self.assertIn('stable-plan',messages[0]['content']);self.assertIn('actual-ack',messages[0]['content'])
        self.assertIn('1100',messages[0]['content'])

    def test_oversized_required_input_fails_before_model_request(self):
        with self.assertRaises(ContextBudgetError):
            build_context_messages(SYSTEM,{},[],[{'role':'user','content':'完整保留'*10000}],TOOLS)

    def test_long_current_round_preserves_recent_tool_pairs(self):
        turn=[{'role':'user','content':'持续比较这些方案'}]
        for index in range(6):
            turn.extend([calls('get_fleet_state'),result('get_fleet_state',{'aircraft':[{
                'id':'FW-01','battery_pct':100-index,'capabilities':['rgb']*300}]})])
        messages,meta=build_context_messages(SYSTEM,{'latest_plan':None},[],turn,TOOLS)
        self.assertGreater(meta['earlier_current_tool_blocks_summarized'],0)
        self.assertEqual(messages[-2]['role'],'assistant');self.assertEqual(messages[-1]['role'],'tool')
        self.assertLessEqual(meta['estimated_input_tokens'],meta['input_budget_tokens'])

    def test_actual_ground_speed_does_not_become_nominal_cruise_speed(self):
        raw={'ok':True,'result':{'aircraft':[{'id':'FW-01','speed_mps':0,'armed':False,'landed':True}],
            'planning_fleet':[{'id':'FW-01','speed_mps':18,'measured_ground_speed_mps':0,'capabilities':['rgb']}]}}
        row=compact_tool_result('get_fleet_state',raw)['result']['aircraft'][0]
        self.assertEqual(row['speed_mps'],0);self.assertEqual(row['planning_speed_mps'],18)

    def test_recorded_context_is_reduced_and_latest_request_preserved(self):
        path=ROOT/'runs/conversations/dcb2bdf2-9dee-4938-a2fc-53e414a1d322.json'
        if not path.exists():self.skipTest('local reported conversation unavailable')
        conversation=json.loads(path.read_text());turns=conversation['turns']
        active=turns[-1]
        plan=conversation.get('plans',{}).get(conversation.get('last_plan_id'),{})
        context={'latest_plan':{'id':plan.get('id'),'targets':plan.get('draft',{}).get('targets',[]),
                 'algorithm':plan.get('algorithm'),'aircraft_ids':plan.get('draft',{}).get('aircraft_ids',[])},
                 'last_selected_aircraft':conversation.get('last_selected_aircraft',[]),
                 'last_execution_id':conversation.get('last_execution_id')}
        messages,meta=build_context_messages(SYSTEM,context,turns[:-1],active,TOOLS)
        users=[message['content'] for message in messages if message['role']=='user']
        original=next(message['content'] for message in active if message['role']=='user')
        self.assertEqual(users[-1],original)
        self.assertLess(meta['input_characters'],len(json.dumps(turns,ensure_ascii=False))*.6)
        self.assertLessEqual(estimate_tokens(messages,TOOLS),16384-4096-1024)


if __name__=='__main__':unittest.main()
