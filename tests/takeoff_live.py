"""Explicit localhost SITL flight check; not included in unittest discovery."""
import json
from pathlib import Path
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
HTTP=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path,data=None):
    payload=None if data is None else json.dumps(data,ensure_ascii=False).encode()
    request=urllib.request.Request('http://127.0.0.1:11843'+path,payload,{'Content-Type':'application/json'})
    with HTTP.open(request,timeout=20) as response:
        return json.load(response)


def main():
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            session=api('/api/simulation')
        except OSError:
            time.sleep(.5)
            continue
        if session['ready'] and len(session['fleet'])==3 and all(a['available'] and a.get('landed') is True for a in session['fleet']):
            break
        time.sleep(.5)
    else:
        raise RuntimeError('SITL aircraft not ready on the ground')
    initial=api('/api/state?mode=live')
    assert all(a['armed'] is False and a['landed'] is True for a in initial['aircraft'])
    before=api('/api/chat',{'message':'汇报无人机状态','mode':'live'})
    assert '未解锁，尚未起飞' in before['reply']
    accepted=api('/api/chat',{'message':'无人机起飞','mode':'live'})
    assert accepted['action']=='takeoff' and accepted['execution']['status']=='queued'
    assert not accepted['dispatched']
    execution=accepted['execution']['id']
    print('TAKEOFF submitted',execution,flush=True)
    report={'initial_state':initial,'initial_status_reply':before,'takeoff_reply':accepted,'success':False}
    began=time.monotonic()
    last_print=0
    try:
        while time.monotonic()-began<240:
            current=api('/api/simulation')['report']
            live=api('/api/state?mode=live')
            assert current['execution_id']==execution
            if current['status'] in ('error','sensor_error','timeout'):
                raise RuntimeError(current.get('error',current['status']))
            airborne=[a for a in live['aircraft'] if not a['stale'] and a['armed'] is True and a['landed'] is False and a['relative_alt_m']>30 and a['speed_mps']>10]
            if len(airborne)==3 and 'first_all_airborne' not in report:
                report['first_all_airborne']={'elapsed_s':time.monotonic()-began,'aircraft':airborne}
                print('PASS all 3 armed and physically airborne',flush=True)
            if time.monotonic()-last_print>15:
                print('FLIGHT',current['status'],[(a['id'],a.get('armed'),a.get('landed'),round(a.get('relative_alt_m') or 0,1)) for a in live['aircraft']],flush=True)
                last_print=time.monotonic()
            if current['status']=='completed' and 'first_all_airborne' in report:
                after=api('/api/chat',{'message':'汇报无人机状态','mode':'live'})
                assert '已离地飞行' in after['reply']
                assert '不能原地悬停' in after['reply']
                report.update(success=True,flight=current,final_status_reply=after)
                print('PASS takeoff and holding waypoints confirmed by PX4',flush=True)
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Takeoff verification timed out')
    finally:
        if not report['success']:
            report['last_state']=api('/api/simulation')
        (ROOT/'reports/takeoff-command-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
