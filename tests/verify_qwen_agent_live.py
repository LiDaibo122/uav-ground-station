"""Opt-in real Qwen -> actual owned PX4 SITL verification; moves one aircraft."""
import json
from pathlib import Path
import time
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
HTTP=urllib.request.build_opener(urllib.request.ProxyHandler({}))
REPORT=ROOT/'reports/qwen-agent-live.json'


def api(path,data=None):
    req=urllib.request.Request('http://127.0.0.1:11843'+path,
        None if data is None else json.dumps(data,ensure_ascii=False).encode(),{'Content-Type':'application/json'})
    with HTTP.open(req,timeout=180) as response:return json.load(response)


def main():
    initial=api('/api/simulation')
    if not initial['ready'] or initial['report']['status']!='waiting_for_task':
        raise RuntimeError('Requires idle, owned SITL; preserving current task')
    conversation='live-native-'+uuid4().hex[:12]
    report={'model':api('/api/health')['model']['model'],'session_id':initial['id'],
        'backend':'actual local Qwen, actual algorithms, actual PX4/Gazebo','turns':[],'checks':[],'semantic_issues':[],'passed':False}
    def save():REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    def chat(text):
        result=api('/api/chat',{'message':text,'mode':'live','conversation_id':conversation})
        report['turns'].append({'user':text,'response':result});save()
        print(json.dumps({'user':text,'tools':[(t['tool'],t['status']) for t in result['tool_calls']],'reply':result['reply']},ensure_ascii=False),flush=True)
        return result
    def wait_for(label,predicate,timeout=130):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            state=api('/api/state?mode=live');sim=api('/api/simulation')
            if sim['id']!=initial['id']:raise RuntimeError('SITL session changed')
            if sim['report'].get('error'):raise RuntimeError(sim['report']['error'])
            if predicate(state,sim):
                report['checks'].append({'name':label,'state':state,'simulation':sim});save();print('PASS',label,flush=True);return
            time.sleep(1)
        raise TimeoutError(label)
    chat('现在谁还在地上，谁已经离地了？')
    result=chat('先让一号上天转一会儿，另外两架先别动。')
    assert any(t['tool']=='takeoff_aircraft' and t['arguments']['aircraft_ids']==['FW-01'] and t['status']=='success' for t in result['tool_calls'])
    wait_for('Only FW-01 airborne above 80 m; other two remain unarmed on ground',lambda state,sim:
        all(a['armed'] and a['landed'] is False and a['relative_alt_m']>80 if a['id']=='FW-01' else not a['armed'] and a['landed'] is True for a in state['aircraft']) and len(state['aircraft'])==3)
    chat('刚才那架先在附近绕着，别继续跑任务。')
    wait_for('FW-01 actual AUTO.LOITER',lambda state,sim:next(a for a in state['aircraft'] if a['id']=='FW-01')['flight_mode']=='AUTO.LOITER',20)
    result=chat('给刚才那架安排一个东一千一百米北五十米的常规巡检点，另外两架先留着。先只看方案，再比较基础拍卖和匈牙利。')
    assert result['plan'] and all(t['east_m']==1100 and t['north_m']==50 for t in result['plan']['tasks'])
    assert 'compare_allocations' in [t['tool'] for t in result['tool_calls']]
    comparisons=[t for t in result['tool_calls'] if t['tool']=='compare_allocations' and t['status']=='success']
    if not any({'auction','hungarian'}.issubset(t['arguments']['algorithms']) for t in comparisons):
        report['semantic_issues'].append('Requested auction + hungarian; model compared a different algorithm set.')
        save()
    assert {uid for uid,route in result['plan']['allocation']['assignments'].items() if route}=={'FW-01'}
    chat('就按这个安排，让它过去检查。')
    wait_for('New inspection mission accepted and flown to target',lambda state,sim:sim['report']['operation']=='inspection' and sim['report']['success'],160)
    chat('让刚才那架回去，其他的先别动。')
    wait_for('FW-01 actual AUTO.RTL; other two still unarmed',lambda state,sim:
        all(a['flight_mode']=='AUTO.RTL' if a['id']=='FW-01' else not a['armed'] and a['landed'] is True for a in state['aircraft']),20)
    report['flight_checks_passed']=True;report['passed']=not report['semantic_issues']
    report['note']='RTL command ACK and actual mode verified; automatic landing not asserted. Flight checks and semantic issues are reported separately.';save()
    print('PASS native Qwen / algorithms / PX4 flight verification',flush=True)


if __name__=='__main__':main()
