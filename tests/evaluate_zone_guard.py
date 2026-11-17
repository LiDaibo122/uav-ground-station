"""Real local Qwen regression for the reported region loop; dry-run actuators."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from packages.ground_core.agent import Agent
from agent_fixture import FixtureSession,environment


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',default='qwen3.5:4b');args=parser.parse_args()
    http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def model(payload):
        req=urllib.request.Request('http://127.0.0.1:11434/api/chat',json.dumps(payload,ensure_ascii=False).encode(),{'Content-Type':'application/json'})
        with http.open(req,timeout=180) as response:return json.load(response)
    session=FixtureSession();env=environment(session);env['scene']=json.loads((ROOT/'config/scene.json').read_text())
    rows=[]
    with tempfile.TemporaryDirectory() as directory:
        agent=Agent(directory,model,args.model,env)
        for i,text in enumerate(['起飞3架无人机，分别去执行A\\B\\S区','先只规划S区，不要执行。']):
            before=len(session.calls)
            result=agent.chat({'message':text,'mode':'live','conversation_id':'region-loop-regression'})
            writes=session.calls[before:]
            if i==0:passed=result['stop_reason']=='task_constraint' and result['last_tool_error']['code']=='out_of_bounds' and not writes and result['agent_steps']<10
            else:passed=bool(result['plan'] and len(result['plan']['tasks'])==3 and not writes)
            rows.append({'user':text,'passed':passed,'actuator_calls':writes,'response':result})
            report={'model':args.model,'thinking_enabled':True,'model_calls':'actual local Ollama native tools',
                'flight_actuation':'dry-run fixture only','cases':rows,'passed':sum(row['passed'] for row in rows),'total':len(rows)}
            (ROOT/'reports/zone-tool-loop-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps({'passed':passed,'tools':[t['tool'] for t in result['tool_calls']],
                'agent_steps':result['agent_steps'],'reply':result['reply']},ensure_ascii=False),flush=True)
    return 0 if all(row['passed'] for row in rows) else 1


if __name__=='__main__':raise SystemExit(main())
