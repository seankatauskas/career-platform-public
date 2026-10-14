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
    const ref={owner:'correspondence',source_id:'mail1',revision:'r1',sha256:'exact-hash',quote:'<script>unsafe()</script>'};
    const proposal=(id,extra={})=>({id,version:3,status:'pending',operation:'create_task',input:{description:'Complete assessment'},application_id:'app1',blockers:[],dependencies:[],evidence:[ref],before:{application:{stage:'preparing'}},...extra});
    const app={id:'app1',job:{employer:'Example',title:'Engineer'}};
    const issue={issue_id:'issue1',id:'issue1',version:1,analysis_id:'analysis1',status:'open',failure_code:'evidence_mismatch',attempt_count:1,sources:[ref]};
    const evidenceGroup=(id,proposals=[],processing=[])=>({id,kind:'evidence_group',source_type:'email',preview_digest:'digest-'+id,applications:[app],application:app,proposals,processing,evidence:[ref]});
    let groups=[evidenceGroup('email:mail1',[proposal('p1'),proposal('blocked',{blockers:['requires_target_record_resolution']})],[issue]),evidenceGroup('email:mail2',[proposal('p2')])];
    let actions=[{action_id:'external',digest:'exact-action',application_id:'app1',authorization:'pending',execution:'not_started',envelope:{kind:'send_reply',account_id:'account',target:{to:['recruiter@example.test']},payload:{body:'Exact reply'}}}];
    let policy={enabled:false,version:0}; let history=[];
    async function api(path,options){
      requests.push({path,options});
      if(path.startsWith('/api/v1/application-owner/review-groups?')) {
        if(failLoad) throw new Error('Connection unavailable');
        const query=new URLSearchParams(path.split('?')[1]);
        const items=query.has('cursor') ? [evidenceGroup('email:later',[proposal('later')])] : query.has('group_id') ? [evidenceGroup('email:linked',[proposal(query.get('group_id'))])] : groups;
        return {items,next_cursor:query.has('cursor')?null:'next',totals:{cards:groups.length+actions.length+1,operations:groups.reduce((n,g)=>n+g.proposals.length,0)+1,processing:groups.filter(g=>g.processing.length).length},actions,policy};
      }
      if(path.startsWith('/api/v1/application-owner/review?')) return {items:history,pages:{processing_history:{next_cursor:null}}};
      if(path.startsWith('/api/v1/application-owner/review-source?')) return {available:true,text:'Exact original email <script>unsafe()</script>',coverage:{complete:true}};
      if(path.startsWith('/api/v1/application-owner/applications?')) return {items:[app,{id:'app2',job:{employer:'Other',title:'Engineer'}}],next_cursor:null};
      if(path.startsWith('/api/v1/application-commands/')) {
        if(failCommand) throw new Error('Response lost');
        if(conflict){const error=new Error('Version changed');error.status=409;throw error;}
        const payload=JSON.parse(options.body);
        if(path.endsWith('/retry_processing')) {issue.status='retry_queued';issue.version++;return issue;}
        if(path.endsWith('/configure_browser_recording')) {policy={enabled:payload.enabled,version:policy.version+1};return policy;}
        if(path.endsWith('/correct_review_target')) return {status:'replaced'};
        if(payload.decisions) for(const group of groups)group.proposals=group.proposals.filter(p=>!payload.decisions.some(d=>d.proposal_id===p.id));
        if(payload.action_id) actions=[];
        return {saved:true};
      }
      throw new Error('Unexpected request '+path);
    }
  `});
  for(const file of ['review-view.js','owner-review-view.js']) await page.addScriptTag({content:await readFile(new URL('../../job_search/web/'+file,import.meta.url),'utf8')});
  await page.evaluate(()=>{document.querySelector("#refresh-attention").addEventListener("click",()=>loadOwnerReviewQueue());return loadReviewQueue();});
  assert.equal(await page.locator('#attention-list > article').count(),3);
  assert.equal(await page.locator('#review-count').innerText(),'4');
  assert.match(await page.locator('#review-summary').innerText(),/4 evidence and action cards · 4 proposed changes · 1 processing problems/);
  const group=page.locator('[data-review-key="evidence_group:email:mail1"]');
  assert.equal(await group.locator('[data-proposal-id]').count(),2);
  assert.equal(await group.getByText('Source evidence',{exact:true}).count(),1);
  assert.equal(await page.locator('#attention script,#attention img').count(),0);
  assert.equal(await group.locator('[data-proposal-id="blocked"]').getByRole('button',{name:'Accept change',exact:true}).count(),0);
  await group.getByText('Source evidence',{exact:true}).click();
  await group.getByRole('button',{name:'Read source email'}).click();
  assert.match(await group.innerText(),/Exact original email/);
  assert.match(await page.evaluate(()=>requests.find(r=>r.path.includes('review-source')).path),/source_id=mail1&revision=r1&sha256=exact-hash/);
  assert.equal(await group.getByRole('button',{name:'Retry processing',exact:true}).isDisabled(),true);
  await group.getByRole('textbox',{name:'Reason for retry or resolution'}).fill('Retry exact quote validation');
  await page.evaluate(()=>{failCommand=true;});
  await group.getByRole('button',{name:'Retry processing',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.match(await group.innerText(),/outcome could not be confirmed/);
  await page.evaluate(()=>{failCommand=false;});
  await group.getByRole('button',{name:'Retry same request'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const retries=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/retry_processing')));
  assert.equal(retries[0].options.headers['Idempotency-Key'],retries[1].options.headers['Idempotency-Key']);
  assert.match(await group.innerText(),/Retry queued/);
  await group.getByText('Correct application or receipt scope',{exact:true}).click();
  await group.getByRole('combobox',{name:'Application',exact:true}).selectOption('app2');
  await group.getByRole('combobox',{name:'Submission evidence'}).selectOption('application');
  await group.getByRole('textbox',{name:'Reason for correction'}).fill('Wrong employer in proposed target');
  await group.getByRole('button',{name:'Replace pending proposals'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const correction=await page.evaluate(()=>JSON.parse(requests.find(r=>r.path.endsWith('/correct_review_target')).options.body));
  assert.equal(correction.application_id,'app2');assert.equal(correction.expected_digest,'digest-email:mail1');assert.equal(correction.receipt_scope,'application');
  await group.locator('[data-proposal-id="p1"] input[type=checkbox]').check();
  // Selection is scoped to this source card, even when other cards are selected.
  await page.locator('[data-proposal-id="p2"] input[type=checkbox]').check();
  await group.getByRole('button',{name:'Accept 1 selected change',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const decisions=await page.evaluate(()=>JSON.parse(requests.find(r=>r.path.endsWith('/review_changes')).options.body));
  assert.deepEqual(decisions.decisions,[{proposal_id:'p1',expected_version:3,decision:'accept'}]);
  assert.equal(await page.locator('[data-proposal-id="p2"]').count(),1);
  await page.getByText('Browser recording: review first',{exact:true}).click();
  await page.getByRole('button',{name:'Enable automatic browser recording',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.equal(await page.getByText('Browser recording: automatic',{exact:true}).count(),1);
  await page.getByRole('button',{name:'Load more evidence',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy && ownerReviewState.groups.some(g=>g.id==='email:later'));
  assert.equal(await page.locator('[data-proposal-id="later"]').count(),1);
  await page.evaluate(()=>{failLoad=true;});
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.waitForFunction(()=>ownerReviewState.stale);
  assert.equal(await page.locator('[data-proposal-id="p2"]').getByRole('button',{name:'Accept change',exact:true}).isDisabled(),true);
  await page.evaluate(()=>{failLoad=false;location.hash='#review/proposal/deep-linked';});
  await page.evaluate(()=>loadOwnerReviewQueue());
  assert.equal(await page.locator('[data-proposal-id="deep-linked"]').count(),1);
  assert.deepEqual(errors,[]);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  console.log('owner grouped review browser checks passed');
} finally { await browser.close(); }
