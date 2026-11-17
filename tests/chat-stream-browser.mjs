import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import {readFile,writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';

const root=path.resolve('web'),report={scope:'isolated HTTP streaming/UI fixture; no model or flight actuation',checks:[],pageErrors:[]};
let submitted=0,legacy=0;
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');
 const json=value=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
 if(url.pathname==='/api/health')return json({model:{available:true,model:'stream-test'}});
 if(url.pathname==='/api/state')return json({aircraft:[]});
 if(url.pathname==='/api/simulation')return json({status:'ready',ready:true,report:{status:'waiting_for_task',tasks:[]}});
 if(url.pathname==='/api/chat'){legacy++;return json({error:'unexpected retry'});}
 if(url.pathname==='/api/chat/stream'){
  let body='';for await(const chunk of req)body+=chunk;const input=JSON.parse(body);const n=++submitted;
  res.writeHead(200,{'Content-Type':'application/x-ndjson; charset=utf-8','Cache-Control':'no-store'});
  const event=value=>res.write(JSON.stringify(value)+'\n');
  event({type:'start',conversation_id:input.conversation_id});event({type:'model_start',step:1});
  event({type:'model_progress',phase:'thinking',thinking_characters:123,thinking:'UNEXPOSED_REASONING_TEST'});
  const text=n===1?'第一段正文':n===2?'尚未完成的解释':'连接断开前的正文';
  setTimeout(()=>{const bytes=Buffer.from(JSON.stringify({type:'text_delta',text})+'\n');res.write(bytes.subarray(0,bytes.length-5));setTimeout(()=>res.write(bytes.subarray(bytes.length-5)),30);},100);
  if(n===3){setTimeout(()=>res.end(),350);return;}
  setTimeout(()=>{
   event({type:'result',result:{reply:n===1?'第一段正文，最终结果。':'生成已停止，未下发新动作。',source:n===1?'qwen_agent':'controller',conversation_id:input.conversation_id,
    partial_reply:n===2?text:'',reply_complete:n===1,stop_reason:n===2?'model_output_limit':null,
    tool_calls:[],model_usage:[],duration_s:1.5}});res.end();
  },1500);return;
 }
 const file=path.resolve(root,url.pathname.slice(1));
 if(!file.startsWith(root+path.sep)){res.writeHead(404);res.end();return;}
 try{const bytes=await readFile(file);res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream'});res.end(bytes);}catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,executablePath:'/home/ldb/.cache/ms-playwright/chromium-1140/chrome-linux/chrome',args:['--no-sandbox','--disable-dev-shm-usage']});
const check=(name,value)=>{assert.ok(value,name);report.checks.push(name);};
try{
 const page=await browser.newPage({viewport:{width:1300,height:900}});page.on('pageerror',e=>report.pageErrors.push(e.message));
 await page.goto(`http://127.0.0.1:${server.address().port}/chat.html`);
 const send=async text=>{await page.locator('#chat-input').fill(text);await page.locator('#send').click();};
 await send('查询状态');
 await page.waitForFunction(()=>document.querySelector('.provisional .message-body')?.textContent.includes('第一段正文'));
 check('Visible text appears before final HTTP result',await page.locator('#send').isDisabled());
 check('Textarea stays editable while model is generating',await page.locator('#chat-input').isEditable());
 await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 check('Final result replaces provisional text exactly once',await page.locator('.provisional').count()===0&&(await page.locator('.message.assistant .message-body').allTextContents()).filter(x=>x==='第一段正文，最终结果。').length===1);
 await send('测试截断');await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 check('Partial content retained separately on truncation',await page.locator('.partial-reply').count()===1);
 check('Partial content is labelled as incomplete',(await page.locator('.partial-reply summary').innerText()).includes('不代表动作已执行'));
 await send('测试断线');await page.waitForFunction(()=>!document.querySelector('#send').disabled);
 check('Interrupted stream preserves visible draft',(await page.locator('.provisional').innerText()).includes('连接断开前的正文'));
 check('Interrupted request is restored without automatic resend',await page.locator('#chat-input').inputValue()==='测试断线'&&submitted===3&&legacy===0);
 check('Failure prompts checking actual state',(await page.locator('.error').last().innerText()).includes('QGC'));
 check('No private reasoning displayed',!(await page.locator('body').innerText()).includes('UNEXPOSED_REASONING_TEST'));
 check('No unhandled browser errors',report.pageErrors.length===0);
 await page.screenshot({path:'reports/chat-stream-ui.png'});
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));await writeFile('reports/chat-stream-browser.json',JSON.stringify(report,null,2));}
console.log(JSON.stringify(report,null,2));
