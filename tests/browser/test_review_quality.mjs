import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const fixture=spawn(process.env.PYTHON || 'python3',['-u','-m','tests.fixtures.review_quality_demo'],{stdio:['ignore','pipe','pipe']});
let stderr=''; fixture.stderr.on('data',chunk=>{stderr+=chunk;});
let browser;
try {
  const ready=await new Promise((resolve,reject)=>{
    let data='';
    const timer=setTimeout(()=>reject(new Error(`fixture timeout: ${stderr}`)),15000);
    fixture.stdout.on('data',chunk=>{data+=chunk;if(data.includes('\n')){clearTimeout(timer);resolve(JSON.parse(data.split('\n')[0]));}});
    fixture.on('exit',code=>{clearTimeout(timer);reject(new Error(`fixture exited ${code}: ${stderr}`));});
  });
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:1000}});
  const errors=[]; page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>route.request().url().startsWith(ready.url)?route.continue():route.abort());
  const broad=ready.lists.find(list=>list.kind==='broad') || ready.lists[0];
  const targeted=ready.lists.find(list=>list.kind==='targeted') || ready.lists[1];
  await page.goto(ready.url+'/#shortlist/'+broad.list_id);
  await page.locator('#shortlist-list .card').nth(3).waitFor();
  assert.equal(await page.locator('.shortlist-related-group .card').count(),2);
  assert.equal(await page.locator('.shortlist-alternatives .card').count(),1);
  const first=page.locator('#shortlist-list .card').first();
  assert.match(await first.innerText(),/Close technical fit/);
  assert.match(await first.innerText(),/active clearance/);
  assert.match(await first.innerText(),/Production ownership/);
  assert.equal((await first.innerText()).match(/Next step:/g).length,1);
  assert.equal(await first.locator('.ranking-details').evaluate(el=>el.open),false);
  assert.deepEqual(await page.locator('#shortlist-list .rank').allTextContents(),['01','02','03','04']);
  await mkdir('.cache/review-quality',{recursive:true});
  await page.screenshot({path:'.cache/review-quality/broad-desktop.png',fullPage:true});
  await page.goto(ready.url+'/#shortlist/'+targeted.list_id);
  await page.waitForFunction(id=>state.shortlist?.list_id===id,targeted.list_id);
  await page.locator('#shortlist-list .card').nth(2).waitFor();
  assert.equal(await page.locator('#shortlist-list .card').count(),3);
  assert.equal(await page.locator('.shortlist-alternatives').count(),0);
  assert.match(await page.locator('#shortlist-list .card').nth(2).innerText(),/Employer availability unconfirmed/);
  assert.doesNotMatch(await page.locator('#shortlist-list').innerText(),/fictional_fixture_observation/);
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'.cache/review-quality/targeted-mobile.png',fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('ok - real v2 ledger, calibration, publication and responsive cards');
} finally {
  if(browser) await browser.close();
  fixture.kill('SIGTERM');
}
