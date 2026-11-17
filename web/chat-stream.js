// One submitted request, no automatic retry after a stream has started.
export async function requestChat(body,onEvent=()=>{}){
  const response=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(response.status===404||response.status===405){
    // Compatibility with an older server: the missing route has executed nothing.
    const legacy=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const value=await legacy.json();if(!legacy.ok)throw new Error(value.error||'请求失败');return value;
  }
  if(!response.ok){const value=await response.json();throw new Error(value.error||'请求失败');}
  if(!response.body)throw new Error('浏览器没有返回可读取的响应流');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',result;
  const consume=line=>{if(!line.trim())return;const event=JSON.parse(line);if(event.type==='error')throw new Error(event.error||'模型处理失败');if(event.type==='result')result=event.result;onEvent(event);};
  try{
    while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});let split;while((split=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,split);buffer=buffer.slice(split+1);consume(line);}if(done)break;}
    if(buffer.trim())consume(buffer);
  }catch(error){if(result)return result;throw error;}
  finally{reader.releaseLock();}
  if(!result)throw new Error('连接在最终结果返回前结束，请先核对任务状态，避免重复下发。');
  return result;
}
