"""Bounded Ollama NDJSON collection. Partial tool calls are never executable."""
import copy
import json
import time
import urllib.request


def collect(chunks,on_event=lambda event:None,clock=time.monotonic,max_seconds=60,thinking_seconds=18):
    began=clock();thinking_began=None;last_progress=-1;done=False
    message={'role':'assistant','content':'','thinking':'','tool_calls':[]}
    result={'message':message};calls={}
    def finish(reason,error=None):
        result.update(done=False,done_reason=reason)
        if error:result['stream_error']=str(error)[:300]
        return result
    try:
        for chunk in chunks:
            now=clock()
            if now-began>max_seconds:return finish('generation_timeout')
            if chunk.get('error'):raise RuntimeError(chunk['error'])
            part=chunk.get('message',{})
            if part.get('thinking'):
                if thinking_began is None:thinking_began=now
                message['thinking']+=part['thinking']
            if part.get('content'):
                message['content']+=part['content']
                on_event({'type':'text_delta','text':part['content']})
            for call in part.get('tool_calls',[]):
                # Native Ollama sends a complete function object per call. An
                # index identifies repeated snapshots of that same call.
                index=call.get('function',{}).get('index',call.get('index'))
                if index is None:message['tool_calls'].append(copy.deepcopy(call))
                else:calls[index]=copy.deepcopy(call)
            if calls:message['tool_calls']=list(calls.values())
            if now-last_progress>=.25 or chunk.get('done'):
                on_event({'type':'model_progress','phase':'answering' if message['content'] else 'thinking',
                    'thinking_characters':len(message['thinking']),'answer_characters':len(message['content']),
                    'elapsed_s':round(now-began,2)})
                last_progress=now
            if chunk.get('done'):
                result.update({k:v for k,v in chunk.items() if k!='message'});done=True;break
            if (thinking_began is not None and not message['content'] and not message['tool_calls']
                    and now-thinking_began>=thinking_seconds):return finish('thinking_timeout')
    except (OSError,ValueError,RuntimeError) as exc:
        return finish('stream_interrupted',exc)
    if not done:return finish('stream_interrupted','模型连接结束但未返回完成标记')
    return result


class OllamaStream:
    def __init__(self,url,opener=None,read_timeout=30,max_seconds=60,thinking_seconds=18):
        self.url=url;self.http=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.read_timeout=read_timeout;self.max_seconds=max_seconds;self.thinking_seconds=thinking_seconds

    def __call__(self,payload,on_event):
        data=copy.deepcopy(payload);data['stream']=True
        request=urllib.request.Request(self.url,json.dumps(data,ensure_ascii=False).encode(),{'Content-Type':'application/json'})
        with self.http.open(request,timeout=self.read_timeout) as response:
            chunks=(json.loads(line) for line in response if line.strip())
            return collect(chunks,on_event,max_seconds=self.max_seconds,thinking_seconds=self.thinking_seconds)
