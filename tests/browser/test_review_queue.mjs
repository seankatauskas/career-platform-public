import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

// Isolated page-module fixture: no server, credentials, or external writes.
const browser = await chromium.launch({channel:'chromium', headless:true});
const page = await browser.newPage({viewport:{width:1280,height:900}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.setContent('<span id="review-count"></span><section id="attention"></section><section id="shortlist"></section><dialog id="job-preview"><header class="job-preview-header"><div><p id="job-preview-company"></p><h2 id="job-preview-title"></h2></div><button id="job-preview-close">Close</button></header><div id="job-preview-body"></div></dialog>');
  await page.addScriptTag({content:`
    const $ = selector => document.querySelector(selector);
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
    let attentionFails = false;
    async function api(path, options) {
      requests.push({path,options});
      if(path === '/api/v1/attention') { if (attentionFails) throw new Error('Fixture unavailable'); return attentionResponse; }
      if(path === '/api/v1/actions') return actionResponse;
      if(path === '/api/v1/curated-shortlists') return {lists:[]};
      if(path.includes('/jobs/preview')) return {job:{title:'Engineer',company:'Example',jobUrl:'https://example.test/job',location:'Remote',ranking_score:.987654,final_score:.876543,score_components:{sparse:.765432},explanation:{summary:'MODEL_DIAGNOSTIC_SENTINEL'}},description_html:'<h3>Responsibilities</h3><ul><li>Build useful things.</li></ul>'};
      return {};
    }
  `});
  for (const file of ['shortlist-view.js','review-view.js','job-preview.js']) await page.addScriptTag({content:await readFile(new URL(`../../job_search/web/${file}`,import.meta.url),'utf8')});
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
  await page.evaluate(() => { location.hash='#review/event_proposal/p1'; $('#attention').style.marginTop='2000px'; renderReviewQueue(); });
  await page.waitForFunction(() => document.activeElement?.dataset.reviewKey === 'event_proposal:p1');
  assert(await page.evaluate(() => scrollY > 1000));
  await page.evaluate(() => { $('#attention').style.marginTop=''; location.hash='#review'; renderReviewQueue(); });
  assert.equal(await page.getByRole('button',{name:'Confirm update',exact:true}).count(),2);
  assert.equal(await page.getByRole('button',{name:'Accept',exact:true}).count(),0);
  const approve = page.getByRole('button',{name:'Create Outlook draft',exact:true});
  await approve.click();
  const decision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/actions/a1/decision'));
  assert.deepEqual(JSON.parse(decision.options.body),{idempotency_key:'action-fixture',approve:true,payload_sha256:'abc'});
  assert.equal(await page.locator('#review-count').innerText(),'');
  await page.evaluate(()=>{ consoleState.reviews=[{id:'p2',kind:'event_proposal',status:'pending',detail:'interview_requested',application_id:'app1',candidate_application_ids:[]}]; renderReviewQueue(); });
  await page.getByRole('button',{name:'Confirm update',exact:true}).click();
  const proposalDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/p2/decision'));
  assert.equal(JSON.parse(proposalDecision.options.body).decision,'accepted');
  await page.evaluate(()=>{ consoleState.reviews=[{id:'late',kind:'event_proposal',status:'pending',detail:'submission_confirmed',application_id:null,candidate_application_ids:[]}]; renderReviewQueue(); });
  assert.equal(await page.getByRole('button',{name:'Confirm update',exact:true}).isDisabled(),true);
  assert.match(await page.locator('#attention-list').innerText(),/No matching application yet/);
  await page.evaluate(()=>{ consoleState.reviews[0].candidate_application_ids=['app1']; renderReviewQueue(); });
  assert.equal(await page.getByRole('combobox',{name:'Application for this proposal'}).inputValue(),'');
  assert.equal(await page.getByRole('button',{name:'Confirm update',exact:true}).isDisabled(),true);
  await page.getByRole('combobox',{name:'Application for this proposal'}).selectOption('app1');
  assert.equal(await page.getByRole('button',{name:'Confirm update',exact:true}).isEnabled(),true);
  await page.getByRole('button',{name:'Confirm update',exact:true}).click();
  const lateDecision = await page.evaluate(()=>requests.find(r=>r.path==='/api/v1/proposals/late/decision'));
  assert.equal(JSON.parse(lateDecision.options.body).selected_application_id,'app1');
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
  console.log('ok (Review normalization, ordering, decision bodies, empty states, preview access)');
} finally { await browser.close(); }
