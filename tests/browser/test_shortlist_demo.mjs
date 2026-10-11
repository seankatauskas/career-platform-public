import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';

const fixture=spawn(process.env.PYTHON || 'python3',['-u','-m','tests.fixtures.shortlist_demo'],{stdio:['ignore','pipe','pipe']});
let stderr=''; fixture.stderr.on('data',chunk=>{stderr+=chunk;});
const ready=await new Promise((resolve,reject)=>{
  let output='';
  const timer=setTimeout(()=>reject(new Error(stderr || 'Demo startup timed out')),15000);
  fixture.stdout.on('data',chunk=>{output+=chunk;if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));}});
  fixture.on('exit',code=>{clearTimeout(timer);reject(new Error(`Demo exited ${code}: ${stderr}`));});
});
const browser=await chromium.launch({headless:true});
try {
  const page=await browser.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>new URL(route.request().url()).origin===new URL(ready.url).origin ? route.continue() : route.abort());
  await page.goto(ready.url);
  await page.locator('#shortlist-list .card').first().waitFor();
  const initialList=await page.locator('#curated-list').inputValue();
  const initialOptions=await page.locator('#curated-list option').count();
  const initialCards=await page.locator('#shortlist-list').innerText();
  await page.locator('#generate-list').click();
  await page.getByText('Queued; waiting to gather jobs…',{exact:true}).waitFor();
  assert.equal(await page.locator('#generate-list').innerText(),'Generating…');
  assert.equal(await page.locator('#generate-list').isDisabled(),true);
  assert.equal(await page.locator('#shortlist-list').innerText(),initialCards);
  // Closing/reopening the view does not stop the local demo worker.
  await page.reload();
  await page.getByText('Reviewing jobs…',{exact:true}).waitFor({timeout:16000});
  await page.getByText('Validating recommendations…',{exact:true}).waitFor({timeout:16000});
  await page.waitForFunction(()=>!document.querySelector('#generate-list').disabled,null,{timeout:16000});
  await page.waitForFunction(id=>document.querySelector('#curated-list').value!==id,initialList);
  assert.equal(await page.locator('#curated-list option').count(),initialOptions+2);
  assert.equal(await page.locator('#shortlist-list .card').count(),5);
  assert.equal(await page.locator('#list-generation-status').isVisible(),false);
  assert.deepEqual(errors,[]);
  // It can be demonstrated again without resetting the fixture.
  await page.locator('#generate-list').click();
  await page.getByText('Queued; waiting to gather jobs…',{exact:true}).waitFor();
  assert.equal(await page.locator('#generate-list').isDisabled(),true);
  console.log('ok: demo generation phases, reload, preserved cards, automatic publication and repeat click');
} finally {
  await browser.close();
  fixture.kill('SIGTERM');
}
