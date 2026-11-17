"""Replay the reported three-point request with real Qwen and dry-run actuators."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from packages.ground_core.agent import Agent
from agent_fixture import FixtureSession,environment

MESSAGE='''先帮我规划，不要执行。
以仿真原点为基准：
巡检点1：东1100米、北50米，常规优先级。
巡检点2：东1350米、北400米，高优先级。
巡检点3：东1650米、北750米，常规优先级。
根据实时位置、电量和可用状态，用CBBA分配飞机，不指定具体飞机。'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',default='qwen3.5:4b')
    parser.add_argument('--conversation',type=Path,default=ROOT/'runs/conversations/dcb2bdf2-9dee-4938-a2fc-53e414a1d322.json')
    args=parser.parse_args();saved=json.loads(args.conversation.read_text())
    index=next(i for i in reversed(range(len(saved['turns'])))
               if any(m.get('role')=='user' and m.get('content')==MESSAGE for m in saved['turns'][i]))
    saved['turns']=saved['turns'][:index];saved.pop('pending_turn',None)
    http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def model(payload):
        req=urllib.request.Request('http://127.0.0.1:11434/api/chat',json.dumps(payload,ensure_ascii=False).encode(),{'Content-Type':'application/json'})
        with http.open(req,timeout=180) as response:return json.load(response)
    rows=[]
    for kind in ('fresh','recorded_history'):
        session=FixtureSession();env=environment(session);env['scene']=json.loads((ROOT/'config/scene.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            key='context-replay-'+kind
            if kind=='recorded_history':
                conversation=copy.deepcopy(saved);conversation['id']=key
                path=Path(directory)/'runs/conversations';path.mkdir(parents=True)
                (path/(key+'.json')).write_text(json.dumps(conversation,ensure_ascii=False))
            agent=Agent(directory,model,args.model,env)
            result=agent.chat({'message':MESSAGE,'mode':'live','conversation_id':key})
            plan=result.get('plan') or {}
            targets=plan.get('tasks',[])
            actual=[u['prompt_eval_count'] for u in result['model_usage'] if isinstance(u.get('prompt_eval_count'),int)]
            correct=bool(result['plan_created_this_turn'] and plan.get('algorithm')=='cbba'
                         and [(t['east_m'],t['north_m'],t['priority']) for t in targets]==[(1100,50,'normal'),(1350,400,'high'),(1650,750,'normal')]
                         and not session.calls and not result['stop_reason'])
            reserved=bool(actual and all(n<=16384-4096-1024 for n in actual))
            row={'case':kind,'passed':correct and reserved,'plan_correct':correct,
                 'generation_and_safety_space_preserved':reserved,'max_actual_prompt_tokens':max(actual,default=None),
                 'source_history_characters':len(json.dumps(saved['turns'],ensure_ascii=False)) if kind=='recorded_history' else 0,
                 'actuator_calls':session.calls,'response':result}
            rows.append(row)
            report={'model':args.model,'thinking_enabled':True,'num_ctx':16384,'reserved_generation_tokens':4096,
                    'model_calls':'actual local Ollama native tools','flight_actuation':'dry-run fixture only',
                    'reported_failure':{'prompt_tokens':16245,'generated_tokens':139,'context_tokens':16384},
                    'passed':sum(r['passed'] for r in rows),'total':len(rows),'cases':rows}
            (ROOT/'reports/context-replay-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps({k:v for k,v in row.items() if k!='response'},ensure_ascii=False),flush=True)
            print(result['reply'],flush=True)
    return 0 if all(r['passed'] for r in rows) else 1


if __name__=='__main__':raise SystemExit(main())
