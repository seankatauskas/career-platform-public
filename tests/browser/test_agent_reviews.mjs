import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const fixture = spawn(process.env.PYTHON || 'python3', ['-u', '-m', 'tests.fixtures.agent_review_demo'], {stdio:['ignore','pipe','pipe']});
let stderr = ''; fixture.stderr.on('data', chunk => {stderr += chunk;});
const ready = await new Promise((resolve, reject) => {
  let data = '';
  const timer = setTimeout(() => reject(new Error(`fixture timeout: ${stderr}`)), 15000);
  fixture.stdout.on('data', chunk => { data += chunk; if(data.includes('\n')) {clearTimeout(timer); resolve(JSON.parse(data.split('\n')[0]));} });
  fixture.on('exit', code => {clearTimeout(timer); reject(new Error(`fixture exited ${code}: ${stderr}`));});
});
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  const requests=[]; page.on('request', request=>requests.push({url:request.url(),method:request.method()}));
  await page.route('**/*', route => route.request().url().startsWith(ready.url) ? route.continue() : route.abort());
  await page.goto(ready.url + '/#shortlist/' + ready.lists[1].list_id);
  await page.locator('#agent-review-status').getByText('Reviewed by Codex', {exact:true}).waitFor();
  assert.equal(await page.locator('#agent-review-details').evaluate(el => el.open), false);
  assert.equal(await page.locator('#agent-review-summary').getByText('Reviewed by agents', {exact:true}).isVisible(), false);
  assert.equal(await page.getByRole('button', {name:'Refresh review progress'}).isVisible(), false);
  await page.getByText('Review details', {exact:true}).click();
  await page.locator('#agent-review-summary').getByText('Reviewed by agents', {exact:true}).waitFor();
  assert.match(await page.locator('#agent-review-summary').innerText(), /2 of 2 postings assessed/);
  assert.match(await page.locator('#agent-review-summary').innerText(), /Strict posting window/);
  await page.getByText('Assessment evidence', {exact:true}).click();
  await page.locator('.agent-assessment').getByText('Python API experience', {exact:true}).waitFor();
  assert.match(await page.locator('.agent-assessment').innerText(), /Python APIs/);
  await page.getByRole('button', {name:'Refresh review progress'}).click();
  await page.locator('.agent-review-entry').first().locator('summary').click();
  await page.locator('#agent-review-history').getByText('Review in progress', {exact:true}).waitFor();
  assert.match(await page.locator('#agent-review-history').innerText(), /0 of 2 postings assessed/);
  await page.selectOption('#shortlist-sort', 'posted-newest');
  assert.equal(await page.locator('#shortlist-list .card').count(), 1);
  await page.getByText('Review details', {exact:true}).click();
  assert.equal(await page.locator('#agent-review-details').evaluate(el => el.open), false);
  const requestsBeforeCards=requests.length;
  await page.evaluate(()=>{
    ++shortlistLoadEpoch;
    const original=state.shortlist;
    const base=original.recommendations[0];
    const dates=['2026-10-02T12:00:00Z','2026-10-04T12:00:00Z','2026-10-01T12:00:00Z',null,'2026-10-03T12:00:00Z'];
    const related_group={id:'platform',label:'Platform roles'};
    const summaries=[
      {decision:'close',alignment:'core',eligibility:'unresolved',eligibility_condition:'Citizenship is unconfirmed.',next_step:'clarify',category:'core',related_group,gaps:['Working C++ is required.'],unknowns:['Citizenship is unconfirmed.'],availability:{status:'open',checked_at:'2026-10-04T12:00:00Z'}},
      {decision:'slight_stretch',alignment:'core',eligibility:'no_known_barrier',eligibility_condition:'',next_step:'apply',category:'core',related_group,gaps:['Learnable platform gap.'],unknowns:[],narrative_complete:true},
      {decision:'bigger_stretch',alignment:'core',gaps:['Senior ownership gap.'],unknowns:['Team size <script>unsafe</script>']},
      null,
      {decision:'broad_only',alignment:'adjacent',eligibility:'no_known_barrier',next_step:'explore',category:'alternative',gaps:[],unknowns:['Renewal targets are unspecified.']},
    ];
    const explanations=['Python API work matches.','Slight stretch. Python API work matches. Learnable platform gap.','Python APIs align.','Caller explanation <script>unsafe</script>','Technical implementation overlaps; commercial ownership changes the career direction.'];
    const recommendations=summaries.map((review_summary,index)=>({...base,id:`fixture-${index}`,title:`Fixture role ${index+1}`,rank:index+1,
      posted_at:dates[index],publishedAt:null,source_updated_at:dates[index],job_posting:undefined,
      recent_company_application:null,review_summary,explanation:explanations[index]}));
    shortlistSort='original';renderShortlist({...original,recommendations,review:{...original.review,metadata:{...original.review.metadata,
      source_inventory:{career_fact_count:9,resume_fact_count:4,pending_career_draft:true},
      search_brief:{revision:2,brief:{broad_geography:'us',targeted_geography:'us',conditional_order:'technical_fit'}},
    }}});
  });
  await page.getByText('Review details',{exact:true}).click();
  assert.match(await page.locator('#agent-review-summary').innerText(),/9 approved career facts and 4 resume excerpts/);
  assert.match(await page.locator('#agent-review-summary').innerText(),/Unapproved profile edits were excluded/);
  assert.match(await page.locator('#agent-review-summary').innerText(),/Broad: United States · Targeted: United States/);
  assert.match(await page.locator('#agent-review-summary').innerText(),/Conditional roles keep their technical fit order/);
  await page.getByText('Review details',{exact:true}).click();
  assert.deepEqual(await page.locator('#shortlist-list .rank').allTextContents(),['01','02','03','04','05']);
  assert.equal(await page.locator('.shortlist-related-group').count(),1);
  assert.equal(await page.locator('.shortlist-related-group .card').count(),2);
  assert.equal(await page.locator('.shortlist-alternatives .card').count(),1);
  const first=page.locator('[data-job-id="fixture-0"]');
  assert.match(await first.innerText(),/Close technical fit/);
  assert.match(await first.innerText(),/Eligibility needs clarification/);
  assert.match(await first.innerText(),/Citizenship is unconfirmed/);
  assert.match(await first.innerText(),/Working C\+\+ is required/);
  assert.match(await first.innerText(),/Open on the employer board/);
  assert.equal(await first.locator('.ranking-details').evaluate(el=>el.open),false);
  assert.equal((await first.innerText()).match(/Citizenship is unconfirmed/g).length,1);
  assert.equal((await page.locator('[data-job-id="fixture-1"]').innerText()).match(/Learnable platform gap/g).length,1);
  const legacy=page.locator('[data-job-id="fixture-2"]');
  assert.match(await legacy.innerText(),/Bigger stretch/);
  assert.match(await legacy.innerText(),/Senior ownership gap/);
  assert.match(await legacy.innerText(),/Team size <script>unsafe/);
  assert.equal(await legacy.locator('.review-condition').count(),0);
  const direct=page.locator('[data-job-id="fixture-3"]');
  assert.match(await direct.innerText(),/Caller explanation <script>unsafe/);
  assert.equal(await direct.locator('.review-fit-labels').count(),0);
  assert.equal(await page.locator('#shortlist-list script').count(),0);
  assert.equal(requests.slice(requestsBeforeCards).filter(request=>request.url.includes('/job-reviews/assessment')).length,0);
  const identities=await page.evaluate(()=>state.shortlist.recommendations.map(job=>[job.id,job.rank]));
  for(const [sort,ranks] of [['posted-newest',['02','05','01','03','04']],['posted-oldest',['03','01','05','02','04']],['updated-newest',['02','05','01','03','04']],['updated-oldest',['03','01','05','02','04']]]) {
    await page.selectOption('#shortlist-sort',sort);
    assert.deepEqual(await page.locator('#shortlist-list .rank').allTextContents(),ranks);
    assert.equal(await page.locator('.shortlist-related-group').count(),0);
    assert.equal(await page.locator('.shortlist-alternatives').count(),0);
    assert.equal(await first.locator('.review-related-marker').isVisible(),true);
    assert.deepEqual(await page.evaluate(()=>state.shortlist.recommendations.map(job=>[job.id,job.rank])),identities);
  }
  await page.selectOption('#shortlist-sort','original');
  await page.evaluate(()=>{
    state.shortlist.recommendations[0].recent_company_application={application_id:'previous',applied_at:'2026-10-01T12:00:00Z'};
    renderShortlist(state.shortlist,true);
  });
  assert.equal(await page.locator('.shortlist-related-group').count(),0);
  assert.deepEqual(await page.locator('#shortlist-list .rank').allTextContents(),['02','03','04','05']);
  assert.match(await page.locator('#shortlist-company-filter-status').innerText(),/4 of 5 roles shown/);
  await page.getByLabel('Exclude recently applied companies').uncheck();
  assert.equal(await page.locator('.shortlist-related-group .card').count(),2);
  assert.equal(requests.slice(requestsBeforeCards).filter(request=>request.method==='POST').length,0);
  await mkdir('.cache/agent-review-browser', {recursive:true});
  await page.screenshot({path:'.cache/agent-review-browser/desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({path:'.cache/agent-review-browser/mobile.png',fullPage:true});
  assert.deepEqual(errors, []);
  console.log('ok: inline fit and caveats, evidence, sibling groups, alternatives, preserved date sorts and IDs, settings-compatible cards, and mobile layout');
} finally {
  await browser.close(); fixture.kill('SIGTERM');
}
