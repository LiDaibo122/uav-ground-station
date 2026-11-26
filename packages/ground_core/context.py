"""Bound model context while retaining exact user requests and domain anchors.

The stored conversation and UI trace remain untouched.  This module only builds
the prompt sent to the model.  Estimates are conservative heuristics, never a
claim to have run the model's tokenizer.
"""
import copy
import json
import math


READ_TOOLS=('get_fleet_state','get_mission_status','xd_state','xd_allocation_status')
WRITE_TOOLS=('execute_plan','takeoff_aircraft','control_aircraft')


def dumps(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def estimate_tokens(messages,tools=()):
    """Budget heuristic: 0.5 per ASCII char, 1.7 per non-ASCII char, plus framing.

    Chinese text, JSON punctuation and identifiers tend to tokenize differently;
    use a deliberately generous estimate and keep a separate safety allowance.
    Production counters returned by Ollama are the actual measurement.
    """
    text=dumps({'messages':messages,'tools':tools})
    ascii_count=sum(ord(char)<128 for char in text)
    framing=24*len(messages)+48*len(tools)+128
    return int(math.ceil(ascii_count*.5+(len(text)-ascii_count)*1.7+framing))


def pick(value,keys):
    return {key:copy.deepcopy(value[key]) for key in keys if key in value}


def compact_allocation(value):
    return pick(value,('algorithm_id','algorithm','assignments','unassigned','capacity','metrics','scope'))


def compact_plan(value):
    draft=value.get('draft',{})
    result=pick(value,('id','created_at','algorithm','executed','mode','available_next_tools'))
    result['targets']=copy.deepcopy(draft.get('targets',value.get('targets',[])))
    result['aircraft_ids']=copy.deepcopy(draft.get('aircraft_ids',value.get('aircraft_ids',[])))
    if 'tasks' in value:
        result['task_labels']={task['id']:task.get('label',task['id']) for task in value['tasks']}
    if 'allocation' in value:result['allocation']=compact_allocation(value['allocation'])
    if value.get('execution'):
        result['execution']=pick(value['execution'],('id','execution_id','status','session_id'))
    return result


def compact_fleet(value):
    planning={item['id']:item for item in value.get('planning_fleet',[]) if item.get('id')}
    actual={item['id']:item for item in value.get('aircraft',value.get('current_aircraft',[])) if item.get('id')}
    rows=[]
    fields=('id','longitude','latitude','east_m','north_m','relative_alt_m','altitude_m',
            'altitude_reference','heading_deg','battery_pct','battery_telemetry_age_s',
            'armed','landed','flight_mode','available','stale','capabilities','capability_source',
            'measured_ground_speed_mps','task')
    for uid in sorted(set(planning)|set(actual)):
        # Actual state fields win; planning adds capabilities and local position.
        merged=dict(planning.get(uid,{}));merged.update(actual.get(uid,{}))
        row=pick(merged,fields)
        if 'speed_mps' in actual.get(uid,{}):row['speed_mps']=actual[uid]['speed_mps']
        if 'speed_mps' in planning.get(uid,{}):row['planning_speed_mps']=planning[uid]['speed_mps']
        age=actual.get(uid,{}).get('age_s',planning.get(uid,{}).get('telemetry_age_s'))
        if age is not None:row['age_s']=age
        rows.append(row)
    result=pick(value,('observed_at','simulation','mission_status'))
    result['aircraft']=rows
    observations=[]
    for uid,observation in value.get('sensor_text_observations',{}).items():
        if not isinstance(observation,dict):
            observations.append({'aircraft_id':uid,'text':copy.deepcopy(observation)})
            continue
        detections=observation.get('disaster_detections',[])
        if observation.get('kind')=='flight_state' and not detections:
            continue  # Already represented by the single structured fleet snapshot.
        item=pick(observation,('aircraft_id','kind','observed_at','source','text','disaster_detections'))
        item.setdefault('aircraft_id',uid);observations.append(item)
    if observations:result['sensor_observations']=observations
    result['interpretation']='一次实时快照；AUTO.LOITER不证明离地；离地/解锁以字段为准。'
    return result


def compact_tool_result(name,payload,historical=False):
    """Return a model-facing copy; do not mutate persisted or frontend results."""
    if not isinstance(payload,dict):return copy.deepcopy(payload)
    if not payload.get('ok'):
        return pick(payload,('ok','error','code','retryable','details'))
    value=payload.get('result')
    if not isinstance(value,dict):return copy.deepcopy(payload)
    if historical and name in READ_TOOLS:
        result={'historical':True,'not_current_telemetry':True,
                'note':'旧遥测值不重放；需要当前状态请重新查询。'}
        if name=='get_mission_status':
            result.update(pick(value,('execution_id','status','assignments','visited_task_ids','remaining_targets','error')))
        return {'ok':True,'result':result}
    if name=='get_fleet_state':result=compact_fleet(value)
    elif name=='xd_library_allocate':
        result=pick(value,('id','library_revision','capacity','assignments','unassigned','flight_blocked'))
        result['task_labels']={t['id']:t['label'] for t in value.get('tasks',[])}
        result['unavailable']={a['id']:a['reasons'] for a in value.get('fleet',[]) if not a['available']}
    elif name=='xd_allocate':
        result=pick(value,('id','algorithm','status','assignments','unassigned'))
        result['tasks']=[pick(t,('id','label','priority','basis')) for t in value['tasks']]
        result['excluded']={a['id']:a['reasons'] for a in value['fleet'] if not a['available']}
    elif name in ('xd_confirm_allocation','xd_allocation_status'):
        result=pick(value,('plan_id','status','repeated','task_labels','unassigned'))
        result['vehicles']={uid:pick(row,('task_ids','phase','detail')) for uid,row in value['vehicles'].items()}
    elif name in ('get_plan','plan_inspection'):result=compact_plan(value)
    elif name=='compare_allocations':
        result=pick(value,('plan_id','results','note'))
    elif name=='get_mission_status':
        result=pick(value,('status','execution_id','assignments','visited_task_ids','remaining_targets','controls','error'))
    elif name in WRITE_TOOLS:
        result=pick(value,('id','execution_id','session_id','status','already_submitted','aircraft_ids',
                           'receipts','skipped','note','postcondition_airborne_verified','current_flight_facts'))
        if 'allocation' in value:result['allocation']=compact_allocation(value['allocation'])
        if 'current_aircraft' in value:
            result['current_aircraft']=compact_fleet({'aircraft':value['current_aircraft']})['aircraft']
    else:result=copy.deepcopy(value)
    if historical:
        result['historical']=True
        result['not_current_telemetry']=True
        result.pop('current_aircraft',None);result.pop('current_flight_facts',None)
    return {'ok':True,'result':result}


def parse_tool(message,historical=False):
    name=message.get('tool_name','')
    try:payload=json.loads(message.get('content','{}'))
    except (TypeError,ValueError):
        return {'tool':name,'payload':{'ok':False,'error':'原工具结果无法解析，需重新查询。'}}
    return {'tool':name,'payload':compact_tool_result(name,payload,historical)}


def history_pair(turn):
    user=next((message for message in turn if message.get('role')=='user'),None)
    if user is None:return []
    tools=[]
    # Repeated failures and fleet snapshots need not be replayed verbatim.
    for message in turn:
        if message.get('role')!='tool':continue
        item=parse_tool(message,historical=True)
        if item not in tools:tools.append(item)
    summary={'historical_turn':True,'not_current_state':True,'tools':tools}
    # Tool data is authoritative. Keep conversational replies only when no tool
    # returned a result; this avoids repeating model hallucinations about actions.
    if not tools:
        reply=next((m.get('content','') for m in reversed(turn) if m.get('role')=='assistant' and not m.get('tool_calls')),'')
        if reply:summary['reply']=reply[:400]
    return [{'role':'user','content':user.get('content','')},
            {'role':'assistant','content':'历史回合摘要（不是当前遥测）：'+dumps(summary)}]


def compact_current(turn,summarize_older=False):
    """Keep active assistant/tool pairs intact, optionally summarize older pairs."""
    messages=[]
    for message in turn:
        item=copy.deepcopy(message)
        if item.get('role')=='tool':
            parsed=parse_tool(item)
            item['content']=dumps(parsed['payload'])
        messages.append(item)
    if not summarize_older:return messages,0
    starts=[i for i,m in enumerate(messages) if m.get('role')=='assistant' and m.get('tool_calls')]
    if len(starts)<=2:return messages,0
    cut=starts[-2]
    earlier=messages[:cut];summary=[]
    for message in earlier:
        if message.get('role')=='tool':
            item=parse_tool(message,historical=True)
            # Latest instance of each read-only result is sufficient here.
            if item['tool'] in READ_TOOLS:
                summary=[old for old in summary if old['tool']!=item['tool']]
            if item not in summary:summary.append(item)
    prefix=[m for m in earlier if m.get('role') in ('user','system')]
    if summary:
        prefix.append({'role':'assistant','content':'本轮较早工具结果摘要（旧遥测不作为当前值）：'+dumps(summary)})
    return prefix+messages[cut:],len(starts)-2


class ContextBudgetError(ValueError):
    pass


def build_context_messages(system,context,turns,current_turn,tool_defs,num_ctx=16384,
                           reserve_tokens=4096,safety_tokens=1024):
    """Pack each model request afresh and leave a generation/safety allowance.

    Returned metadata reports estimates; production usage counters are separate.
    Exact current user content and domain anchors are never substring-truncated.
    If required content alone cannot fit, fail before a model/actuator call.
    """
    budget=num_ctx-reserve_tokens-safety_tokens
    if budget<=0:raise ContextBudgetError('模型上下文配置没有为输入保留空间')
    anchors=copy.deepcopy(context)
    base=[{'role':'system','content':system+'\n当前会话资料（方案/选择锚点，不代表已执行）：'+dumps(anchors)}]
    active,older_count=compact_current(current_turn)
    if estimate_tokens(base+active,tool_defs)>budget:
        active,older_count=compact_current(current_turn,summarize_older=True)
    required=estimate_tokens(base+active,tool_defs)
    if required>budget:
        raise ContextBudgetError('本轮必要内容超出上下文预算；未发送新的模型请求，请缩小本轮需求或读取指定方案详情')
    history=[];included=0
    for turn in reversed(turns[-6:]):
        pair=history_pair(turn)
        if not pair:continue
        candidate=base+pair+history+active
        if estimate_tokens(candidate,tool_defs)>budget:break
        history=pair+history;included+=1
    messages=base+history+active
    metadata={'estimator':'conservative character heuristic; not tokenizer counts',
              'estimated_input_tokens':estimate_tokens(messages,tool_defs),
              'estimated_required_tokens':required,'input_budget_tokens':budget,
              'num_ctx':num_ctx,'reserved_generation_tokens':reserve_tokens,'safety_tokens':safety_tokens,
              'history_turns_included':included,'history_turns_omitted':len(turns)-included,
              'earlier_current_tool_blocks_summarized':older_count,
              'input_characters':len(dumps(messages)),'tool_characters':len(dumps(tool_defs))}
    return messages,metadata
