import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from packages.ground_core.agent import Agent
from packages.ground_core.agent_tools import REGISTRY
from packages.ground_core.context import ContextBudgetError,build_context_messages
from agent_fixture import FixtureSession,environment,target


def call(name,args):
    return {'role':'assistant','content':'','tool_calls':[{'function':{'name':name,'arguments':args}}]}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.session=FixtureSession();self.requests=[]

    def agent(self,responses,env=None):
        iterator=iter(responses)
        def model(payload):
            self.requests.append(payload)
            response=next(iterator)
            if isinstance(response,Exception):raise response
            return {'message':response}
        return Agent(Path(self.tmp.name),model,'actual-test-model',env or environment(self.session))

    def test_noncanonical_request_uses_model_selected_tool(self):
        agent=self.agent([call('takeoff_aircraft',{'aircraft_ids':['FW-01']}),{'role':'assistant','content':'一号机的起飞任务已排队，其余保持待命。'}])
        result=agent.chat({'message':'一号先上去绕一绕，其他先别动','mode':'live'})
        self.assertEqual(self.session.calls[0][0:2],('takeoff',['FW-01']))
        self.assertEqual(result['tool_calls'][0]['selected_by'],'qwen')
        self.assertIn('tools',self.requests[0]);self.assertNotIn('format',self.requests[0])
        self.assertFalse(result['dispatched'])

    def test_words_alone_never_bypass_model(self):
        agent=self.agent([{'role':'assistant','content':'这是操作原理说明，本次没有执行。'}])
        agent.chat({'message':'讨论一下“无人机起飞”这个功能，不要操作','mode':'live'})
        self.assertFalse(self.session.calls)
        self.assertEqual(len(self.requests),1)

    def test_reply_timeout_preserves_real_receipt_and_conversation(self):
        agent=self.agent([call('takeoff_aircraft',{'aircraft_ids':['FW-01']}),TimeoutError('model reply timed out')])
        result=agent.chat({'message':'一号先去空中等候','mode':'live','conversation_id':'timeout-receipt'})
        self.assertEqual(result['execution']['id'],'fixture-takeoff')
        self.assertEqual(result['model_error'],'model reply timed out')
        saved=json.loads((Path(self.tmp.name)/'runs/conversations/timeout-receipt.json').read_text())
        self.assertEqual(saved['last_selected_aircraft'],['FW-01'])
        self.assertTrue(any(m['role']=='tool' for m in saved['turns'][0]))
        self.assertEqual(saved['turns'][0][-1]['role'],'assistant')

    def test_demo_exposes_no_actuation_tools(self):
        agent=self.agent([call('takeoff_aircraft',{'aircraft_ids':[]}),{'role':'assistant','content':'演示模式不能操作飞机。'}])
        result=agent.chat({'message':'让飞机飞起来','mode':'demo'})
        self.assertFalse(self.session.calls)
        self.assertEqual(result['tool_calls'][0]['status'],'error')
        names=[t['function']['name'] for t in self.requests[0]['tools']]
        self.assertNotIn('takeoff_aircraft',names)

    def test_plan_compare_and_multiturn_execution(self):
        agent=self.agent([call('plan_inspection',{'targets':[target()],'algorithm':'cbba','aircraft_ids':[]}),
            call('compare_allocations',{'plan_id':'','algorithms':['cbba','hungarian','auction']}),{'role':'assistant','content':'比较完成，尚未执行。'},
            call('get_plan',{'plan_id':''}),call('execute_plan',{'plan_id':''}),{'role':'assistant','content':'刚才的方案已排队。'},
            call('execute_plan',{'plan_id':''}),{'role':'assistant','content':'该方案已经提交，没有重复下发。'}])
        first=agent.chat({'message':'先对比一下方案','mode':'live','conversation_id':'multi-turn-case'})
        self.assertFalse(self.session.calls)
        self.assertTrue(all(x['status']=='success' for x in first['tool_calls']))
        second=agent.chat({'message':'就按刚才那个办','mode':'live','conversation_id':'multi-turn-case'})
        self.assertEqual(len(self.session.calls),1)
        agent.chat({'message':'再看看刚才提交结果','mode':'live','conversation_id':'multi-turn-case'})
        self.assertEqual(len(self.session.calls),1)
        resumed=self.requests[3]['messages']
        self.assertTrue(any(m['role']=='user' and m['content']=='先对比一下方案' for m in resumed))
        self.assertIn(first['plan']['id'],resumed[0]['content'])
        self.assertIn('latest_plan',resumed[0]['content'])
        self.assertFalse(any(m['role']=='tool' for m in resumed))
        self.assertNotIn('input_fleet',json.dumps(resumed,ensure_ascii=False))
        saved=json.loads((Path(self.tmp.name)/'runs/conversations/multi-turn-case.json').read_text())
        original=saved['turns'][0]
        self.assertTrue(any(m.get('tool_calls') for m in original if m['role']=='assistant'))
        self.assertTrue(any(m['role']=='tool' for m in original))

    def test_context_packing_reduces_model_payload_without_losing_raw_results(self):
        key='packed-context-history'
        directory=Path(self.tmp.name)/'runs/conversations';directory.mkdir(parents=True)
        old_payload={'ok':True,'result':{'aircraft':[{'id':'FW-01','diagnostic_padding':'OLD_TELEMETRY_ONLY'*300}],
                     'planning_fleet':[{'id':'FW-01','diagnostic_padding':'OLD_TELEMETRY_ONLY'*300}]}}
        history=[]
        for i in range(6):
            history.append([{'role':'user','content':'历史状态查询%d'%i},call('get_fleet_state',{}),
                {'role':'tool','tool_name':'get_fleet_state','content':json.dumps(old_payload,ensure_ascii=False)}])
        (directory/(key+'.json')).write_text(json.dumps({'id':key,'turns':history,'plans':{}},ensure_ascii=False))
        agent=self.agent([call('get_fleet_state',{}),{'role':'assistant','content':'已读取最新状态，没有执行飞控。'}])
        result=agent.chat({'message':'现在检查机群状态，保持任务不执行','mode':'live','conversation_id':key})
        sent=self.requests[1]['messages']
        self.assertNotIn('OLD_TELEMETRY_ONLY',json.dumps(sent,ensure_ascii=False))
        compact=json.loads(next(m['content'] for m in sent if m['role']=='tool'))
        raw=result['tool_calls'][0]['result']
        self.assertIn('planning_fleet',raw['result'])
        self.assertNotIn('planning_fleet',compact['result'])
        self.assertEqual(len(compact['result']['aircraft']),3)
        self.assertLess(len(json.dumps(compact)),len(json.dumps(raw)))
        for usage in result['model_usage']:
            packing=usage['packing']
            self.assertLessEqual(packing['estimated_input_tokens'],packing['input_budget_tokens'])
            self.assertEqual(packing['input_budget_tokens']+packing['reserved_generation_tokens']+packing['safety_tokens'],usage['num_ctx'])
            self.assertEqual(packing['reserved_generation_tokens'],4096)
        saved=json.loads((directory/(key+'.json')).read_text())
        latest_raw=json.loads(next(m['content'] for m in saved['turns'][-1] if m['role']=='tool'))
        self.assertEqual(latest_raw,raw)
        self.assertIn('OLD_TELEMETRY_ONLY',saved['turns'][0][-1]['content'])
        self.assertFalse(self.session.calls)

    def test_context_full_after_state_does_not_present_previous_plan_as_new(self):
        agent=self.agent([call('plan_inspection',{'targets':[target()]}),
            {'role':'assistant','content':'上一份方案已规划，未执行。'},call('get_fleet_state',{})])
        original=agent.request_model
        def model(payload):
            if len(self.requests)<3:return original(payload)
            self.requests.append(payload)
            return {'message':{'role':'assistant','content':''},'done_reason':'length',
                    'prompt_eval_count':16245,'eval_count':139}
        agent.request_model=model
        key='state-context-limit'
        old=agent.chat({'message':'先规划东1100米北50米','mode':'live','conversation_id':key})
        result=agent.chat({'message':'重新规划三个巡检点，先不要执行','mode':'live','conversation_id':key})
        self.assertEqual(result['stop_reason'],'context_limit')
        self.assertIn('上下文空间已用满',result['reply'])
        self.assertIn('本轮只取得状态信息，尚未生成新的巡检方案',result['reply'])
        self.assertFalse(result['plan_created_this_turn'])
        self.assertEqual(result['plan_origin'],'previous_turn')
        self.assertEqual(result['plan']['id'],old['plan']['id'])
        self.assertIsNone(result['draft']);self.assertIsNone(result['execution'])
        usage=result['model_usage'][-1]
        self.assertEqual((usage['prompt_eval_count'],usage['eval_count'],usage['done_reason']),(16245,139,'length'))
        saved=json.loads((Path(self.tmp.name)/'runs/conversations'/(key+'.json')).read_text())
        self.assertEqual(saved['last_model_usage'],result['model_usage'])
        self.assertEqual(saved['last_turn_status']['stop_reason'],'context_limit')
        self.assertFalse(saved['last_turn_status']['plan_created'])
        self.assertEqual([m['tool_name'] for m in saved['turns'][-1] if m['role']=='tool'],['get_fleet_state'])
        self.assertFalse(self.session.calls)

    def test_budget_failure_after_takeoff_preserves_receipt_without_retry(self):
        agent=self.agent([call('takeoff_aircraft',{'aircraft_ids':['FW-01']})])
        packing_calls=[]
        def pack(*args,**kwargs):
            packing_calls.append(None)
            if len(packing_calls)==2:raise ContextBudgetError('injected context budget failure')
            return build_context_messages(*args,**kwargs)
        with patch('packages.ground_core.agent.build_context_messages',side_effect=pack):
            result=agent.chat({'message':'一号先上天等候','mode':'live','conversation_id':'write-budget-failure'})
        self.assertEqual(result['stop_reason'],'context_budget')
        self.assertEqual(result['source'],'controller')
        self.assertEqual(result['execution']['id'],'fixture-takeoff')
        self.assertIn('回执仍有效',result['reply'])
        self.assertEqual(len(self.requests),1)
        self.assertEqual(len(self.session.calls),1)
        self.assertEqual(len(packing_calls),2)
        saved=json.loads((Path(self.tmp.name)/'runs/conversations/write-budget-failure.json').read_text())
        tool_results=[json.loads(m['content']) for m in saved['turns'][-1] if m['role']=='tool']
        self.assertEqual(tool_results[0]['result']['id'],'fixture-takeoff')
        self.assertEqual(saved['last_selected_aircraft'],['FW-01'])

    def test_parameter_error_is_returned_to_model_for_repair(self):
        incomplete=target();incomplete.pop('x')
        agent=self.agent([call('plan_inspection',{'targets':[incomplete],'algorithm':'cbba','aircraft_ids':[]}),
            call('plan_inspection',{'targets':[target()],'algorithm':'cbba','aircraft_ids':[]}),{'role':'assistant','content':'已改为范围内的点，尚未执行。'}])
        result=agent.chat({'message':'讨论两个点','mode':'live'})
        self.assertEqual([x['status'] for x in result['tool_calls']],['error','success'])
        self.assertFalse(self.session.calls)

    def test_zone_requires_id_and_does_not_require_unused_coordinates(self):
        agent=self.agent([call('plan_inspection',{'targets':[{'frame':'zone','zone_id':'S'}]}),
                          {'role':'assistant','content':'区域方案已生成，尚未执行。'}])
        result=agent.chat({'message':'先看看S区方案','mode':'live'})
        self.assertEqual(len(result['plan']['tasks']),3)
        self.assertEqual(result['plan']['draft']['targets'][0]['x'],None)
        self.assertEqual(result['plan']['draft']['targets'][0]['label'],'S区')
        self.assertFalse(self.session.calls)

    def test_local_and_wgs84_require_both_coordinates(self):
        for frame in ('local','wgs84'):
            with self.subTest(frame=frame):
                agent=self.agent([call('plan_inspection',{'targets':[{'frame':frame,'x':1000}]}),
                                  {'role':'assistant','content':'缺少北向坐标或纬度，请补充。'}])
                result=agent.chat({'message':'先规划','mode':'live'})
                self.assertIn('数值参数：y',result['tool_calls'][0]['result']['error'])
                self.assertFalse(result['plan'])

    def test_recorded_zone_loop_stops_on_real_range_and_capacity_constraint(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/zone-tool-loop.json').read_text())
        env=environment(self.session)
        env['scene']=json.loads((Path(__file__).parents[1]/'config/scene.json').read_text())
        agent=self.agent(fixture['assistant_calls'],env)
        result=agent.chat({'message':fixture['user'],'mode':'live'})
        self.assertEqual(len(self.requests),1)
        self.assertEqual(result['stop_reason'],'task_constraint')
        self.assertEqual(result['last_tool_error']['code'],'out_of_bounds')
        self.assertEqual(result['last_tool_error']['details']['expanded_task_count'],9)
        self.assertIn('2.5 公里',result['reply']);self.assertIn('9 点',result['reply'])
        self.assertIn('没有下发',result['reply'])
        self.assertFalse(self.session.calls)

    def test_same_invalid_request_does_not_consume_ten_rounds(self):
        invalid=call('plan_inspection',{'targets':[{'frame':'local','y':50}]})
        agent=self.agent([invalid]*10)
        result=agent.chat({'message':'先规划东1100米北50米','mode':'live'})
        self.assertEqual(len(self.requests),2)
        self.assertEqual(result['stop_reason'],'repeated_error')
        self.assertIn('数值参数：x',result['reply'])
        self.assertTrue(result['tool_calls'][-1]['cached'])
        self.assertFalse(self.session.calls)

    def test_batch_does_not_execute_after_task_constraint_failure(self):
        message=call('plan_inspection',{'targets':[target(90000,0)]})
        message['tool_calls']+=call('takeoff_aircraft',{'aircraft_ids':[]})['tool_calls']
        agent=self.agent([message])
        result=agent.chat({'message':'查看远处任务方案','mode':'live'})
        self.assertEqual(result['tool_calls'][1]['result']['code'],'batch_stopped')
        self.assertFalse(self.session.calls)

    def test_duplicate_write_calls_in_one_model_message_are_not_resent(self):
        message=call('takeoff_aircraft',{'aircraft_ids':['FW-01']})
        message['tool_calls']*=2
        agent=self.agent([message,{'role':'assistant','content':'一号起飞任务已排队。'}])
        result=agent.chat({'message':'一号先起飞','mode':'live'})
        self.assertEqual(len(self.session.calls),1)
        self.assertTrue(result['tool_calls'][1]['cached'])

    def test_repeated_success_keeps_the_only_write_receipt(self):
        message=call('takeoff_aircraft',{'aircraft_ids':['FW-01']})
        agent=self.agent([message]*10)
        result=agent.chat({'message':'一号先起飞','mode':'live'})
        self.assertEqual(len(self.session.calls),1)
        self.assertEqual(result['stop_reason'],'no_progress')
        self.assertEqual(result['execution']['id'],'fixture-takeoff')
        self.assertIn('回执仍有效',result['reply'])

    def test_last_model_step_is_reserved_for_summary(self):
        responses=[call('plan_inspection',{'targets':[target(1000+i,50)]}) for i in range(9)]
        responses.append({'role':'assistant','content':'已完成多种方案规划，尚未执行。'})
        agent=self.agent(responses)
        result=agent.chat({'message':'先规划多个候选方案','mode':'live'})
        self.assertEqual(len(self.requests),10)
        self.assertEqual(self.requests[-1]['tools'],[])
        self.assertIsNone(result['stop_reason'])
        self.assertFalse(self.session.calls)

    def test_generation_length_after_write_returns_saved_receipt(self):
        agent=self.agent([call('takeoff_aircraft',{'aircraft_ids':['FW-01']})])
        original=agent.request_model
        def model(payload):
            if not self.requests:return original(payload)
            return {'message':{'role':'assistant','content':''},'done_reason':'length'}
        agent.request_model=model
        result=agent.chat({'message':'一号起飞','mode':'live'})
        self.assertEqual(result['stop_reason'],'model_output_limit')
        self.assertEqual(result['execution']['id'],'fixture-takeoff')
        self.assertEqual(len(self.session.calls),1)

    def test_recorded_execute_busy_then_length_preserves_specific_failure(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/execute-busy-output-limit.json').read_text())
        error=json.loads(fixture['pending_turn'][2]['content'])['error']
        responses=iter([
            {'message':call('plan_inspection',{'targets':[target()]})},
            {'message':{'role':'assistant','content':'方案已准备，尚未执行。'}},
            {'message':fixture['pending_turn'][1]},fixture['following_model_response']])
        def model(payload):
            self.requests.append(payload)
            return next(responses)
        agent=Agent(Path(self.tmp.name),model,'recorded-test-model',environment(self.session))
        key='recorded-busy-length'
        agent.chat({'message':'先规划东1100米北50米，暂不执行','mode':'live','conversation_id':key})
        attempts=[]
        def busy(*args,**kwargs):
            attempts.append((args,kwargs))
            raise RuntimeError(error)
        self.session.submit=busy
        self.session.report['status']='executing'
        result=agent.chat({'message':fixture['pending_turn'][0]['content'],'mode':'live','conversation_id':key})
        self.assertEqual(result['stop_reason'],'model_output_limit')
        self.assertEqual(result['last_tool_error']['error'],error)
        self.assertIn('模型本轮生成用完额度',result['reply'])
        self.assertIn('工具实际反馈：'+error,result['reply'])
        self.assertIn('实时遥测确认',result['reply'])
        self.assertNotIn('没有下发',result['reply'])
        self.assertNotIn('已成功',result['reply'])
        self.assertIsNone(result['execution'])
        self.assertEqual(len(attempts),1)
        self.assertFalse(self.session.calls)
        saved=json.loads((Path(self.tmp.name)/'runs/conversations'/(key+'.json')).read_text())
        turn=saved['turns'][-1]
        self.assertEqual(turn[0],fixture['pending_turn'][0])
        self.assertEqual(json.loads(next(m['content'] for m in turn if m['role']=='tool'))['error'],error)
        self.assertEqual(turn[-1]['content'],result['reply'])

    def test_previous_pending_busy_result_survives_conversation_recovery(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/execute-busy-output-limit.json').read_text())
        key='pending-busy-recovery'
        directory=Path(self.tmp.name)/'runs/conversations';directory.mkdir(parents=True)
        (directory/(key+'.json')).write_text(json.dumps({'id':key,'turns':[],'plans':{},
            'pending_turn':fixture['pending_turn']},ensure_ascii=False))
        agent=self.agent([{'role':'assistant','content':'上次执行请求被拒绝，已有任务正在执行。'}])
        agent.chat({'message':'上次执行返回了什么？','mode':'live','conversation_id':key})
        saved=json.loads((directory/(key+'.json')).read_text())
        self.assertEqual(saved['turns'][0],fixture['pending_turn'])
        self.assertEqual(len(saved['turns']),2)
        self.assertFalse(self.session.calls)

    def test_uncertain_write_error_never_claims_no_command_was_sent(self):
        def timeout(action,ids):
            self.session.calls.append((action,ids))
            raise TimeoutError('FW-01 COMMAND_ACK timeout')
        self.session.control=timeout
        self.session.fleet[0].update(armed=True,landed=False)
        message=call('control_aircraft',{'action':'hold','aircraft_ids':['FW-01']})
        agent=self.agent([message]*2)
        result=agent.chat({'message':'一号先等一等','mode':'live'})
        self.assertEqual(len(self.session.calls),1)
        self.assertEqual(result['stop_reason'],'repeated_error')
        self.assertIn('是否已执行需以实时遥测确认',result['reply'])
        self.assertNotIn('没有下发',result['reply'])

    def test_unknown_tool_cannot_invoke_code(self):
        agent=self.agent([call('run_shell',{'cmd':'echo bad'}),{'role':'assistant','content':'没有该工具。'}])
        result=agent.chat({'message':'测试未知接口','mode':'live'})
        self.assertEqual(result['tool_calls'][0]['status'],'error');self.assertFalse(self.session.calls)

    def test_model_can_choose_different_algorithms(self):
        for algorithm in ('cbba','hungarian','auction','greedy'):
            with self.subTest(algorithm=algorithm):
                agent=self.agent([call('plan_inspection',{'targets':[target()],'algorithm':algorithm,'aircraft_ids':[]}),{'role':'assistant','content':'已规划。'}])
                result=agent.chat({'message':'请规划','mode':'live'})
                self.assertEqual(result['plan']['allocation']['algorithm_id'],algorithm)


    def test_rectangular_auction_matches_minimum_cost_assignment(self):
        from packages.swarm_sim.algorithms import solve
        import random
        import math
        generator=random.Random(12)
        fleet=self.session.snapshot()['fleet']
        for count in (1,2,3):
            for _ in range(8):
                tasks=[dict(id=str(i),east_m=generator.uniform(100,1800),north_m=generator.uniform(-500,1200),required_capabilities=['rgb']) for i in range(count)]
                auction=solve(fleet,tasks,'auction');optimal=solve(fleet,tasks,'hungarian')
                lookup={t['id']:t for t in tasks}
                def cost(plan):
                    return sum(math.hypot(lookup[tid]['east_m']-a['east_m'],lookup[tid]['north_m']-a['north_m'])/(a['speed_mps']*(.5+.5*a['battery_pct']/100)) for a in fleet for tid in plan['assignments'][a['id']])
                self.assertLessEqual(abs(cost(auction)-cost(optimal)),.04)

if __name__=='__main__':unittest.main()
