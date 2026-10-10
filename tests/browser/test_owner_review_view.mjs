import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const browser = await chromium.launch({channel:'chromium',headless:true});
const page = await browser.newPage({viewport:{width:1280,height:900}});
page.setDefaultTimeout(10000);
const errors=[]; page.on('pageerror',error=>errors.push(error.message));
try {
  await page.setContent('<span id="review-count"></span><section id="attention"></section>');
  for(const file of ['styles.css','review-view.css']) await page.addStyleTag({content:await readFile(new URL('../../job_search/web/'+file,import.meta.url),'utf8')});
  await page.addScriptTag({content:`
    const state={applicationBackend:'owners',applications:[]};
    const consoleState={view:'review',reviews:[],actions:[]};
    const node=(tag,cls='',text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
    const key=prefix=>prefix+'-'+Math.random();
    const renderApplicationTable=()=>{};const renderApplicationReviewNotices=()=>{};
    const requests=[]; let failLoad=false,failCommand=false,conflict=false;
    const proposal=(id,input={description:'Complete the assessment'})=>({kind:'proposal',id,version:3,status:'pending',operation:'create_task',input,application_id:'app1',blockers:[],dependencies:[],evidence:[{owner:'correspondence',source_id:'mail1',revision:2,sha256:'exact-hash',quote:'<script>unsafe()</script>',start:7,end:36}]});
    const action=(id,authorization='pending',execution='not_started')=>({kind:'external_action',id,application_id:'app1',action:{action_id:id,digest:'exact-'+id,authorization,execution,expires_at:'2027-10-10T00:00:00Z',envelope:{kind:'send_reply',account_id:'reviewed-account',target:{to:['recruiter@example.test']},payload:{body:'Thank you. <img src=x onerror=unsafe()>'},consequence:{task_id:'reply-task',operation:'complete_task'}}}});
    let queue=[proposal('p1'),{...proposal('blocked'),application_id:null,blockers:['needs_target']},action('pending-action'),action('uncertain-action','approved','uncertain'),{kind:'processing',id:'issue1',processing:{id:'issue1',issue_id:'issue1',version:0,analysis_id:'analysis1',attempt_count:2,status:'open',failure_code:'incomplete_coverage',coverage:{complete:false,reasons:['attachment_unavailable']},sources:[{owner:'correspondence',source_id:'unassociated',revision:1,sha256:'source-hash'}],findings:[]}},{kind:'processing',id:'complete-analysis',processing:{analysis_id:'complete-analysis',status:'succeeded',coverage:{complete:true},sources:[{owner:'correspondence',source_id:'irrelevant',revision:1,sha256:'irrelevant-hash'}],findings:[]}},{kind:'processing_history',id:'earlier-failure',processing:{issue_id:'issue1',analysis_id:'earlier-failure',status:'failed',failure_code:'provider_failed',coverage:{complete:true},sources:[],findings:[]}}];
    async function api(path,options){
      requests.push({path,options});
      if(path.startsWith('/api/v1/application-owner/review?')) {
        if(failLoad) throw new Error('Connection unavailable');
        if(path.includes('group=proposals')) return {items:[proposal('p2',{text:'Second page note'})],pages:{proposals:{next_cursor:null}}};
        return {items:queue,pages:{proposals:{next_cursor:'page-2',truncated:true},external_actions:{next_cursor:null},processing:{next_cursor:null}}};
      }
      if(path.startsWith('/api/v1/application-owner/review-source?')) return {available:true,text:'Exact original email <script>unsafe()</script>',coverage:{complete:true}};
      if(path.startsWith('/api/v1/application-commands/')) {
        if(failCommand) throw new Error('Response lost');
        if(conflict){const error=new Error('Version changed');error.status=409;throw error;}
        const payload=JSON.parse(options.body);
        if(path.endsWith('/retry_processing')) {const found=queue.find(item=>item.id===payload.issue_id);found.processing.status='retry_queued';found.processing.version+=1;return found.processing;}
        if(path.endsWith('/resolve_processing')) {const found=queue.find(item=>item.id===payload.issue_id);found.processing.status='resolved_manually';found.processing.version+=1;found.processing.resolution_reason=payload.reason;queue=queue.filter(item=>item!==found);queue.push({kind:'processing_history',id:found.processing.analysis_id,processing:{analysis_id:found.processing.analysis_id,status:'failed',failure_code:'incomplete_coverage',sources:found.processing.sources,current_issue:found.processing}});return found.processing;}
        if(payload.decisions)queue=queue.filter(item=>!payload.decisions.some(d=>d.proposal_id===item.id));
        else queue=queue.filter(item=>item.id!==payload.action_id);
        return {saved:true};
      }
      throw new Error('Unexpected legacy request '+path);
    }
  `});
  for(const file of ['review-view.js','owner-review-view.js']) await page.addScriptTag({content:await readFile(new URL('../../job_search/web/'+file,import.meta.url),'utf8')});
  await page.evaluate(()=>loadReviewQueue());
  assert.equal(await page.locator('#attention-list > article').count(),4);
  assert.equal(await page.locator('#owner-processing-list > article').count(),1);
  assert.equal(await page.locator('#review-count').innerText(),'5+');
  assert.equal(await page.locator('#mail-review-history').isVisible(),true);
  assert.equal(await page.locator('#mail-review-history').getAttribute('open'),null);
  assert.equal(await page.locator('#mail-history-list > article').count(),2);
  assert.equal(await page.locator('#attention-list [data-review-key="processing:complete-analysis"]').count(),0);
  assert.equal(await page.locator('#attention-list > .stack-item.review-card').count(),4);
  assert.equal(await page.locator('[data-review-key="proposal:p1"] h3').innerText(),'Create a task');
  await page.evaluate(()=>{location.hash='#review/proposal/later?application=other-app';renderReviewQueue();});
  assert.match(await page.locator('#attention-list').innerText(),/Load more to check the remaining items/);
  assert.doesNotMatch(await page.locator('#attention-list').innerText(),/Nothing needs review/);
  await page.evaluate(()=>{location.hash='#review';renderReviewQueue();});
  assert.equal(await page.locator('#attention script,#attention img').count(),0);
  const blocked=page.locator('[data-review-key="proposal:blocked"]');
  assert.match(await blocked.innerText(),/Not linked to an application/);
  assert.equal(await blocked.getByRole('button',{name:'Accept change',exact:true}).count(),0);
  const processing=page.locator('[data-review-key="processing:issue1"]');
  assert.match(await processing.innerText(),/attachment unavailable/);
  await processing.getByText('Source evidence',{exact:true}).click();
  await processing.getByRole('button',{name:'Read source email'}).click();
  assert.match(await processing.innerText(),/Exact original email/);
  const read=await page.evaluate(()=>requests.find(r=>r.path.includes('review-source')));
  assert.match(read.path,/source_id=unassociated&revision=1&sha256=source-hash/);
  const retryButton=processing.getByRole('button',{name:'Retry processing',exact:true});
  assert.equal(await retryButton.isDisabled(),true);
  await processing.getByRole('textbox',{name:'Reason for retry or resolution'}).fill('Retry after the analysis configuration was fixed.');
  await page.evaluate(()=>{failCommand=true;});
  await retryButton.click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.match(await processing.innerText(),/request outcome could not be confirmed/);
  assert.equal(await processing.getByRole('textbox').count(),0);
  await page.evaluate(()=>{failCommand=false;});
  await processing.getByRole('button',{name:'Retry same request',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const retries=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/retry_processing')));
  assert.deepEqual(JSON.parse(retries[0].options.body),{issue_id:'issue1',expected_version:0,expected_analysis_id:'analysis1',reason:'Retry after the analysis configuration was fixed.'});
  assert.equal(retries[0].options.headers['Idempotency-Key'],retries[1].options.headers['Idempotency-Key']);
  assert.match(await processing.innerText(),/Retry queued/);
  assert.equal(await processing.getByRole('button',{name:'Retry already queued'}).isDisabled(),true);
  assert.equal(await page.locator('#owner-processing-list > article').count(),1);
  await processing.getByRole('textbox',{name:'Reason for retry or resolution'}).fill('Reviewed the original email; no further analysis is needed.');
  await processing.getByRole('button',{name:'Resolve processing problem'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const resolution=await page.evaluate(()=>requests.find(r=>r.path.endsWith('/resolve_processing')));
  assert.deepEqual(JSON.parse(resolution.options.body),{issue_id:'issue1',expected_version:1,expected_analysis_id:'analysis1',reason:'Reviewed the original email; no further analysis is needed.'});
  assert.equal(await page.locator('#owner-processing-list > article').count(),0);
  assert.equal(await page.locator('#mail-history-list > article').count(),3);
  assert.match(await page.locator('#mail-history-list').textContent(),/Resolution reason: Reviewed the original email/);
  const uncertain=page.locator('[data-review-key="external_action:uncertain-action"]');
  assert.equal(await uncertain.getByRole('button',{name:'Approve exact action'}).count(),0);
  assert.equal(await uncertain.getByRole('button',{name:/Retry|Mark sent/}).count(),0);
  assert.equal(await uncertain.getByRole('link',{name:'View recovery status'}).getAttribute('href'),'#ops');
  await page.getByRole('button',{name:'Load more application changes'}).click();
  await page.locator('[data-review-key="proposal:p2"]').waitFor();
  assert.equal(await page.getByRole('button',{name:'Load more application changes'}).count(),0);
  await page.locator('[data-review-key="proposal:p1"]').getByRole('checkbox').check();
  await page.locator('[data-review-key="proposal:p2"]').getByRole('checkbox').check();
  await page.getByRole('button',{name:'Accept selected changes'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const batch=await page.evaluate(()=>requests.find(r=>r.path.endsWith('/review_changes')));
  assert.deepEqual(JSON.parse(batch.options.body),{decisions:[{proposal_id:'p1',expected_version:3,decision:'accept'},{proposal_id:'p2',expected_version:3,decision:'accept'}]});
  const pending=page.locator('[data-review-key="external_action:pending-action"]');
  assert.match(await pending.innerText(),/reviewed-account/);
  assert.match(await pending.innerText(),/recruiter@example.test/);
  assert.match(await pending.innerText(),/Thank you/);
  await page.evaluate(()=>{failCommand=true;});
  await pending.getByRole('button',{name:'Approve exact action'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  await page.evaluate(()=>{failCommand=false;});
  await pending.getByRole('button',{name:'Approve exact action'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const approvals=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/authorize_action')));
  assert.deepEqual(JSON.parse(approvals[0].options.body),{action_id:'pending-action',expected_digest:'exact-pending-action'});
  assert.equal(approvals[0].options.headers['Idempotency-Key'],approvals[1].options.headers['Idempotency-Key']);
  await page.evaluate(()=>{queue.push(proposal('stale'));return loadReviewQueue();});
  await page.evaluate(()=>{conflict=true;});
  await page.locator('[data-review-key="proposal:stale"]').getByRole('button',{name:'Accept change',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.match(await page.locator('#review-feedback').innerText(),/Refresh and review/);
  assert.equal(await page.locator('[data-review-key="proposal:stale"]').getByRole('button',{name:'Accept change',exact:true}).isDisabled(),true);
  await page.evaluate(()=>{conflict=false;failLoad=true;return loadReviewQueue();});
  assert.match(await page.locator('#review-feedback').innerText(),/Previous items are kept/);
  await page.evaluate(()=>{failLoad=false;return loadReviewQueue();});
  await uncertain.getByRole('button',{name:'Stop further execution'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const revoke=await page.evaluate(()=>requests.find(r=>r.path.endsWith('/revoke_action')));
  assert.deepEqual(JSON.parse(revoke.options.body),{action_id:'uncertain-action',expected_digest:'exact-uncertain-action'});
  for(const width of [390,1280]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
  await page.evaluate(()=>{queue=queue.filter(item=>item.id==='complete-analysis');return loadReviewQueue();});
  assert.equal(await page.locator('#review-count').innerText(),'');
  assert.equal(await page.locator('#attention-list > article').count(),0);
  assert.equal(await page.locator('#mail-history-list > article').count(),1);
  await page.locator('#mail-review-history > summary').click();
  await page.locator('#mail-history-list .review-message > summary').click();
  await page.locator('#mail-history-list').getByRole('button',{name:'Read source email'}).click();
  assert.match(await page.locator('#mail-history-list').innerText(),/Exact original email/);
  assert.deepEqual(errors,[]);
  console.log('ok (owner Review: global evidence, exact decisions, pagination, stale conflicts, safe recovery, processing retry/resolution, retained attempts, idempotent retry, escaping)');
}finally{await browser.close();}
