"""Telemetry-grounded status facts for the model tools."""

def status_reply(state, report=None):
    if not state.get('aircraft'):
        return '尚未收到无人机实时遥测，无法确认解锁或起飞状态。'
    lines=[]
    for a in state['aircraft']:
        if a.get('stale'):
            phase='遥测已过期，无法确认当前飞行状态'
        elif a.get('landed') is False:
            phase='已离地飞行' if a.get('armed') is True else '已离地，当前未解锁' if a.get('armed') is False else '已离地，解锁状态未知'
        elif a.get('armed') is False:
            phase='未解锁，尚未起飞' if a.get('landed') is True else '未解锁，离地状态未上报'
        elif a.get('landed') is True:
            phase='已解锁但仍在地面' if a.get('armed') else '仍在地面'
        elif a.get('armed') is True and (a.get('relative_alt_m') or 0)>3:
            phase='已解锁、高度已上升，离地状态待飞控确认'
        else:
            phase='解锁/离地状态未上报，不能仅凭模式确认起飞'
        height=a.get('relative_alt_m')
        speed=a.get('speed_mps')
        battery=a.get('battery_pct')
        details=['模式 '+a.get('flight_mode','未知')]
        if a.get('longitude') is not None and a.get('latitude') is not None:
            details.append('位置 %.6f, %.6f'%(a['longitude'],a['latitude']))
        if height is not None:
            details.append('相对高度 %.1f 米'%height)
        if speed is not None:
            details.append('地速 %.1f 米/秒'%speed)
        details.append('电量 %s'%('未知' if battery is None else '%d%%'%battery))
        if report and not report.get('session_stopped_at'):
            route=report.get('allocation',{}).get('assignments',{}).get(a['id'],[])
            if route:
                labels=[next((t['label'] for t in report.get('tasks',[]) if t['id']==tid),tid) for tid in route]
                visited=report.get('aircraft',{}).get(a['id'],{}).get('visited',{})
                details.append('任务 '+ '、'.join(labels))
                details.append('已完成 %d/%d'%(len(visited),len(route)))
            else:
                details.append('当前无分配任务')
        lines.append('%s：%s；%s。'%(a['id'],phase,'，'.join(details)))
    if report and report.get('error'):
        lines.append('执行反馈：'+report['error'])
    lines.append('以上为 PX4 仿真遥测；固定翼通过持续前飞盘旋等待，不能原地悬停。')
    return '\n'.join(lines)
