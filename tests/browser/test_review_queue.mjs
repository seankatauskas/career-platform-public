import assert from 'node:assert/strict';
import {readFile,mkdir} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

// Isolated page-module fixture: no server, credentials, or external writes.
const browser = await chromium.launch({channel:'chromium', headless:true});
const page = await browser.newPage({viewport:{width:1280,height:900}});
page.setDefaultTimeout(10000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.setContent('<span id="review-count"></span><section id="attention"></section><section id="shortlist"></section><dialog id="job-preview"><header class="job-preview-header"><div><p id="job-preview-company"></p><h2 id="job-preview-title"></h2></div><button id="job-preview-close">Close</button></header><div id="job-preview-body"></div></dialog>');
  for (const file of ['styles.css','review-view.css']) await page.addStyleTag({content:await readFile(new URL(`../../job_search/web/${file}`,import.meta.url),'utf8')});
  await page.addScriptTag({content:`
    function node(tag, cls='', text='') { const el = document.createElement(tag); el.className = cls; el.textContent = text; return el; }
    function clear(el) { el.replaceChildren(); el.classList.remove('empty'); }
    const meta = values => node('p','meta',values.filter(Boolean).join(' · '));
    const displayDate = value => value || 'Not recorded';
    const postingDate = displayDate;
    const postingDates = () => node('p','posting-dates','Posted Sep 20, 2026');
    const jobSummary = item => node('div','job-summary',item.title);
    const applicationHref = (id,tab) => '#applications/'+id+'/'+tab;
    const key = prefix => prefix+'-fixture';
    const notice = () => {};
    const renderApplicationTable = () => {};
    const renderApplicationReviewNotices = () => {};
    const refreshApplicationWorkspace = async () => {};
    const loadApplications = async () => {};
    const consoleState = {view:'review',reviews:[],actions:[]};
    const state = {applications:[{application_id:'app1',employer_snapshot:'Example',title_snapshot:'Engineer'}]};
    window.requests = [];
    let attentionResponse = {items:[]}; let actionResponse = {actions:[]};
    let mailAnalyses = []; let mailDecisionConflict = false;
    let attentionFails = false;
    let archiveAnalysisFails = false; let archiveAnalysisItems = [];
    let archiveAnalysisGate = null; let releaseArchiveAnalysis;
    let archiveAnalysisActive = 0; let archiveAnalysisMaxActive = 0;
    async function api(path, options) {
      requests.push({path,options});
      if(path === '/api/v1/attention') { if (attentionFails) throw new Error('Fixture unavailable'); return attentionResponse; }
      if(path === '/api/v1/actions') return actionResponse;
      if(path === '/api/v1/mail/failures/analyze') {
        archiveAnalysisActive += 1;
        archiveAnalysisMaxActive = Math.max(archiveAnalysisMaxActive, archiveAnalysisActive);
        try {
          if (archiveAnalysisGate) await archiveAnalysisGate;
          await new Promise(resolve=>setTimeout(resolve,0));
          if (archiveAnalysisFails) throw new Error('Saved email analysis unavailable');
          attentionResponse = {items:archiveAnalysisItems};
          return {status:'proposal_created'};
        } finally { archiveAnalysisActive -= 1; }
      }
      if(path.startsWith('/api/v1/attention/message?')) return {subject:'Interview <script>not markup</script>',body:'First line\\n\\n'+'Full archived body '.repeat(200)+'\\nLast line',available:true,truncated:false};
      if(path.startsWith('/api/v1/mail-review/applications?')) return {applications:[{application_id:'older-app',employer_snapshot:'Older Company',title_snapshot:'Engineer'}],next_cursor:null};
      if(path === '/api/v1/mail-review/preview') {const decisions=JSON.parse(options.body).decisions;return {decisions,preview_hash:'preview-fixture',notice:'No mail is sent.',changes:decisions.map(d=>({proposal_id:d.proposal_id,decision:d.decision,application:'Example · Engineer',creates_application:!!d.new_application,event_type:d.event_type,from_phase:'preparing',to_phase:d.event_type==='rejection_received'?'terminal':'active',terminal_outcome:d.event_type==='rejection_received'?'rejected':null,next_step:d.task}))};}
      if(path === '/api/v1/mail-review/resolve') return {resolved:[]};
      if(path === '/api/v1/mail-analyses?history=true&limit=100') return {analyses:mailAnalyses.filter(item=>item.replay_id)};
      if(path.startsWith('/api/v1/mail-analyses/')) {
        const id=path.split('/')[4]; const found=mailAnalyses.find(item=>item.analysis_id===id);
        if(path.endsWith('/decisions')) { if(mailDecisionConflict) { const error=new Error('changed'); error.status=409; throw error; } return {...found,revision:'saved'}; }
        return found;
      }
      if(path === '/api/v1/curated-shortlists') return {lists:[]};
      if(path.includes('/jobs/preview')) return {job:{title:'Engineer',company:'Example',jobUrl:'https://example.test/job',location:'Remote',ranking_score:.987654,final_score:.876543,score_components:{sparse:.765432},explanation:{summary:'MODEL_DIAGNOSTIC_SENTINEL'}},description_html:'<h3>Responsibilities</h3><ul><li>Build useful things.</li></ul>'};
      return {};
    }
  `});
  for (const file of ['shortlist-view.js','review-view.js','job-preview.js','lifecycle-view.js']) await page.addScriptTag({content:await readFile(new URL(`../../job_search/web/${file}`,import.meta.url),'utf8')});
  // index.html loads the page modules before app.js defines this shared helper.
  // Do not let the fixture hide an eager dependency on that later script.
  assert.deepEqual(errors, []);
  await page.addScriptTag({content:'const $ = selector => document.querySelector(selector);'});
  await page.evaluate(async () => { attentionFails = true; await loadReviewQueue(); });
  assert.match(await page.locator('#attention-list').innerText(), /could not be fully loaded/);
  assert.match(await page.locator('#review-count').getAttribute('title'), /unavailable/);
  assert.equal(await page.locator('#review-count').innerText(), '—');
  await page.evaluate(async () => { attentionFails = false; await loadReviewQueue(); });
  assert.match(await page.locator('#attention-list').innerText(), /Nothing needs review/);
  assert.equal(await page.locator('#review-feedback').isVisible(), false);
  assert.equal(await page.locator('#review-count').getAttribute('title'), '');
  await page.evaluate(() => {
    const proposal = {id:'p1',kind:'event_proposal',status:'review',detail:'interview_requested',application_id:'app1',created_at:'2026-09-01',candidate_application_ids:['app2']};
    consoleState.reviews = [proposal,proposal,{...proposal,id:'completed',status:'accepted'},{id:'a1',kind:'action_proposal',status:'approval'}, {id:'t1',kind:'temporal_proposal',status:'review',detail:'deadline',due_at:'2026-10-01',created_at:'2026-09-03',application_id:'app1'}, {...proposal,id:'candidate',application_id:null,candidate_application_ids:['app1'],created_at:'2026-09-02'}];
    consoleState.actions = [{action_id:'a1',kind:'outlook_reply_draft',status:'pending',application_id:'app1',expires_at:'2026-09-30',created_at:'2026-09-03',payload:{subject:'Interview reply',body:'Thanks'},payload_sha256:'abc'}, {action_id:'a2',kind:'outlook_calendar_hold',status:'needs_reconciliation',created_at:'2026-09-20',payload:{}}, {action_id:'done',kind:'outlook_reply_draft',status:'executed',created_at:'2026-09-01',payload:{}}];
    renderReviewQueue();
  });
  assert.deepEqual(await page.evaluate(() => getReviewItems().map(item=>item.id)), ['a2','a1','t1','p1','candidate']);
  assert.equal(await page.locator('#review-count').innerText(), '5');
  assert.equal(await page.locator('#attention-list > article').count(), 5);
  assert.equal(await page.locator('#action-list > article').count(), 1);
  assert.equal(await page.evaluate(()=>reviewItemsForApplication('app1').length), 4);
  assert.equal(await page.evaluate(()=>reviewItemsForApplication('app2').length), 0);
  const messagePanel = page.locator('[data-review-key="event_proposal:p1"] .review-message');
  assert.equal(await messagePanel.getAttribute('open'), null);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path.startsWith('/api/v1/attention/message?')).length), 0);
  await messagePanel.locator('summary').click();
  await messagePanel.locator('.message-body').waitFor();
  assert.equal(await messagePanel.locator('.message-subject').textContent(), 'Interview <script>not markup</script>');
  assert.equal(await messagePanel.locator('script').count(), 0);
  assert.equal(await messagePanel.locator('.message-body').textContent(), 'First line\n\n'+'Full archived body '.repeat(200)+'\nLast line');
  await page.evaluate(()=>renderReviewQueue());
  assert.notEqual(await messagePanel.getAttribute('open'), null);
  await messagePanel.locator('.message-body').waitFor();
  const draftPanel = page.locator('[data-review-key="outlook_reply_draft:a1"] .review-message');
  await draftPanel.locator('summary').click();
  assert.equal(await draftPanel.locator('.message-subject').textContent(), 'Interview reply');
  assert.equal(await draftPanel.locator('.message-body').textContent(), 'Thanks');
  await page.evaluate(() => { location.hash='#review/event_proposal/p1'; $('#attention').style.marginTop='2000px'; renderReviewQueue(); });
  await page.waitForFunction(() => document.activeElement?.dataset.reviewKey === 'event_proposal:p1');
  assert(await page.evaluate(() => scrollY > 1000));
  await page.evaluate(() => { $('#attention').style.marginTop=''; location.hash='#review'; renderReviewQueue(); });
  assert.equal(await page.getByRole('button',{name:'Record interview request',exact:true}).count(),2);
  assert.equal(await page.getByRole('button',{name:'Accept',exact:true}).count(),0);
  const approve = page.getByRole('button',{name:'Create Outlook draft',exact:true});
  await approve.click();
  const decision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/actions/a1/decision'));
  assert.deepEqual(JSON.parse(decision.options.body),{idempotency_key:'action-fixture',approve:true,payload_sha256:'abc'});
  assert.equal(await page.locator('#review-count').innerText(),'');
  await page.evaluate(()=>{ consoleState.reviews=[{id:'p2',kind:'event_proposal',status:'pending',detail:'interview_requested',application_id:'app1',candidate_application_ids:[]}]; renderReviewQueue(); });
  await page.getByRole('button',{name:'Record interview request',exact:true}).click();
  const proposalDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/p2/decision'));
  assert.equal(JSON.parse(proposalDecision.options.body).decision,'accepted');
  await page.evaluate(()=>{ consoleState.reviews=[{id:'late',kind:'event_proposal',status:'pending',detail:'submission_confirmed',application_id:null,candidate_application_ids:[]}]; renderReviewQueue(); });
  assert.equal(await page.getByRole('button',{name:'Confirm application received',exact:true}).isDisabled(),true);
  assert.match(await page.locator('#attention-list').innerText(),/No matching application yet/);
  await page.evaluate(()=>{ consoleState.reviews[0].candidate_application_ids=['app1']; renderReviewQueue(); });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Confirm application received',exact:true}).isDisabled(),true);
  await page.getByRole('combobox',{name:'Application for this proposal'}).selectOption('app1');
  assert.equal(await page.getByRole('button',{name:'Confirm application received',exact:true}).isEnabled(),true);
  await page.getByRole('button',{name:'Confirm application received',exact:true}).click();
  const lateDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/late/decision'));
  assert.equal(JSON.parse(lateDecision.options.body).selected_application_id,'app1');
  // Suggestions come from the server's subject/body match, including applications
  // outside the currently loaded page of the application table.
  await page.evaluate(() => {
    consoleState.reviews = [{id:'recommended',kind:'event_proposal',status:'review',detail:'rejected',
      application_id:null,candidate_application_ids:['app1','app2'],
      suggested_resolution:{action:'accept',label:'Record rejection',application_id:'app2',confidence:'high',
        explanation:'The email subject names Other Company and the body names Data Engineer.',requires_selection:false},
      application_matches:[{application_id:'app2',employer:'Other Company',title:'Data Engineer',confidence:'high',explanation:'Employer and role match.'},
        {application_id:'app1',employer:'Example',title:'Engineer',confidence:'low',explanation:'Role only.'}]}];
    renderReviewQueue();
  });
  const recommendedCard = page.locator('[data-review-key="event_proposal:recommended"]');
  const applicationChoice = recommendedCard.getByRole('combobox',{name:'Application for this proposal'});
  assert.equal(await applicationChoice.inputValue(),'app2');
  assert.match(await applicationChoice.locator('option:checked').innerText(),/Other Company.*Data Engineer/);
  assert.match(await recommendedCard.innerText(),/email subject names Other Company/);
  assert.equal(await recommendedCard.getByRole('button',{name:'Record rejection',exact:true}).isEnabled(),true);
  const postsBeforeOverride = await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length);
  await applicationChoice.selectOption('app1');
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await applicationChoice.inputValue(),'app1');
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length),postsBeforeOverride);
  await recommendedCard.getByRole('button',{name:'Record rejection',exact:true}).click();
  const overrideDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/recommended/decision'));
  assert.equal(JSON.parse(overrideDecision.options.body).selected_application_id,'app1');

  await page.evaluate(() => {
    consoleState.reviews = [{id:'offer',kind:'event_proposal',status:'review',detail:'offer_received',
      application_id:null,candidate_application_ids:['app1'],
      suggested_resolution:{action:'accept',label:'Record offer',application_id:'app1',confidence:'high',explanation:'Employer and title are in the email.',requires_selection:false}}];
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'app1');
  await page.getByRole('button',{name:'Record offer',exact:true}).click();
  const offerDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/offer/decision'));
  assert.equal(JSON.parse(offerDecision.options.body).selected_application_id,'app1');

  // A suggestion that still requires selection must not silently choose an app.
  await page.evaluate(() => {
    consoleState.reviews = [{id:'ambiguous',kind:'event_proposal',status:'review',detail:'interview_requested',
      application_id:null,candidate_application_ids:['app1','app2'],
      suggested_resolution:{action:'review',label:'Choose an application',application_id:'app1',confidence:'low',
        explanation:'Two applications match the same employer.',requires_selection:true}}];
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Record interview request',exact:true}).isDisabled(),true);
  await page.getByRole('combobox',{name:'Application for this proposal'}).selectOption('app2');
  assert.equal(await page.getByRole('button',{name:'Record interview request',exact:true}).isEnabled(),true);
  await page.getByRole('combobox',{name:'Application for this proposal'}).selectOption('');
  assert.equal(await page.getByRole('button',{name:'Record interview request',exact:true}).isDisabled(),true);
  await page.evaluate(() => {
    // A stale recommendation cannot become a valid selection by itself.
    consoleState.reviews[0].suggested_resolution = {action:'accept',label:'Record interview request',application_id:'missing',confidence:'high',requires_selection:false};
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Record interview request',exact:true}).isDisabled(),true);

  await page.evaluate(() => {
    consoleState.reviews = [{id:'discovery',kind:'mail_discovery',status:'review',
      candidate_application_ids:['app1','app2'],
      proposal:{employer:'Other Company',title:'Data Engineer',observation:{subject:'Your Data Engineer application',sender:'recruiter@example.test',direction:'inbound'}},
      suggested_resolution:{action:'link',label:'Link application',application_id:'app2',confidence:'high',explanation:'The subject and email body name this role.',requires_selection:false},
      application_matches:[{application_id:'app2',employer:'Other Company',title:'Data Engineer',confidence:'high',explanation:'Employer and role match.'}]}];
    renderReviewQueue();
  });
  const discoveryCard = page.locator('[data-review-key="mail_discovery:discovery"]');
  const discoveryChoice = discoveryCard.getByRole('combobox',{name:'Or link existing application'});
  assert.equal(await discoveryChoice.inputValue(),'app2');
  assert.match(await discoveryChoice.locator('option:checked').innerText(),/Other Company.*Data Engineer/);
  await discoveryChoice.selectOption('app1');
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await discoveryChoice.inputValue(),'app1');
  await discoveryCard.getByRole('button',{name:'Link application',exact:true}).click();
  const linkDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/lifecycle/discoveries/decide'));
  assert.equal(JSON.parse(linkDecision.options.body).decision,'link');
  assert.equal(JSON.parse(linkDecision.options.body).application_id,'app1');
  await page.evaluate(() => {
    consoleState.reviews = [{id:'unmatched-discovery',kind:'mail_discovery',status:'review',
      proposal:{employer:'New Employer',title:'Designer',observation:{subject:'Designer role',direction:'inbound'}},
      suggested_resolution:{action:'review',label:'Review application details',application_id:null,confidence:'low',explanation:'No tracked application matches.',requires_selection:true},application_matches:[]}];
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('combobox',{name:'Or link existing application'}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Link application',exact:true}).isDisabled(),true);
  assert.equal(await page.getByRole('button',{name:'Create application record',exact:true}).isEnabled(),true);
  // A strong subject/body recommendation can correct an earlier association.
  await page.evaluate(() => {
    consoleState.reviews = [{id:'assigned',kind:'event_proposal',status:'review',detail:'submission_confirmed',
      application_id:'app1',candidate_application_ids:['app2'],
      suggested_resolution:{action:'accept',label:'Confirm application received',application_id:'app2',confidence:'high',requires_selection:false}}];
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'app2');
  await page.getByRole('button',{name:'Confirm application received',exact:true}).click();
  const assignedDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/assigned/decision'));
  assert.equal(JSON.parse(assignedDecision.options.body).selected_application_id,'app2');

  await page.evaluate(() => {
    consoleState.reviews = [{id:'conflicting-assignment',kind:'event_proposal',status:'review',detail:'submission_confirmed',
      application_id:'app1',candidate_application_ids:['app1'],
      suggested_resolution:{action:'review',label:'Choose an application',application_id:null,confidence:'low',requires_selection:true,
        explanation:'The email body conflicts with the previous association.'}}];
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Confirm application received',exact:true}).isDisabled(),true);
  await page.getByRole('combobox',{name:'Application for this proposal'}).selectOption('app1');
  assert.equal(await page.getByRole('button',{name:'Confirm application received',exact:true}).isEnabled(),true);

  await page.evaluate(() => {
    consoleState.reviews = [{id:'failure',kind:'mail_processing_failure',status:'failed',can_retry:false,
      account_id:'fixture-account',folder_ref:'inbox',query_version:'v1'}];
    renderReviewQueue();
  });
  assert.equal(await page.getByRole('button',{name:'Retry processing',exact:true}).isDisabled(),true);
  assert.equal(await page.locator('.review-suggested-action').innerText(),'Dismiss');
  await page.getByRole('button',{name:'Dismiss',exact:true}).click();
  const dismissDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/mail/failures/resolve'));
  assert.equal(JSON.parse(dismissDecision.options.body).action,'dismiss');
  assert.equal(JSON.parse(dismissDecision.options.body).message_id,'failure');
  // A catalog-only recommendation creates its application only on confirmation.
  const beforeCatalog = await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length);
  await page.evaluate(() => {
    const job={ats:'greenhouse',id:'job:42',company:'Catalog Company',title:'Data Engineer',jobUrl:'https://example.test/job'};
    consoleState.reviews=[{id:'catalog-event',kind:'event_proposal',status:'review',detail:'rejected',candidate_application_ids:[],
      job_matches:[job],suggested_resolution:{action:'accept',label:'Record rejection',application_id:null,job,requires_selection:false,
        explanation:'Employer and role match this collected posting.'}}];
    renderReviewQueue();
  });
  const catalogChoice=page.getByRole('combobox',{name:'Application for this proposal'});
  assert.equal(await catalogChoice.inputValue(),'job:greenhouse:job:42');
  assert.match(await page.locator('.review-suggestion').innerText(),/Likely job[\s\S]*Catalog Company/);
  assert.match(await page.locator('.review-suggestion').innerText(),/creates (?:an|the) application record/);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length),beforeCatalog);
  await page.getByRole('button',{name:'Record rejection',exact:true}).click();
  const catalogDecision=await page.evaluate(()=>JSON.parse(requests.find(r=>r.path==='/api/v1/proposals/catalog-event/decision').options.body));
  assert.deepEqual(catalogDecision.selected_job,{ats:'greenhouse',id:'job:42'});
  assert.equal('selected_application_id' in catalogDecision,false);

  await page.evaluate(() => {
    const job={ats:'ashby',id:'catalog-override',company:'Catalog Company',title:'Engineer'};
    consoleState.reviews=[{id:'catalog-override',kind:'event_proposal',status:'review',detail:'submission_confirmed',candidate_application_ids:['app1'],
      job_matches:[job],suggested_resolution:{action:'accept',label:'Confirm application received',application_id:null,job,requires_selection:false}}];
    renderReviewQueue();
  });
  await catalogChoice.selectOption('app1');
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await catalogChoice.inputValue(),'app1');
  await page.getByRole('button',{name:'Confirm application received',exact:true}).click();
  const catalogOverride=await page.evaluate(()=>JSON.parse(requests.find(r=>r.path==='/api/v1/proposals/catalog-override/decision').options.body));
  assert.equal(catalogOverride.selected_application_id,'app1');
  assert.equal('selected_job' in catalogOverride,false);

  await page.evaluate(() => {
    const job={ats:'lever',id:'discovery-job',company:'Catalog Company',title:'Designer'};
    consoleState.reviews=[{id:'catalog-discovery',kind:'mail_discovery',status:'review',
      proposal:{employer:'Catalog Company',title:'Designer',observation:{subject:'Your Designer role',direction:'inbound'}},
      job_matches:[job],suggested_resolution:{action:'link_job',label:'Link job and create record',application_id:null,job,requires_selection:false}}];
    renderReviewQueue();
  });
  const catalogDiscovery=page.getByRole('combobox',{name:'Or link existing application'});
  assert.equal(await catalogDiscovery.inputValue(),'job:lever:discovery-job');
  assert.equal(await page.getByRole('button',{name:'Link job and create record',exact:true}).isEnabled(),true);
  await catalogDiscovery.selectOption('app1');
  assert.equal(await page.getByRole('button',{name:'Link application',exact:true}).isEnabled(),true);
  await catalogDiscovery.selectOption('job:lever:discovery-job');
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await catalogDiscovery.inputValue(),'job:lever:discovery-job');
  await page.getByRole('button',{name:'Link job and create record',exact:true}).click();
  const catalogLink=await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path==='/api/v1/lifecycle/discoveries/decide').at(-1).options.body));
  assert.equal(catalogLink.decision,'link_job');
  assert.deepEqual(catalogLink.selected_job,{ats:'lever',id:'discovery-job'});
  assert.equal('application_id' in catalogLink,false);

  await page.evaluate(() => {
    const job={ats:'ashby',id:'rejected-job',company:'Catalog Company',title:'Engineer'};
    consoleState.reviews=[{id:'reject-catalog-match',kind:'event_proposal',status:'review',detail:'submission_confirmed',candidate_application_ids:[],
      job_matches:[job],suggested_resolution:{action:'accept',application_id:null,job,requires_selection:false}}];
    renderReviewQueue();
  });
  await page.getByRole('button',{name:'Reject',exact:true}).click();
  const rejectCatalog=await page.evaluate(()=>JSON.parse(requests.find(r=>r.path==='/api/v1/proposals/reject-catalog-match/decision').options.body));
  assert.equal(rejectCatalog.decision,'rejected');
  assert.equal('selected_job' in rejectCatalog,false);
  assert.equal(rejectCatalog.selected_application_id,null);

  // Prefer a recommended existing application when both forms are present.
  await page.evaluate(() => {
    const job={ats:'ashby',id:'already-tracked',company:'Example',title:'Engineer'};
    consoleState.reviews=[{id:'existing-preferred',kind:'event_proposal',status:'review',detail:'interview_requested',candidate_application_ids:['app1'],
      job_matches:[job],suggested_resolution:{action:'accept',label:'Record interview request',application_id:'app1',job,requires_selection:false}}];
    renderReviewQueue();
  });
  assert.equal(await catalogChoice.inputValue(),'app1');
  await page.evaluate(()=>{consoleState.reviews[0].suggested_resolution.requires_selection=true;renderReviewQueue();});
  assert.equal(await catalogChoice.inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Record interview request',exact:true}).isDisabled(),true);
  // Preparing an archived Graph failure exposes a proposed update automatically,
  // while recording that update still requires the user's action.
  const archivePostsBefore=await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length);
  await page.evaluate(async () => {
    const job={ats:'greenhouse',id:'archive-role',company:'Fixture Finance',title:'Backend Engineer'};
    attentionResponse={items:[{id:'archive-success',kind:'mail_processing_failure',status:'failed',can_retry:false,
      can_analyze_archive:true,account_id:'fixture-account',folder_ref:'inbox',query_version:'archive-v1',
      error:'Graph request failed (400)'}]};
    archiveAnalysisItems=[{id:'archive-proposal',kind:'event_proposal',status:'review',detail:'submission_confirmed',
      candidate_application_ids:[],job_matches:[job],suggested_resolution:{action:'accept',label:'Confirm application received',
        application_id:null,job,requires_selection:false,explanation:'The archived subject and body identify this role.'}}];
    await loadReviewQueue();
  });
  await page.locator('[data-review-key="event_proposal:archive-proposal"]').waitFor();
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'job:greenhouse:archive-role');
  assert.equal(await page.getByRole('button',{name:'Confirm application received',exact:true}).isEnabled(),true);
  const archivePreparation=await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze'));
  assert.equal(archivePreparation.length,1);
  const archiveBody=JSON.parse(archivePreparation[0].options.body);
  assert.equal(archiveBody.account_id,'fixture-account');
  assert.equal(archiveBody.folder_ref,'inbox');
  assert.equal(archiveBody.query_version,'archive-v1');
  assert.equal(archiveBody.message_id,'archive-success');
  assert.equal(typeof archiveBody.idempotency_key,'string');
  assert(archiveBody.idempotency_key.length>0);
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.options?.method==='POST').length),archivePostsBefore+1);
  assert.equal(await page.evaluate(()=>requests.some(r=>r.path==='/api/v1/proposals/archive-proposal/decision')),false);
  await page.getByRole('button',{name:'Confirm application received',exact:true}).click();
  const archiveConfirmation=await page.evaluate(()=>JSON.parse(requests.find(r=>r.path==='/api/v1/proposals/archive-proposal/decision').options.body));
  assert.deepEqual(archiveConfirmation.selected_job,{ats:'greenhouse',id:'archive-role'});

  // A preparation error keeps the review usable and never retries on every render.
  await page.evaluate(async () => {
    archiveAnalysisFails=true;
    attentionResponse={items:[{id:'archive-error',kind:'mail_processing_failure',status:'failed',can_retry:true,
      can_analyze_archive:true,account_id:'fixture-account',folder_ref:'inbox',query_version:'archive-v1'}]};
    await loadReviewQueue();
    await queueReviewFailureAnalysis();
  });
  await page.getByRole('button',{name:'Retry processing',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Retry processing',exact:true}).isEnabled(),true);
  assert.equal(await page.getByRole('button',{name:'Dismiss',exact:true}).isEnabled(),true);
  const failedPreparationCount=await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id==='archive-error').length);
  assert.equal(failedPreparationCount,1);
  await page.evaluate(async ()=>{renderReviewQueue();await Promise.all([loadReviewQueue(),loadReviewQueue()]);renderReviewQueue();});
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id==='archive-error').length),failedPreparationCount);
  assert.equal(await page.getByRole('button',{name:'Dismiss',exact:true}).isEnabled(),true);
  await page.evaluate(()=>{archiveAnalysisFails=false;archiveAnalysisItems=[];});
  await page.getByRole('button',{name:'Retry suggested action',exact:true}).click();
  await page.waitForFunction(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id==='archive-error').length===2);
  await page.evaluate(async()=>{await queueReviewFailureAnalysis();attentionResponse={items:[]};});
  await page.evaluate(async()=>{
    archiveAnalysisFails=true;
    attentionResponse={items:['inbox','archive'].map(folder_ref=>({id:'same-message-id',kind:'mail_processing_failure',status:'failed',
      can_analyze_archive:true,can_retry:true,account_id:'fixture-account',folder_ref,query_version:'archive-v1'}))};
    await loadReviewQueue();
    await queueReviewFailureAnalysis();
  });
  const scopedAnalyses=await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id==='same-message-id').map(r=>JSON.parse(r.options.body)));
  assert.deepEqual(scopedAnalyses.map(body=>body.folder_ref).sort(),['archive','inbox']);
  await page.evaluate(async()=>{
    attentionResponse={items:[0,1,2,3].map(index=>({id:'bounded-analysis-'+index,kind:'mail_processing_failure',status:'failed',
      can_analyze_archive:true,can_retry:true,account_id:'fixture-account',folder_ref:'inbox',query_version:'archive-v1'}))};
    await loadReviewQueue();
    await reviewFailureAnalysisTask;
  });
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id.startsWith('bounded-analysis-')).length),4);
  assert.equal(await page.evaluate(()=>archiveAnalysisMaxActive),1);
  await page.evaluate(async()=>{await loadReviewQueue();await reviewFailureAnalysisTask;});
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id.startsWith('bounded-analysis-')).length),4);
  // Leaving Review pauses queued work after the current request; returning resumes.
  await page.evaluate(async()=>{
    archiveAnalysisGate=new Promise(resolve=>{releaseArchiveAnalysis=resolve;});
    attentionResponse={items:[0,1].map(index=>({id:'navigation-analysis-'+index,kind:'mail_processing_failure',status:'failed',
      can_analyze_archive:true,can_retry:true,account_id:'fixture-account',folder_ref:'inbox',query_version:'archive-v1'}))};
    await loadReviewQueue();
  });
  await page.waitForFunction(()=>requests.some(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id==='navigation-analysis-0'));
  await page.evaluate(async()=>{consoleState.view='applications';releaseArchiveAnalysis();await reviewFailureAnalysisTask;});
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id.startsWith('navigation-analysis-')).length),1);
  await page.evaluate(async()=>{consoleState.view='review';archiveAnalysisGate=null;await loadReviewQueue();await reviewFailureAnalysisTask;});
  assert.equal(await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail/failures/analyze' && JSON.parse(r.options.body).message_id.startsWith('navigation-analysis-')).length),2);
  assert.equal(await page.evaluate(()=>archiveAnalysisMaxActive),1);
  await page.evaluate(()=>{attentionResponse={items:[]};archiveAnalysisFails=false;});
  await page.evaluate(() => { consoleState.reviews=[]; renderReviewQueue(); });
  // Exercise the real main-column width: medium screens retain the sidebar.
  // Long unmatched explanations and selected roles must not squeeze evidence
  // into a narrow column or overlap the separate action footer.
  await page.evaluate(()=>{
    const main=document.createElement('main');
    $('#attention').before(main);main.append($('#attention'));
    const job={ats:'greenhouse',id:'layout-role',company:'Northstar International Financial Technology and Services',
      title:'Senior Staff Software Engineer, Distributed Infrastructure and Enterprise Financial Data Platforms'};
    consoleState.reviews=[
      {id:'layout-unmatched',kind:'event_proposal',status:'review',detail:'submission_confirmed',subject:'Thank you for your application to our distributed infrastructure engineering team',
        candidate_application_ids:[],evidence_quote:'We have received your application and our team will review your experience.',
        suggested_resolution:{action:'review',application_id:null,requires_selection:true,explanation:'The email identifies an employer, but none of the saved applications or collected roles matches this conversation. Review the email and choose the correct application before confirming.'}},
      {id:'layout-matched',kind:'event_proposal',status:'review',detail:'interview_requested',subject:'Interview invitation for the enterprise financial data platforms engineering position',
        candidate_application_ids:['app1'],job_matches:[job],evidence_quote:'Please choose an interview time to discuss your distributed systems experience.',
        suggested_resolution:{action:'accept',label:'Record interview request',application_id:null,job,requires_selection:false,
          explanation:'The employer and role in the subject and body match this collected posting.'}}
    ];
    renderReviewQueue();
  });
  await mkdir(new URL('../../.cache/review-layout/',import.meta.url),{recursive:true});
  for(const width of [390,900,1024,1440]) {
    await page.setViewportSize({width,height:1000});
    const dimensions=await page.locator('#attention-list > article').evaluateAll(cards=>cards.map(card=>{
      const content=card.querySelector('.review-card-content');
      const actions=card.querySelector('.review-card-actions');
      if(!content || !actions) return {hasStructure:false};
      const contentBox=content.getBoundingClientRect(),actionsBox=actions.getBoundingClientRect();
      const style=getComputedStyle(card);
      const inner=card.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
      const controls=[...actions.children].map(el=>el.getBoundingClientRect());
      return {hasStructure:true,contentWidth:contentBox.width,inner,actionGap:actionsBox.top-contentBox.bottom,
        cardOverflow:card.scrollWidth-card.clientWidth,paragraphActions:actions.querySelectorAll('p,select').length,
        controlsInside:controls.every(box=>box.left>=actionsBox.left-1 && box.right<=actionsBox.right+1),
        noControlOverlap:controls.every((a,index)=>controls.slice(index+1).every(b=>a.right<=b.left || b.right<=a.left || a.bottom<=b.top || b.bottom<=a.top))};
    }));
    for(const measure of dimensions) {
      assert.equal(measure.hasStructure,true,`Review content/footer structure at ${width}px`);
      assert(measure.contentWidth>=measure.inner*.95,`Evidence retains the full content width at ${width}px: ${JSON.stringify(measure)}`);
      assert(measure.actionGap>=12,`Actions are separated from content at ${width}px`);
      assert(measure.cardOverflow<=1,`Card overflow at ${width}px`);
      assert.equal(measure.paragraphActions,0);
      assert.equal(measure.controlsInside,true);
      assert.equal(measure.noControlOverlap,true);
    }
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`Page overflow at ${width}px`);
    const choice=page.getByRole('combobox',{name:'Application for this proposal'});
    assert.equal(await choice.inputValue(),'job:greenhouse:layout-role');
    assert(await choice.evaluate(el=>el.getBoundingClientRect().width>=Math.min(280,el.parentElement.clientWidth*.9)),`Usable selector width at ${width}px`);
    assert.equal(await page.locator('[data-review-key="event_proposal:layout-unmatched"]').getByRole('button',{name:'Confirm application received',exact:true}).isDisabled(),true);
    await page.locator('#attention').screenshot({path:new URL(`../../.cache/review-layout/review-${width}.png`,import.meta.url).pathname});
  }
  await page.getByRole('combobox',{name:'Application for this proposal'}).selectOption('app1');
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'app1');
  await page.evaluate(()=>{const main=$('#attention').parentElement;main.replaceWith($('#attention'));consoleState.reviews=[];renderReviewQueue();});
  await page.setViewportSize({width:1280,height:900});
  {
  await page.evaluate(()=>{ consoleState.reviews=[{id:'manual-p2',kind:'event_proposal',status:'pending',detail:'interview_requested',application_id:'app1',candidate_application_ids:[]}]; renderReviewQueue(); });
  await page.locator('#attention-list .review-message > summary').click();
  await page.waitForFunction(()=>document.querySelector('#attention-list .message-body')?.textContent.endsWith('Last line'));
  assert.match(await page.locator('#attention-list .message-subject').innerText(), /<script>not markup<\/script>/);
  assert.equal(await page.locator('#attention-list .review-message script').count(), 0);
  assert((await page.locator('#attention-list .message-body').innerText()).length > 2048);
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await page.locator('#attention-list .review-message').evaluate(el=>el.open), true);
  await page.waitForFunction(()=>document.querySelector('#attention-list .message-body')?.textContent.endsWith('Last line'));
  await page.getByRole('button',{name:'Resolve email',exact:true}).click();
  await page.getByRole('button',{name:'Preview resolution',exact:true}).click();
  await page.getByRole('button',{name:'Save resolution',exact:true}).click();
  const proposalDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/mail-review/resolve'));
  assert.equal(JSON.parse(proposalDecision.options.body).decisions[0].event_type,'interview_requested');
  assert.equal(JSON.parse(proposalDecision.options.body).preview_hash,'preview-fixture');
  await page.evaluate(()=>{ consoleState.reviews=[{id:'late',kind:'event_proposal',status:'pending',detail:'submission_confirmed',evidence_quote:'Thanks for applying.',application_id:null,candidate_application_ids:[]}]; renderReviewQueue(); });
  await page.getByRole('button',{name:'Resolve email',exact:true}).click();
  assert.equal(await page.getByRole('combobox',{name:'Application',exact:true}).inputValue(),'');
  await page.getByRole('button',{name:'Preview resolution',exact:true}).click();
  assert.match(await page.locator('.mail-resolution .notice[role=status]').innerText(),/Choose an application/);
  await page.getByRole('searchbox',{name:'Find an application'}).fill('Older Company');
  await page.waitForFunction(()=>[...document.querySelectorAll('.mail-resolution option')].some(el=>el.value==='older-app'));
  await page.getByRole('combobox',{name:'Application',exact:true}).selectOption('older-app');
  assert.equal(await page.getByRole('combobox',{name:'Application',exact:true}).inputValue(),'older-app');
  await page.getByRole('searchbox',{name:'Find an application'}).fill('Example');
  await page.getByRole('combobox',{name:'Application',exact:true}).selectOption('app1');
  await page.getByRole('textbox',{name:'Review note (optional)'}).fill('I checked the employer.');
  await page.evaluate(()=>renderReviewQueue());
  assert.equal(await page.getByRole('textbox',{name:'Review note (optional)'}).inputValue(),'I checked the employer.');
  await page.getByRole('button',{name:'Preview resolution',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'Save resolution',exact:true}).isVisible(),true);
  await page.getByRole('combobox',{name:'What does the email mean?'}).selectOption('rejection_received');
  await page.getByRole('textbox',{name:'Supporting words from the email'}).fill('We will not proceed to interview.');
  assert.equal(await page.getByRole('button',{name:'Save resolution',exact:true}).isVisible(),false);
  await page.getByRole('button',{name:'Preview resolution',exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
  await mkdir(new URL('../../.cache/mail-review-ui/',import.meta.url),{recursive:true});
  await page.locator('#attention').screenshot({path:new URL('../../.cache/mail-review-ui/mobile.png',import.meta.url).pathname});
  await page.setViewportSize({width:1280,height:900});
  await page.locator('#attention').screenshot({path:new URL('../../.cache/mail-review-ui/desktop.png',import.meta.url).pathname});
  await page.getByRole('button',{name:'Save resolution',exact:true}).click();
  const lateDecision = await page.evaluate(()=>requests.filter(r=>r.path==='/api/v1/mail-review/resolve').at(-1));
  assert.equal(JSON.parse(lateDecision.options.body).decisions[0].application_id,'app1');
  assert.equal(JSON.parse(lateDecision.options.body).decisions[0].event_type,'rejection_received');
  // One message has independent findings; legacy projections and history do not inflate current review.
  await page.evaluate(()=>{
    location.hash='#review';
    const evidence=[{source_id:'current',quote:'<script>not executable</script> Please complete the assessment.',start:0,end:61}];
    const analysis={analysis_id:'mail1',revision:'r1',mode:'shared',subject:'Application and assessment',created_at:'2026-10-01',application_id:'app1',candidate_application_ids:['app1'],coverage:[{source_id:'attachment',reason:'attachment_unavailable'}],findings:[
      {finding_id:'receipt',type:'event',status:'pending',value:{application_id:'app1',confidence:.98,event_type:'submission_confirmed',evidence},projection:{kind:'event_proposal',id:'receipt-proposal'}},
      {finding_id:'action',type:'action',status:'pending',value:{application_id:'app1',confidence:.98,kind:'complete_assessment',description:'Complete the assessment',actor:'applicant',obligation:'required',channel:'portal',temporal_index:null,evidence}},
      {finding_id:'unclear',type:'uncertainty',status:'pending',value:{reason:'missing_attachment',description:'Instructions are missing',finding_type:'action',finding_index:0}}
    ]};
    mailAnalyses=[analysis,{...analysis,analysis_id:'historic',mode:'replay',replay_id:'replay-1'}];
    consoleState.reviews=[{kind:'mail_analysis',id:'mail1',status:'review',analysis},{kind:'event_proposal',id:'receipt-proposal',status:'pending'},
      {kind:'mail_analysis',id:'historic',status:'review',analysis:mailAnalyses[1]}, {kind:'mail_analysis',id:'shadow',status:'review',analysis:{...analysis,mode:'shadow'}}];
    consoleState.actions=[]; renderReviewQueue();
  });
  assert.equal(await page.locator('#attention-list > article').count(),1);
  assert.equal(await page.locator('#review-count').innerText(),'1');
  assert.equal(await page.locator('.mail-analysis script').count(),0);
  assert.match(await page.locator('.mail-analysis').innerText(),/Coverage: attachment unavailable/);
  assert.equal(await page.getByRole('button',{name:'Save selected decisions'}).isDisabled(),true);
  await page.getByRole('combobox',{name:'Decision for Application confirmation',exact:true}).selectOption('accepted');
  await page.getByRole('combobox',{name:'Decision for Complete the assessment',exact:true}).selectOption('rejected');
  await page.getByRole('button',{name:'Save selected decisions'}).click();
  const mailDecision=await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/mail-analyses/mail1/decisions'));
  const mailBody=JSON.parse(mailDecision.options.body);
  assert.equal(mailBody.revision,'r1');
  assert.deepEqual(mailBody.decisions.map(f=>[f.finding_id,f.decision]),[['receipt','accepted'],['action','rejected']]);
  assert.equal(mailDecision.options.headers['Idempotency-Key'],'mail-review-fixture');
  await page.locator('#mail-review-history > summary').click();
  await page.getByRole('button',{name:'Load email history'}).click();
  assert.equal(await page.locator('#mail-history-list > article').count(),1);
  assert.match(await page.locator('#mail-history-list').innerText(),/Historical reprocessing/);
  assert.equal(await page.locator('#review-count').innerText(),'');
  // A stale batch is never retried as if it had succeeded. Refresh retrieves its new revision.
  await page.evaluate(()=>{ mailDecisionConflict=true; consoleState.reviews=[{kind:'mail_analysis',id:'mail1',status:'review',analysis:mailAnalyses[0]}]; renderReviewQueue(); });
  const currentMail=page.locator('#attention-list .mail-analysis');
  await currentMail.getByRole('combobox',{name:'Decision for Complete the assessment',exact:true}).selectOption('accepted');
  await currentMail.locator('[data-finding-id="action"] summary').click();
  await currentMail.getByRole('textbox',{name:'Requested action',exact:true}).fill('Complete the coding exercise');
  await currentMail.getByRole('button',{name:'Save selected decisions'}).click();
  assert.match(await currentMail.innerText(),/no decisions from this batch were saved/);
  const corrected=await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path==='/api/v1/mail-analyses/mail1/decisions').at(-1).options.body));
  assert.equal(corrected.decisions[0].replacement.description,'Complete the coding exercise');
  assert.equal(corrected.decisions[0].replacement.evidence[0].source_id,'current');
  await currentMail.getByRole('button',{name:'Refresh this email review'}).click();
  assert.equal(await page.locator('#attention-list .mail-analysis').getByRole('button',{name:'Save selected decisions'}).isDisabled(),true);
  await page.evaluate(()=>{
    mailDecisionConflict=false;
    const booking=mailAnalyses[0].findings[1];
    mailAnalyses[0]={...mailAnalyses[0],findings:[{...booking,value:{...booking.value,application_id:null,kind:'other',description:'Book a time using the scheduling link'}}]};
    consoleState.reviews=[{kind:'mail_analysis',id:'mail1',status:'review',analysis:mailAnalyses[0]}]; renderReviewQueue();
  });
  const bookingCard=page.locator('#attention-list .mail-analysis');
  await bookingCard.getByRole('combobox',{name:'Decision for Book a time using the scheduling link',exact:true}).selectOption('accepted');
  assert.equal(await bookingCard.getByRole('combobox',{name:'Application for Book a time using the scheduling link',exact:true}).inputValue(),'');
  assert.equal(await bookingCard.getByRole('button',{name:'Save selected decisions'}).isDisabled(),true);
  await bookingCard.getByRole('combobox',{name:'Application for Book a time using the scheduling link',exact:true}).selectOption('app1');
  assert.equal(await bookingCard.getByRole('button',{name:'Save selected decisions'}).isDisabled(),true);
  await bookingCard.getByText('Correct this finding',{exact:true}).click();
  await bookingCard.getByRole('combobox',{name:'Task for this request',exact:true}).selectOption('follow_up');
  await bookingCard.getByRole('button',{name:'Save selected decisions'}).click();
  const bookingDecision=await page.evaluate(()=>JSON.parse(requests.filter(r=>r.path==='/api/v1/mail-analyses/mail1/decisions').at(-1).options.body));
  assert.equal(bookingDecision.decisions[0].replacement.kind,'other');
  assert.equal(bookingDecision.decisions[0].replacement.task_kind,'follow_up');
  assert.equal(bookingDecision.decisions[0].replacement.application_id,'app1');
  }
  await page.evaluate(()=>renderShortlist({recommendations:[],model:{ready:false},session_id:null}));
  assert.match(await page.locator('#shortlist-list').innerText(),/refresh to load saved model rankings/);
  await page.evaluate(()=>renderShortlist({recommendations:[],model:{ready:true},session_id:'saved',options:{days:7}}));
  assert.match(await page.locator('#shortlist-list').innerText(),/No jobs matched these filters/);
  await page.evaluate(()=>renderShortlist({source:'curated',recommendations:[]}));
  assert.match(await page.locator('#shortlist-list').innerText(),/No Codex picks/);
  for (const policy of ['selective','broad','compare','champion']) {
    await page.evaluate(policy=>renderShortlist({recommendations:[{title:'Engineer',ranking_score:.987654}],options:{days:7,policy},model:{ready:true}}),policy);
    await page.locator('#shortlist-list .ranking-details summary').click();
    assert.equal(await page.locator('#shortlist-list .shortlist-score').innerText(),'Ranking score 0.988');
  }
  await page.evaluate(()=>renderShortlist({source:'curated',recommendations:[{title:'Engineer',ranking_score:.987654,final_score:.876543,score_components:{sparse:.765432},explanation:'Close fit: your Python experience matches the role.'}],data_status:{broad:{status:'stale',latest_score_at:'2026-10-01'}},options:{days:7}}));
  assert.equal(await page.locator('#shortlist-list .shortlist-score').count(),0);
  assert.match(await page.locator('#shortlist-list').innerText(),/Close fit: your Python experience/);
  assert.equal(await page.locator('#shortlist-freshness').isVisible(),false);
  assert.doesNotMatch(await page.locator('#shortlist-list').textContent(),/0\.987|0\.876|0\.765|Ranking score/);
  await page.evaluate(()=>renderShortlist({recommendations:[{title:'Engineer',ranking_score:.987}],options:{days:7},model:{ready:true}}));
  assert.equal(await page.locator('.ranking-details').getAttribute('open'),null);
  assert.equal(await page.locator('.shortlist-advanced').getAttribute('open'),null);
  await page.evaluate(()=>openJobPreview({id:'job1',ats:'greenhouse',ranking_score:.987654,semantic_score:.876543,score_components:{sparse:.765432},explanation:{summary:'MODEL_DIAGNOSTIC_SENTINEL'}}));
  await page.locator('.job-preview-description h3').waitFor();
  assert.equal(await page.locator('.job-preview-header-actions a').getAttribute('href'),'https://example.test/job');
  assert.equal(await page.locator('.job-preview-description li').innerText(),'Build useful things.');
  assert.doesNotMatch(await page.locator('#job-preview').textContent(),/Ranking score|0\.987|0\.876|0\.765|MODEL_DIAGNOSTIC_SENTINEL/);
  assert.deepEqual(await page.evaluate(()=>previewPostingFacts({ats:'ashby',id:'j',ranking_score:.99,job_posting:{title:'Nested posting',score_components:{sparse:.8}},application_id:'app1'})),{ats:'ashby',id:'j',application_id:'app1',title:'Nested posting'});
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#job-preview').isVisible(),false);
  await page.evaluate(async () => {
    $('#shortlist-feedback').hidden = false;
    $('#shortlist-feedback').textContent = 'Old error';
    await refreshShortlist({preventDefault(){},submitter:$('#shortlist-form button[type=submit]')});
  });
  assert.equal(await page.locator('#shortlist-feedback').isVisible(), false);
  const focusChecks = await page.evaluate(async () => {
    initializeDiscoveryViews();
    consoleState.view = 'shortlist';
    shortlistSource = 'curated'; state.shortlist = {source:'curated'}; state.shortlistLoading = true;
    const before = requests.length;
    window.dispatchEvent(new Event('focus'));
    const guarded = requests.length === before;
    state.shortlistLoading = false; state.shortlist = {source:'model'}; shortlistSource = 'auto';
    $('#shortlist-days').dispatchEvent(new Event('change',{bubbles:true}));
    window.dispatchEvent(new Event('focus'));
    const preservesEdits = requests.length === before;
    state.shortlist = {source:'curated'}; shortlistSource = 'curated';
    window.dispatchEvent(new Event('focus'));
    return {guarded,preservesEdits,discovers:requests.length === before + 1};
  });
  assert.deepEqual(focusChecks,{guarded:true,preservesEdits:true,discovers:true});
  // The legacy Ranking Lab retains jobs and order without duplicating score diagnostics.
  const lab = await browser.newPage();
  lab.on('pageerror',error=>errors.push(error.message));
  const labHTML = (await readFile(new URL('../../job_search/ranking/web/index.html',import.meta.url),'utf8')).replace(/<script[^>]*>[\s\S]*?<\/script>/g,'');
  await lab.setContent(labHTML);
  await lab.addScriptTag({content:`window.fetch=async()=>({ok:true,json:async()=>({model:{ready:true,score_count:1},options:{limit:20},recommendations:[{title:'Legacy ranked role',company:'Example',rank:1,segment:'explore',final_score:.987654,ranking_score:.876543,score_components:{dense_linear:.765432,dense_neighbor:.654321,sparse:.543210},explanation:{summary:'MODEL_DIAGNOSTIC_SENTINEL'},salary:{status:'known'},jobUrl:'https://example.test/job'}]})});`});
  await lab.addScriptTag({content:await readFile(new URL('../../job_search/ranking/web/app.js',import.meta.url),'utf8')});
  await lab.locator('.recommendation-card').waitFor();
  for (const width of [390,1280]) {
    await lab.setViewportSize({width,height:900});
    const text = await lab.locator('#recommendation-list').innerText();
    assert.match(text,/Legacy ranked role/);
    assert.match(text,/#1/);
    assert.match(text,/Salary\s*known/);
    assert.doesNotMatch(text,/Preference|Combined|Semantic|Neighbors|Lexical|Explore|MODEL_DIAGNOSTIC_SENTINEL|0\.987|99%|88%/);
    assert.equal(await lab.locator('.score-line,.recommendation-explanation,.recommendation-card.explore').count(),0);
  }
  await lab.close();
  assert.deepEqual(errors,[]);
  console.log('ok (Review normalization, ordering, decision bodies, suggested matches and overrides, empty states, preview access)');
} finally { await browser.close(); }
