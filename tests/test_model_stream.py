import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from agent_fixture import FixtureSession,environment
from packages.ground_core.agent import Agent
from packages.ground_core.model_stream import collect


def frame(content='',thinking='',calls=None,done=False,reason='stop'):
    value={'message':{'role':'assistant','content':content,'thinking':thinking},'done':done}
    if calls:value['message']['tool_calls']=calls
    if done:value.update(done_reason=reason,prompt_eval_count=1000,eval_count=20)
    return value


class ModelStreamTests(unittest.TestCase):
    def test_content_and_reasoning_collected_separately_without_exposing_reasoning(self):
        events=[];r=collect([frame(thinking='private external reasoning'),frame(content='当前'),frame(content='状态。'),frame(done=True)],events.append)
        self.assertEqual(r['message']['content'],'当前状态。')
        self.assertEqual(r['message']['thinking'],'private external reasoning')
        self.assertNotIn('private external reasoning',json.dumps(events))
        self.assertEqual(r['eval_count'],20)
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='text_delta'),'当前状态。')

    def test_thinking_budget_ends_before_exhausting_output(self):
        times=iter([0,1,20]);r=collect([frame(thinking='a'),frame(thinking='b')],clock=lambda:next(times),thinking_seconds=18)
        self.assertEqual(r['done_reason'],'thinking_timeout');self.assertFalse(r['done'])

    def test_partial_output_on_disconnect_is_retained_but_calls_are_not_executed(self):
        calls=[{'function':{'index':0,'name':'takeoff_aircraft','arguments':{'aircraft_ids':['FW-01']}}}]
        def chunks():
            yield frame(content='先说明当前状态',calls=calls)
            raise OSError('connection lost')
        response=collect(chunks());session=FixtureSession();events=[]
        with tempfile.TemporaryDirectory() as root:
            agent=Agent(root,None,'test',environment(session),request_stream=lambda payload,emit:response)
            result=agent.chat({'message':'讨论操作原理','mode':'live'},events.append)
            self.assertEqual(result['stop_reason'],'stream_interrupted')
            self.assertEqual(result['partial_reply'],'先说明当前状态')
            self.assertFalse(session.calls)
            self.assertEqual(events[-1]['type'],'result')

    def test_truncated_complete_looking_tool_call_is_not_executed(self):
        calls=[{'function':{'name':'takeoff_aircraft','arguments':{'aircraft_ids':[]}}}]
        response=collect([frame(content='尚在生成',calls=calls),frame(done=True,reason='length')]);session=FixtureSession()
        with tempfile.TemporaryDirectory() as root:
            result=Agent(root,None,'test',environment(session),request_stream=lambda payload,emit:response).chat({'message':'讨论','mode':'live'})
            self.assertEqual(result['stop_reason'],'model_output_limit');self.assertFalse(session.calls)
            self.assertEqual(result['partial_reply'],'尚在生成');self.assertFalse(result['reply_complete'])

    def test_accepted_write_receipt_survives_next_model_timeout(self):
        session=FixtureSession();steps=iter([
            collect([frame(calls=[{'function':{'name':'takeoff_aircraft','arguments':{'aircraft_ids':['FW-01']}}}],done=True)]),
            {'message':{'content':'已取得回执，正在解释'},'done_reason':'thinking_timeout'}])
        with tempfile.TemporaryDirectory() as root:
            result=Agent(root,None,'test',environment(session),request_stream=lambda payload,emit:next(steps)).chat({'message':'一号起飞','mode':'live'})
            self.assertEqual(len(session.calls),1)
            self.assertEqual(result['execution']['id'],'fixture-takeoff')
            self.assertIn('回执仍有效',result['reply'])

    def test_missing_done_marker_cannot_execute_a_tool(self):
        r=collect([frame(calls=[{'function':{'name':'get_fleet_state','arguments':{}}}])])
        self.assertEqual(r['done_reason'],'stream_interrupted')

    def test_total_generation_deadline_keeps_visible_text(self):
        times=iter([0,1,61]);r=collect([frame(content='正文'),frame(content='后续')],clock=lambda:next(times))
        self.assertEqual(r['done_reason'],'generation_timeout');self.assertEqual(r['message']['content'],'正文')


spec=importlib.util.spec_from_file_location('stream_ground_app',Path(__file__).resolve().parents[1]/'server/app.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)


class HttpStreamTests(unittest.TestCase):
    def test_http_flushes_content_before_final_result_and_releases_model_lock(self):
        gate=threading.Event();requests=[]
        def model(payload,emit):
            requests.append(copy.deepcopy(payload));emit({'type':'text_delta','text':'已取得状态'})
            gate.wait(3)
            return {'message':{'content':'已取得状态，没有执行动作。'},'done_reason':'stop'}
        with tempfile.TemporaryDirectory() as root:
            agent=Agent(root,None,'test',environment(FixtureSession()),request_stream=model)
            server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch.object(app,'get_agent',return_value=agent):
                    req=urllib.request.Request('http://127.0.0.1:%d/api/chat/stream'%server.server_port,json.dumps({'message':'查状态','mode':'live'}).encode(),{'Content-Type':'application/json'})
                    h=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with h.open(req,timeout=5) as response:
                        self.assertIn('application/x-ndjson',response.headers['Content-Type'])
                        seen=[]
                        while True:
                            event=json.loads(response.readline());seen.append(event)
                            if event['type']=='text_delta':break
                        self.assertFalse(gate.is_set())
                        self.assertFalse(any(e['type']=='result' for e in seen))
                        gate.set();seen.extend(json.loads(line) for line in response if line.strip())
                    result=next(e['result'] for e in seen if e['type']=='result')
                    self.assertIn('没有执行动作',result['reply']);self.assertFalse(app.MODEL_LOCK.locked())
            finally:gate.set();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
