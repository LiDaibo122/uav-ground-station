"""Real local Qwen streaming, real solvers, isolated flight actuator fixture."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent_fixture import FixtureSession,environment
from packages.ground_core.agent import Agent
from packages.ground_core.model_stream import OllamaStream


def main():
    saved=json.loads((ROOT/'runs/conversations/5d40ae60-ba62-49c1-8fa2-e39300fa6c22.json').read_text())
    observed=json.loads(next(m['content'] for m in saved['turns'][-1] if m['role']=='tool'))['result']
    rows=[]
    for case,text in [('reported_composite','全部往北飞100m，绕行一周返航'),
                      ('three_points','先只规划不要执行：东1100米北50米常规，东1350米北400米高优先级，东1650米北750米常规，用CBBA自动分配飞机。')]:
        session=FixtureSession();env=environment(session)
        with tempfile.TemporaryDirectory() as directory:
            key='stream-replay-'+case
            if case=='reported_composite':
                session.fleet=copy.deepcopy(observed['planning_fleet']);session.report['status']=observed['mission_status']
                env['live_state']=lambda:dict(mode='live',px4_connected=True,aircraft=copy.deepcopy(observed['aircraft']))
                old=copy.deepcopy(saved);old['id']=key;old['turns']=old['turns'][:-1]
                path=Path(directory)/'runs/conversations';path.mkdir(parents=True);(path/(key+'.json')).write_text(json.dumps(old,ensure_ascii=False))
            began=time.monotonic();events=[]
            def event(value):
                row={'at_s':round(time.monotonic()-began,3),'type':value['type']}
                if value['type']=='model_progress':row.update(phase=value['phase'],thinking_characters=value['thinking_characters'],answer_characters=value['answer_characters'])
                if value['type']=='text_delta':row['characters']=len(value['text'])
                if value['type']=='tool_result':row['tool']=value['call']['tool']
                events.append(row)
            agent=Agent(directory,None,'qwen3.5:4b',env,request_stream=OllamaStream('http://127.0.0.1:11434/api/chat'))
            result=agent.chat({'message':text,'mode':'live','conversation_id':key},event)
            passed=not result['stop_reason'] and not session.calls
            if case=='three_points':passed=passed and result['plan_created_this_turn'] and len(result['plan']['tasks'])==3
            rows.append({'case':case,'passed':passed,'actuator_calls':session.calls,'events':events,
                'first_progress_s':next((e['at_s'] for e in events if e['type']=='model_progress'),None),
                'first_text_s':next((e['at_s'] for e in events if e['type']=='text_delta'),None),'result':result})
            (ROOT/'reports/model-stream-verification.json').write_text(json.dumps({'scope':'real Qwen and solvers, dry-run flight actuators','passed':sum(r['passed'] for r in rows),'total':len(rows),'cases':rows},ensure_ascii=False,indent=2))
            print(json.dumps({k:v for k,v in rows[-1].items() if k not in ('events','result')},ensure_ascii=False),flush=True)
            print(result['reply'],flush=True)
    return 0 if all(row['passed'] for row in rows) else 1


if __name__=='__main__':raise SystemExit(main())
