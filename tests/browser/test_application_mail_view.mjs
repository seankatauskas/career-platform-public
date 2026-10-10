// UI route and text rendering contract; Python HTTP tests enforce owner access.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const calls=[], bodyText='Exact e\u0301\n<script>unsafe()</script>', app={id:'app:fixture',disposition:'open',version:1};
const warning='A historical submission record has no verified link to a browser attempt. Browser captures remain separate until that link is reviewed.';
app.submission_summary={warnings:[{code:'unresolved_submission_attempt',message:warning}]};
const workspace={application:app,job:{title:'Fictional role'},progress:{stage:'tracking'},review:{items:[]},
  records:{tasks:{items:[]},notes:{items:[]}},conversation:{items:[{id:'message:fixture',direction:'incoming'}]},actions:{items:[]}};
const task=id=>({id,description:id,status:'open',version:1});
const proposal=id=>({id,operation:'add_note',version:1,input:{text:id},evidence:[],dependencies:[],blockers:[]});
const action=id=>({action_id:id,digest:'fixture',authorization:'pending',execution:'not_started',envelope:{kind:'send_reply',payload:{body:id},target:{}}});
workspace.records.tasks={items:[task('first-task')],next_cursor:'tasks-next'};
workspace.review={items:[proposal('first-review')],next_cursor:'review-next'};
workspace.actions={items:[action('first-action')],next_cursor:'actions-next'};
let available=true;
const server=createServer(async(req,res)=>{
  calls.push(req.url);
  const url=new URL(req.url,'http://localhost');
  let result;
  if(url.pathname==='/api/v1/session')result={csrf_token:'fixture'};
  else if(['/api/v1/applications','/api/v1/application-owner/applications'].includes(url.pathname))result=url.searchParams.has('after')?
    {items:[{id:'last-app',job:{title:'Last role'},progress:workspace.progress}],next_cursor:null}:
    {items:[{...app,job:workspace.job,progress:workspace.progress}],next_cursor:'applications-next'};
  else if(['/api/v1/workspace','/api/v1/application-owner/workspace'].includes(url.pathname))result=workspace;
  else if(url.pathname==='/api/v1/application-owner/workspace-page') {
    assert.equal(url.searchParams.get('application_id'),app.id);
    const group=url.searchParams.get('group');
    assert.equal(url.searchParams.get('cursor'),group+'-next');
    result={items:[{tasks:task('last-task'),review:proposal('last-review'),actions:action('last-action')}[group]],next_cursor:null};
  }
  else if(url.pathname==='/api/v1/applications/app%3Afixture/conversation/message%3Afixture')result={available,excerpt:available?bodyText:'',coverage:{complete:available}};
  if(result) {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));return;}
  const file=['/','/applications'].includes(url.pathname)?'application-candidate.html':url.pathname.slice(1).replace(/^candidate\./,'application-candidate.');
  if(!['application-candidate.html','application-candidate.js','application-candidate.css'].includes(file)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
  res.end(await readFile(path.join(root,'job_search/web',file)));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true});
try {
  const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const origin='http://127.0.0.1:'+server.address().port;
  await page.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():route.abort());
  await page.goto(origin+'/applications?application_id=app:fixture');
  await page.getByText(warning,{exact:true}).waitFor();
  await page.getByRole('button',{name:'View message',exact:true}).click();
  await page.waitForFunction(text=>document.querySelector('#messages .message-body').textContent===text,bodyText);
  assert.equal(await page.locator('#messages script').count(),0);
  assert(calls.includes('/api/v1/applications/app%3Afixture/conversation/message%3Afixture'));
  available=false;
  await page.getByRole('button',{name:'View message',exact:true}).click();
  await page.getByText('Message body is unavailable.',{exact:true}).waitFor();
  await page.getByText('Some evidence is unavailable.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Next applications',exact:true}).click();
  await page.getByRole('button',{name:'Application · Last role',exact:true}).waitFor();
  await page.getByRole('button',{name:'Previous applications',exact:true}).click();
  for(const group of ['tasks','review','actions']) {
    await page.getByRole('button',{name:'Load more '+group,exact:true}).click();
    await page.locator('#'+(group==='review'?'reviews':group)).getByText('last-'+(group==='tasks'?'task':group==='actions'?'action':'review'),{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Load more '+group,exact:true}).count(),0);
  }
  assert.equal(await page.getByRole('button',{name:'Complete task',exact:true}).count(),2);
  assert.equal(await page.getByRole('button',{name:'Accept change',exact:true}).count(),2);
  assert.equal(await page.getByRole('button',{name:'Approve exact action',exact:true}).count(),2);
  const bodyReads=calls.filter(x=>x.includes('/conversation/')).length;
  await page.goto(origin+'/?application_id=app:fixture');
  await page.getByRole('button',{name:'View message',exact:true}).click();
  await page.getByText('Message body is unavailable in this standalone workspace.',{exact:true}).waitFor();
  assert.equal(calls.filter(x=>x.includes('/conversation/')).length,bodyReads);
  assert.deepEqual(errors,[]);
  console.log('ok (application mail text and workspace pagination)');
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
