"""Grounded Qwen task requests -> explicit inspection coordinates."""
import math
import re
from uuid import uuid4

TARGET_SCHEMA = {'type':'object','properties':{
    'label':{'type':'string'},
    'frame':{'type':'string','enum':['local','wgs84','zone']},
    'x':{'type':['number','null']},'y':{'type':['number','null']},
    'zone_id':{'type':'string'},
    'priority':{'type':'string','enum':['low','normal','high']},
    'sensor':{'type':'string','enum':['rgb','thermal']}
},'required':['label','frame','x','y','zone_id','priority','sensor'],'additionalProperties':False}


class TaskConstraintError(ValueError):
    """A task cannot be repaired by silently changing the user's requested area."""
    retryable=False

    def __init__(self,message,code,details):
        super().__init__(message)
        self.code=code
        self.details=details


def local_xy(lon, lat, origin):
    return (math.radians(lon-origin['lon'])*6371000*math.cos(math.radians(origin['lat'])),
            math.radians(lat-origin['lat'])*6371000)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value):
        raise ValueError('任务点必须包含有效数值坐标')
    return float(value)


def validate_targets(targets):
    if not isinstance(targets, list) or len(targets)>6:
        raise ValueError('本轮仿真支持 1—6 个目标')
    result=[]
    for target in targets:
        if not isinstance(target,dict) or target.get('frame') not in ('local','wgs84','zone'):
            raise ValueError('任务点坐标类型无效')
        if target.get('priority') not in ('low','normal','high') or target.get('sensor') not in ('rgb','thermal'):
            raise ValueError('任务点优先级或传感器类型无效')
        label=target.get('label','')
        if not isinstance(label,str) or not 1<=len(label)<=100:
            raise ValueError('请填写有效的任务点名称')
        item={k:target.get(k) for k in TARGET_SCHEMA['required']}
        if item['frame']!='zone':
            item['x'],item['y']=number(item['x']),number(item['y'])
        elif not isinstance(item['zone_id'],str) or not item['zone_id']:
            raise ValueError('区域任务需要明确地图区域编号')
        result.append(item)
    return result


def explicit_local_pairs(text):
    scalar=r'([-+]?\d+(?:\.\d+)?)'
    return [(float(x),float(y)) for x,y in re.findall(
        r'东\s*'+scalar+r'\s*(?:米|m)?\s*[,，、/\s]*北\s*'+scalar,text or '')]


def ground_targets(targets, instruction_text=None):
    """Resolve model frame labels using coordinate pairs explicitly given by the user."""
    parsed=validate_targets(targets)
    pairs=explicit_local_pairs(instruction_text)
    for target in parsed:
        if target['frame']!='zone':
            continue
        if target['x'] is not None and target['y'] is not None:
            xy=(number(target['x']),number(target['y']))
            if xy in pairs:
                target.update(frame='local',x=xy[0],y=xy[1],zone_id='')
                continue
            raise ValueError('模型同时给出了区域和点坐标，无法确定任务位置；本次未下发')
        if pairs:
            raise ValueError('你指定了明确坐标，但模型返回了区域任务；本次未下发，请重试')
    return parsed


def normalize_targets(draft, origin, zones, source_text=None, instruction_text=None):
    targets=ground_targets(draft.get('targets',[]),instruction_text if instruction_text is not None else source_text)
    if not targets:
        raise ValueError('任务缺少坐标或地图区域，请在对话中给出任务点')
    tasks=[];outside=[]
    request_id=uuid4().hex[:8]
    numbers=set(float(v) for v in re.findall(r'[-+]?\d+(?:\.\d+)?',source_text or ''))
    local_pairs=explicit_local_pairs(source_text)
    for target in targets:
        if target['frame']=='zone':
            zone=next((z for z in zones if z['id']==target['zone_id']),None)
            if zone is None:
                raise ValueError('任务引用未知地图区域')
            if source_text is not None and not (target['zone_id'] in source_text or zone['name'].split('·')[0].strip() in source_text or (zone['id']=='S' and '仿真' in source_text)):
                raise ValueError('区域没有来自你的指令，请明确指定地图区域')
            xy=[local_xy(lon,lat,origin) for lon,lat in zone['coordinates']]
            cx,cy=sum(p[0] for p in xy)/len(xy),sum(p[1] for p in xy)/len(xy)
            # Three deterministic interior samples, not a full area-coverage claim.
            points=[((cx+x)/2,(cy+y)/2) for x,y in [xy[0],(cx,cy),xy[len(xy)//2]]]
        else:
            x,y=target['x'],target['y']
            if source_text is not None and (x not in numbers or y not in numbers):
                raise ValueError('模型给出的坐标未出现在你的指令中，请明确提供坐标或地图区域')
            if source_text is not None and target['frame']=='local' and local_pairs and (x,y) not in local_pairs:
                raise ValueError('模型组合的东/北坐标与原指令不符，请重新说明任务点')
            if target['frame']=='wgs84':
                if not -180<=x<=180 or not -85<=y<=85:
                    raise ValueError('经纬度超出范围')
                points=[local_xy(x,y,origin)]
            else:
                points=[(x,y)]
        for x,y in points:
            if math.hypot(x,y)>2500:
                outside.append({'label':target['label'],'zone_id':target.get('zone_id'),
                                'east_m':round(x,2),'north_m':round(y,2),
                                'distance_m':round(math.hypot(x,y),2)})
            tasks.append({'id':'QWEN-%s-%02d'%(request_id,len(tasks)+1),
                          'label':target['label'], 'east_m':x,'north_m':y,
                          'priority':target['priority'],'required_capabilities':[target['sensor']],
                          'allowed_aircraft_ids':draft.get('aircraft_ids',[]),
                          'source':'qwen_user_request','request_target':target})
    if outside:
        labels='、'.join(dict.fromkeys(p['label'] for p in outside))
        capacity=' 区域展开共 %d 点，还超过单轮 6 点上限；范围调整后也需要分批。'%len(tasks) if len(tasks)>6 else ''
        raise TaskConstraintError('%s 的任务点超出本轮平地仿真 2.5 公里范围，请调整目标位置。%s 该方案未下发，也不会自动替换为 S 区。'%(labels,capacity),
            'out_of_bounds',{'max_radius_m':2500,'out_of_bounds':outside,'expanded_task_count':len(tasks),'max_tasks':6})
    if len(tasks)>6:
        raise TaskConstraintError('区域展开后共 %d 个巡检点，超过本轮上限 6 点，请分批下发；该方案未下发'%len(tasks),
            'task_capacity_exceeded',{'expanded_task_count':len(tasks),'max_tasks':6})
    if len({(round(t['east_m'],2),round(t['north_m'],2)) for t in tasks})!=len(tasks):
        raise ValueError('任务点坐标重复，请检查指令')
    return tasks
