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
    let policy={enabled:false,version:0}; let history=[]; let previewCorrections=false, pagedApplications=false;
    let discovery={employer:'Northwind',title:'Platform Engineer',can_create:true,complete:true,match_digest:'match-preview',applications:[],jobs:[],reason:'Create a Tracking application and attach this email. Proposed updates remain in Review.'};
    async function api(path,options){
      requests.push({path,options});
      if(path.startsWith('/api/v1/application-owner/review-groups?')) {
        if(failLoad) throw new Error('Connection unavailable');
        const query=new URLSearchParams(path.split('?')[1]);
        const items=query.has('cursor') ? [evidenceGroup('email:later',[proposal('later')])] : query.has('group_id') ? [evidenceGroup('email:linked',[proposal(query.get('group_id'))])] : groups;
        return {items,next_cursor:query.has('cursor')?null:'next',totals:{decisions:groups.filter(g=>g.proposals.length).length+actions.length+1,cards:groups.length+actions.length+1,operations:groups.reduce((n,g)=>n+g.proposals.length,0)+1,processing:groups.filter(g=>g.processing.length).length},actions,policy};
      }
      if(path.startsWith('/api/v1/application-owner/review?')) return {items:history,pages:{processing_history:{next_cursor:null}}};
      if(path.startsWith('/api/v1/application-owner/review-source?')) return {available:true,text:'Exact original email <script>unsafe()</script>',coverage:{complete:true}};
      if(path.startsWith('/api/v1/application-owner/discovery-preview?')) return discovery;
      if(pagedApplications && path.startsWith('/api/v1/application-owner/applications?') && !path.includes('&after=')) return {items:[app],next_cursor:'second'};
      if(path.startsWith('/api/v1/application-owner/applications?')) return {items:[app,{id:'app2',job:{employer:'Other',title:'Engineer'}}],next_cursor:null};
      if(path.startsWith('/api/v1/application-commands/')) {
        if(failCommand) throw new Error('Response lost');
        if(conflict){const error=new Error('Version changed');error.status=409;throw error;}
        const payload=JSON.parse(options.body);
        if(path.endsWith('/retry_processing')) {issue.status='retry_queued';issue.version++;return issue;}
        if(path.endsWith('/configure_browser_recording')) {policy={enabled:payload.enabled,version:policy.version+1};return policy;}
        if(path.endsWith('/correct_review_target')) {
          if(previewCorrections) groups=groups.map(g=>g.id!==payload.group_id?g:{...g,preview_digest:'corrected-digest',applications:[{id:payload.application_id,job:{employer:'Other',title:'Engineer'}}],application:{id:payload.application_id},
            proposals:[proposal('corrected-link',{operation:'link_message',application_id:payload.application_id,input:{application_id:payload.application_id,message_id:g.id.slice(6)}}),proposal('corrected-receipt',{operation:'record_submission_receipt',application_id:payload.application_id,input:{application_id:payload.application_id},dependencies:['corrected-link'],prerequisites:[{id:'corrected-link',status:'pending',operation:'link_message'}]})]});
          return {status:'replaced'};
        }
        if(path.endsWith('/create_application_from_review')) {groups=groups.map(g=>({...g,applications:[app],application:app,association:{application_id:app.id}}));return {status:'applied',application_id:app.id};}
        if(payload.decisions) for(const group of groups) {
          for(const decision of payload.decisions) {
            const linked=group.proposals.find(p=>p.id===decision.proposal_id && p.operation==='link_message');
            if(linked && decision.decision==='accept') {
              group.association={application_id:linked.application_id};
              for(const p of group.proposals) p.prerequisites=(p.prerequisites||[]).map(d=>d.id===linked.id?{...d,status:'applied'}:d);
            }
          }
          group.proposals=group.proposals.filter(p=>!payload.decisions.some(d=>d.proposal_id===p.id));
        }
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
  assert.match(await page.locator('#review-summary').innerText(),/4 decisions · 1 system issues/);
  const group=page.locator('[data-review-key="evidence_group:email:mail1"]');
  assert.equal(await group.locator('[data-proposal-id]').count(),2);
  assert.equal(await group.getByText('Source evidence',{exact:true}).count(),1);
  assert.equal(await page.locator('#attention script,#attention img').count(),0);
  assert.equal(await group.locator('[data-proposal-id="blocked"]').getByRole('button',{name:'Accept change',exact:true}).count(),0);
  await group.getByText('Source evidence',{exact:true}).click();
  await group.getByRole('button',{name:'Read source email'}).click();
  assert.match(await group.innerText(),/Exact original email/);
  assert.match(await page.evaluate(()=>requests.find(r=>r.path.includes('review-source')).path),/source_id=mail1&revision=r1&sha256=exact-hash/);
  const system=page.locator('#owner-processing-section');
  assert.equal(await group.getByRole('button',{name:'Retry processing',exact:true}).count(),0);
  assert.equal(await system.getByRole('button',{name:'Retry processing',exact:true}).isDisabled(),false);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length),0);
  await group.getByRole('link',{name:'Analysis needs attention · View system issue'}).click();
  assert.equal(await page.evaluate(()=>document.activeElement.id),'system-email:mail1');
  await system.screenshot({path:'.cache/review-system-attention.png'});
  await system.getByRole('link',{name:'View proposed updates'}).click();
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.reviewKey),'evidence_group:email:mail1');
  await page.evaluate(()=>{failCommand=true;});
  await system.getByRole('button',{name:'Retry processing',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.match(await system.innerText(),/outcome could not be confirmed/);
  await page.evaluate(()=>{failCommand=false;});
  await system.getByRole('button',{name:'Retry same request'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const retries=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/retry_processing')));
  assert.equal(retries[0].options.headers['Idempotency-Key'],retries[1].options.headers['Idempotency-Key']);
  assert.match(await system.innerText(),/Retry queued/);
  await group.getByText('Change application',{exact:true}).click();
  await group.getByRole('radio',{name:'Other · Engineer'}).check();
  await group.getByText('Receipt options',{exact:true}).click();
  await group.getByRole('combobox',{name:'Submission evidence'}).selectOption('application');
  await group.getByRole('button',{name:'Continue',exact:true}).click();
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
  assert.equal(decisions.group_id,'email:mail1');assert.equal(decisions.expected_digest,'digest-email:mail1');
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
  await page.evaluate(()=>{
    location.hash='#review';
    groups=[{...evidenceGroup('email:new',[proposal('unassigned',{application_id:null,blockers:['needs_target']})]),applications:[],application:null}];
    actions=[];
    return loadOwnerReviewQueue();
  });
  const unassigned=page.locator('[data-review-key="evidence_group:email:new"]');
  await unassigned.getByRole('button',{name:'None of these',exact:true}).click();
  await unassigned.getByRole('button',{name:'Create application record',exact:true}).waitFor();
  assert.equal(await unassigned.getByLabel('Employer from email').inputValue(),'Northwind');
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/create_application_from_review')).length),0);
  await unassigned.getByLabel('Role from email').fill('Staff Platform Engineer');
  assert.equal(await unassigned.getByRole('button',{name:'Create application record',exact:true}).count(),0);
  await page.evaluate(()=>{discovery={...discovery,title:'Staff Platform Engineer'};});
  await unassigned.getByRole('button',{name:'Check for existing role',exact:true}).click();
  await page.evaluate(()=>{failCommand=true;});
  await unassigned.getByRole('button',{name:'Create application record',exact:true}).click();
  await unassigned.getByRole('button',{name:'Retry same creation',exact:true}).waitFor();
  assert.match(await unassigned.innerText(),/outcome is unconfirmed/);
  await page.evaluate(()=>{failCommand=false;});
  await unassigned.getByRole('button',{name:'Retry same creation',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const creations=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/create_application_from_review')));
  assert.equal(creations.length,2);
  assert.equal(creations[0].options.headers['Idempotency-Key'],creations[1].options.headers['Idempotency-Key']);
  assert.deepEqual(JSON.parse(creations[0].options.body),{group_id:'email:new',expected_digest:'digest-email:new',expected_match_digest:'match-preview',employer:'Northwind',title:'Staff Platform Engineer'});
  assert.equal(await unassigned.locator('[data-proposal-id="unassigned"]').count(),1);
  assert.equal(await unassigned.getByRole('button',{name:'Create application record',exact:true}).count(),0);
  await page.evaluate(()=>{
    groups=[{...evidenceGroup('email:catalog',[proposal('catalog-receipt',{application_id:null,blockers:['needs_target','unresolved_association']})]),
      applications:[],application:null,catalog_retrieval:{complete:true},catalog_candidates:[
        {id:'catalog:greenhouse:7703668003',match_digest:'catalog-match',suggested:true,
          job:{ats:'greenhouse',source_id:'7703668003',employer:'Axon',title:'Software Engineer I',location:'Seattle'}},
        {id:'catalog:greenhouse:7818932003',match_digest:'other-match',suggested:false,
          job:{ats:'greenhouse',source_id:'7818932003',employer:'Axon',title:'Software Engineer I',location:'Romania'}}]}];
    return loadOwnerReviewQueue();
  });
  const catalogGroup=page.locator('[data-review-key="evidence_group:email:catalog"]');
  await catalogGroup.getByRole('button',{name:'None of these',exact:true}).click();
  assert.match(await catalogGroup.innerText(),/No application or browser submission was recorded/);
  assert.match(await catalogGroup.innerText(),/Seattle/);
  assert.match(await catalogGroup.innerText(),/Romania/);
  assert(!/Which application is this email about/.test(await catalogGroup.innerText()));
  assert.equal(await catalogGroup.getByRole('button',{name:'Use this role and attach email',exact:true}).count(),2);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await catalogGroup.getByRole('button',{name:'Use this role and attach email',exact:true}).first().click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const selectedRole=await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path.endsWith('/create_application_from_review')).at(-1).options.body));
  assert.deepEqual(selectedRole,{group_id:'email:catalog',expected_digest:'digest-email:catalog',
    expected_match_digest:'catalog-match',catalog_id:'catalog:greenhouse:7703668003',employer:'Axon',title:'Software Engineer I'});
  assert.equal(await catalogGroup.locator('[data-proposal-id="catalog-receipt"]').count(),1);
  await page.evaluate(()=>{
    window.installBundleFixture=(extra=[])=>{
      const evidence=[{...ref,source_id:'bundle',quote:'Thank you for applying. We received your application.'}];
      const link=proposal('bundle-link',{operation:'link_message',input:{message_id:'bundle',application_id:'app1'},evidence});
      const receipt=proposal('bundle-receipt',{operation:'record_submission_receipt',input:{application_id:'app1',evidence},evidence,
        dependencies:[link.id],prerequisites:[link]});
      groups=[{...evidenceGroup('email:bundle',[link,receipt,...extra.map(p=>({...p,dependencies:[link.id],prerequisites:[link]}))]),evidence}];
      actions=[];failCommand=false;conflict=false;location.hash='#review';
      return loadOwnerReviewQueue();
    };
    return installBundleFixture();
  });
  const bundled=page.locator('[data-review-key="evidence_group:email:bundle"]');
  assert.equal(await bundled.locator('[data-proposal-id]').count(),1);
  assert.equal(await bundled.locator('input[type=checkbox]').count(),0);
  assert.match(await bundled.innerText(),/1 update to review/);
  assert.match(await bundled.innerText(),/Also attaches this email/);
  assert.equal(await bundled.getByRole('button',{name:'Record receipt',exact:true}).count(),1);
  assert.equal(await bundled.getByRole('heading',{name:'Link this email',exact:true}).count(),0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.screenshot({path:'.cache/review-receipt-mobile.png',fullPage:true});
  await page.setViewportSize({width:1280,height:900});
  await page.screenshot({path:'.cache/review-receipt-desktop.png',fullPage:true});
  await page.evaluate(()=>{failCommand=true;});
  await bundled.getByRole('button',{name:'Record receipt',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  await page.evaluate(()=>{failCommand=false;});
  await bundled.getByRole('button',{name:'Record receipt',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const bundleCalls=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/review_changes')).slice(-2));
  assert.equal(bundleCalls[0].options.headers['Idempotency-Key'],bundleCalls[1].options.headers['Idempotency-Key']);
  assert.deepEqual(JSON.parse(bundleCalls[1].options.body),{group_id:'email:bundle',expected_digest:'digest-email:bundle',decisions:[
    {proposal_id:'bundle-receipt',expected_version:3,decision:'accept'},{proposal_id:'bundle-link',expected_version:3,decision:'accept'}]});
  // A shared link follows the accepted update; an independent request remains pending.
  await page.evaluate(()=>installBundleFixture([proposal('request',{operation:'record_request',input:{application_id:'app1',description:'Send availability',kind:'send_availability'}})]));
  await bundled.getByRole('button',{name:'Record receipt',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.equal(await bundled.getByRole('button',{name:'Add task',exact:true}).count(),1);
  assert.equal(await page.evaluate(()=>groups[0].proposals.length),1);
  await bundled.getByRole('button',{name:'Add task',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const requestOnly=await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path.endsWith('/review_changes')).at(-1).options.body));
  assert.deepEqual(requestOnly.decisions,[{proposal_id:'request',expected_version:3,decision:'accept'}]);
  // Dismissing a receipt cannot reject the link still needed by another update.
  await page.evaluate(()=>installBundleFixture([proposal('request',{operation:'record_request',input:{application_id:'app1',description:'Send availability'}})]));
  await bundled.locator('[data-proposal-id="bundle-receipt"]').getByRole('button',{name:'Dismiss update',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const dismissed=await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path.endsWith('/review_changes')).at(-1).options.body));
  assert.deepEqual(dismissed.decisions,[{proposal_id:'bundle-receipt',expected_version:3,decision:'reject'}]);
  assert.equal(await bundled.getByRole('button',{name:'Add task',exact:true}).count(),1);
  // Closure keeps its consequences visible, and a stale card blocks a second click.
  await page.evaluate(()=>installBundleFixture([proposal('closure',{operation:'close_application',input:{application_id:'app1',outcome:'rejected'},
    closure:{records:[{kind:'tasks',description:'Send availability'},{kind:'reminders',description:'Follow up'}]}})]));
  assert.match(await bundled.innerText(),/will cancel 2 open tasks/);
  await bundled.getByText('View affected work',{exact:true}).click();
  assert.match(await bundled.innerText(),/Send availability/);
  await page.evaluate(()=>{conflict=true;});
  await bundled.getByRole('button',{name:'Record rejection',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.equal(await bundled.getByRole('button',{name:'Record rejection',exact:true}).isDisabled(),true);
  // Wrong targets, unresolved identities, and non-link dependencies never get a compact bundle.
  await page.evaluate(async()=>{
    await installBundleFixture();groups[0].proposals[0].input.application_id='app2';return loadOwnerReviewQueue();
  });
  assert.equal(await bundled.getByRole('button',{name:'Record receipt',exact:true}).count(),0);
  await page.evaluate(async()=>{
    await installBundleFixture();groups[0].proposals[1].blockers=['needs_target'];return loadOwnerReviewQueue();
  });
  assert.equal(await bundled.getByRole('button',{name:'Record receipt',exact:true}).count(),0);
  await page.evaluate(async()=>{
    await installBundleFixture();groups[0].proposals[0].operation='create_task';return loadOwnerReviewQueue();
  });
  assert.equal(await bundled.getByRole('button',{name:'Record receipt',exact:true}).count(),0);
  // System-only evidence is absent from decisions and application review notices.
  await page.evaluate(()=>{
    conflict=false; groups=[{...evidenceGroup('email:system',[],[{...issue,status:'open',version:7}]),applications:[],application:null}]; actions=[];
    return loadOwnerReviewQueue();
  });
  assert.equal(await page.locator('#attention-list > article').count(),0);
  assert.match(await page.locator('#attention-list').innerText(),/Nothing needs review/);
  assert.equal(await page.evaluate(()=>ownerReviewCardItems().length),0);
  assert.equal(await page.locator('#review-count').innerText(),'1'); // one unloaded decision in fixture
  const systemOnly=page.locator('#owner-processing-section');
  await systemOnly.getByText('Resolve without retrying',{exact:true}).click();
  assert.equal(await systemOnly.getByRole('button',{name:'Resolve processing problem'}).isDisabled(),true);
  await systemOnly.getByLabel('Resolution',{exact:true}).selectOption('Already handled');
  assert.equal(await systemOnly.getByRole('button',{name:'Resolve processing problem'}).isDisabled(),false);
  await page.evaluate(()=>renderOwnerReviewQueue());
  assert.equal(await systemOnly.getByLabel('Resolution',{exact:true}).inputValue(),'Already handled');
  await systemOnly.getByRole('button',{name:'Resolve processing problem'}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.equal(await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path.endsWith('/resolve_processing')).at(-1).options.body).reason),'Already handled');
  // No candidate is selected implicitly. Search loads later pages, and refresh preserves choices.
  await page.evaluate(()=>{
    previewCorrections=true; pagedApplications=true;
    groups=[{...evidenceGroup('email:picker',[proposal('choose',{application_id:null,blockers:['needs_target']})]),application:null,applications:[],application_candidates:[app]}];
    return loadOwnerReviewQueue();
  });
  const picker=page.locator('[data-review-key="evidence_group:email:picker"]');
  assert.equal(await picker.getByRole('button',{name:'Continue',exact:true}).isDisabled(),true);
  assert.equal(await picker.locator('input[type=radio]:checked').count(),0);
  await picker.getByLabel('Search applications',{exact:true}).fill('Other');
  await picker.getByRole('radio',{name:'Other · Engineer',exact:true}).check();
  assert.equal(await page.evaluate(()=>document.activeElement?.type),'radio');
  await page.evaluate(()=>loadOwnerReviewQueue());
  assert.equal(await picker.getByLabel('Search applications',{exact:true}).inputValue(),'Other');
  assert.equal(await picker.getByRole('radio',{name:'Other · Engineer',exact:true}).isChecked(),true);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.screenshot({path:'.cache/review-application-picker-mobile.png',fullPage:true});
  await page.setViewportSize({width:1280,height:900});
  // A stale group saves the choice but requires refresh before preparing another preview.
  await page.evaluate(()=>{conflict=true;});
  await picker.getByRole('button',{name:'Continue',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.equal(await picker.getByRole('button',{name:'Continue',exact:true}).isDisabled(),true);
  assert.match(await page.locator('#review-feedback').innerText(),/Your selection is saved/);
  await page.evaluate(()=>{conflict=false;return loadOwnerReviewQueue();});
  // An uncertain request freezes the selection and retries the identical payload and key.
  await page.evaluate(()=>{failCommand=true;});
  await picker.getByRole('button',{name:'Continue',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  assert.equal(await picker.getByLabel('Search applications',{exact:true}).isDisabled(),true);
  await page.evaluate(()=>{failCommand=false;});
  await picker.getByRole('button',{name:'Retry same preview request',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy);
  const previewCalls=await page.evaluate(()=>requests.filter(r=>r.path.endsWith('/correct_review_target')).slice(-2));
  assert.equal(previewCalls[0].options.body,previewCalls[1].options.body);
  assert.equal(previewCalls[0].options.headers['Idempotency-Key'],previewCalls[1].options.headers['Idempotency-Key']);
  assert.equal(await picker.getByRole('button',{name:'Record receipt',exact:true}).count(),1);
  assert.match(await picker.innerText(),/Other · Engineer/);
  assert.equal(await page.evaluate(()=>groups[0].proposals.length),2); // preview is still pending
  // Accepted links cannot be silently moved by choosing another application.
  await page.evaluate(()=>{
    groups[0].association={application_id:'app1'}; return loadOwnerReviewQueue();
  });
  await picker.getByText('Change application',{exact:true}).click();
  assert.match(await picker.innerText(),/requires association correction/);
  assert.equal(await picker.getByRole('button',{name:'Continue',exact:true}).isDisabled(),true);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.deepEqual(errors,[]);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  console.log('owner grouped review browser checks passed');
} finally { await browser.close(); }
