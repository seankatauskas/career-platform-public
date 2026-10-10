import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '../../extension/node_modules/playwright-core/index.mjs';
import {startDemo} from './demo-process.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const stateDir=await mkdtemp(path.join(os.tmpdir(),'career-lifecycle-'));
const demo=await startDemo({root,stateDir});
const browser=await chromium.launch({channel:'chromium',headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
await context.route('**/*',r=>r.request().url().startsWith(demo.url)||r.request().url().startsWith('data:')?r.continue():r.abort());
const page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
const output=path.join(root,'.cache/lifecycle-browser');await mkdir(output,{recursive:true});
try {
  await page.goto(demo.url+'/#applications');
  await page.locator('#application-list .application-link').first().click();
  await page.locator('#workspace-lifecycle').getByRole('heading',{name:'Next steps'}).waitFor();
  const add=page.locator('#workspace-lifecycle details').filter({has:page.locator('summary').filter({hasText:'Add a next step'})});
  await add.locator(':scope > summary').click();
  await add.getByLabel('Task',{exact:true}).selectOption('send_document');
  await add.getByLabel('Description',{exact:true}).fill('Send portfolio link');
  await add.getByRole('button',{name:'Save next step'}).click();
  let task=page.locator('#workspace-lifecycle .lifecycle-item').filter({hasText:'Send portfolio link'});
  await task.getByRole('button',{name:'Mark complete',exact:true}).waitFor();
  await task.getByRole('button',{name:'Mark complete',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#workspace-lifecycle .lifecycle-item')].some(x=>x.textContent.includes('Send portfolio link')&&x.textContent.includes('completed')));
  await task.getByText('Change history',{exact:true}).click();
  await task.getByText(/Revision 2/).waitFor();
  // Record a real assessment using the normal form and then an outcome revision.
  const detail=page.locator('#workspace-lifecycle details').filter({has:page.locator('summary').filter({hasText:'Assessments and offers'})});
  await detail.locator(':scope > summary').click();
  await detail.getByLabel('Title',{exact:true}).fill('Design exercise');
  await detail.getByLabel('Details',{exact:true}).fill('Submit through employer portal');
  await detail.getByRole('button',{name:'Save record',exact:true}).click();
  await page.waitForFunction(()=>consoleState.workspace?.briefing.details.some(x=>x.details.title==='Design exercise'));
  await detail.locator(':scope > summary').click();
  const card=detail.locator('.lifecycle-item').filter({hasText:'Design exercise'});
  await card.getByLabel('Record outcome').selectOption('submitted');
  await card.getByRole('button',{name:'Save outcome'}).click();
  await page.waitForFunction(()=>consoleState.workspace?.briefing.details.some(x=>x.details.title==='Design exercise'&&x.status==='submitted'));
  // Interviews are proposed in the workspace and separately accepted in Review.
  const meeting=page.locator('#workspace-lifecycle details').filter({has:page.locator('summary').filter({hasText:'Record or reschedule an interview'})});
  await meeting.locator(':scope > summary').click();
  await meeting.getByLabel('Round name').fill('Technical screen');
  const start=new Date(Date.now()+86400000*3);start.setSeconds(0,0);
  const local=date=>new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
  await meeting.getByLabel('Start (local time)').fill(local(start));
  await meeting.getByLabel('End (local time)').fill(local(new Date(start.getTime()+3600000)));
  await meeting.getByRole('button',{name:'Propose interview time'}).click();
  await page.waitForFunction(()=>consoleState.reviews.some(x=>x.kind==='interview_revision'));
  await page.goto(demo.url+'/#review');
  const review=page.locator('#attention-list article').filter({hasText:'Technical screen'});
  await review.getByRole('button',{name:'Confirm update',exact:true}).click();
  await page.waitForFunction(()=>!consoleState.reviews.some(x=>x.kind==='interview_revision'));
  await page.goto(demo.url+'/#applications');
  await page.locator('#application-list .application-link').first().click();
  await page.locator('#workspace-lifecycle .lifecycle-item').filter({hasText:'Technical screen'}).first().waitFor();
  // Navigation can clear workspace state after the previous DOM was observed.
  // Wait for the completed load and capture its briefing in the same callback.
  const briefingHandle=await page.waitForFunction(()=>
    !document.querySelector('#application-workspace').hasAttribute('aria-busy')
      && consoleState.workspace?.briefing);
  const briefing=await briefingHandle.jsonValue();
  await briefingHandle.dispose();
  assert.equal(briefing.interviews.rounds[0].status,'confirmed');
  assert(briefing.reminders.some(x=>x.source==='interview'));
  assert.equal(briefing.coverage.complete,false);
  await page.locator('.workspace-tabs a[data-tab=messages]').click();
  await page.getByRole('heading',{name:'Conversation history'}).waitFor();
  // Tab navigation reloads the workspace; wait before opening controls that the
  // completed response replaces, especially on slower shared CI runners.
  await page.waitForLoadState('networkidle');
  await page.getByText('Review older Outlook history',{exact:true}).click();
  await page.getByRole('button',{name:'Refresh replay status'}).waitFor();
  await page.locator('.workspace-tabs a[data-tab=overview]').click();
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile overflow');
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  await writeFile(path.join(output,'results.json'),JSON.stringify({passed:true,checks:['task completion and history','conversation and replay controls','assessment version','interview proposal approval','shared briefing reminders','mobile layout'],errors},null,2));
  console.log('ok (lifecycle browser acceptance)');
} catch (error) {
  await page.screenshot({path:path.join(output,"failure.png"),fullPage:true}).catch(()=>{});
  console.error(JSON.stringify({error:error.message,errors,diagnostics:demo.diagnostics()}));
  throw error;
} finally {
  await browser.close(); demo.process.kill("SIGINT");
}
