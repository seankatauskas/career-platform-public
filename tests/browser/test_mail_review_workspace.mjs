// Real local HTTP boundary: a searched catalog role becomes an attached tracking
// record, while the proposed receipt remains a distinct, explicit approval.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';
const child=spawn(process.env.PYTHON || 'python3',['-m','tests.browser.mail_review_fixture'],{stdio:['ignore','pipe','pipe']});
let stderr=''; child.stderr.on('data',d=>stderr+=d);
const fixture=await new Promise((resolve,reject)=>{
  let data=''; const timeout=setTimeout(()=>reject(new Error('Fixture timed out: '+stderr)),20000);
  child.on('exit',code=>{clearTimeout(timeout);reject(new Error('Fixture exited '+code+': '+stderr));});
  child.stdout.on('data',d=>{data+=d;try{const value=JSON.parse(data.split('\n')[0]);clearTimeout(timeout);resolve(value);}catch{}});
}).catch(error=>{child.kill('SIGINT');throw error;});
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/*',r=>r.request().url().startsWith(fixture.url)?r.continue():r.abort());
await mkdir('.cache/mail-review-workspace',{recursive:true});
try {
  await page.goto(fixture.url+'/#review');
  const card=page.locator('[data-review-key="evidence_group:'+fixture.group_id+'"]');
  await card.getByRole('button',{name:'None of these',exact:true}).click();
  await card.getByRole('button',{name:'Use this role and attach email',exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>state.applications.length),0);
  await card.getByRole('button',{name:'Use this role and attach email',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:'.cache/mail-review-workspace/catalog-choice.png',fullPage:true});
  await card.getByRole('button',{name:'Use this role and attach email',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy && ownerReviewState.groups[0]?.association);
  assert.match(await card.innerText(),/Northwind/);
  assert.equal(await card.getByRole('button',{name:'Record receipt',exact:true}).count(),1);
  const applicationId=await page.evaluate(()=>ownerReviewState.groups[0].association.application_id);
  // Use the documented owner workspace endpoint for actual persisted effect checks.
  const actual=await page.evaluate(id=>api('/api/v1/application-owner/workspace?application_id='+encodeURIComponent(id)),applicationId);
  assert.equal(actual.job.sources[0].source_id,'catalog-platform');
  assert.equal(actual.records.receipts.items.length,0);
  assert.equal(actual.records.submissions.items.length,0);
  await page.evaluate(()=>{document.documentElement.dataset.theme='dark';});
  await page.screenshot({path:'.cache/mail-review-workspace/receipt-ready-dark.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:'.cache/mail-review-workspace/receipt-ready-mobile.png',fullPage:true});
  await card.getByRole('button',{name:'Record receipt',exact:true}).click();
  await page.waitForFunction(()=>!ownerReviewState.busy && !ownerReviewState.groups.some(g=>g.proposals.length));
  const saved=await page.evaluate(id=>api('/api/v1/application-owner/workspace?application_id='+encodeURIComponent(id)),applicationId);
  assert.equal(saved.records.receipts.items.length,1);
  assert.equal(saved.records.submissions.items.length,0);
  assert.deepEqual(errors,[]);
  console.log('ok (real mail review catalog search, reviewed link, separate receipt approval)');
} finally {await browser.close();child.kill('SIGINT');if(child.exitCode===null) await new Promise(resolve=>child.once('exit',resolve));}
