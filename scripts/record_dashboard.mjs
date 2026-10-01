// Film the production dashboard using isolated, deterministic portfolio records.
// Browser regression tests intentionally run separately from this edited film.
import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {mkdtemp, mkdir, readFile, writeFile, rename, readdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID, createHash} from 'node:crypto';
import {performance} from 'node:perf_hooks';
import {chromium} from '../extension/node_modules/playwright-core/index.mjs';
import {startDemo} from '../tests/browser/demo-process.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.resolve(process.env.CONSOLE_OUTPUT || '.cache/dashboard-film');
const viewport = {width:800, height:640};
const pixels = {width:1600, height:1280};
await mkdir(output, {recursive:true});
const framesDir = await mkdtemp(path.join(output, 'frames-'));
const stateDir = await mkdtemp(path.join(tmpdir(), 'career-portfolio-film-'));
const report = {schemaVersion:2, passed:false, sourceRevision:execFileSync('git', ['rev-parse','HEAD'], {cwd:root,encoding:'utf8'}).trim(),
  scenario:'portfolio', viewport, pixels, deviceScaleFactor:2, frameRate:30, shots:[], assets:[], browserErrors:[], requestViolations:[],
  simulation:'Production dashboard and services; isolated fictional records and external responses. No live account connections.'};
report.workingTreeDirty=!!execFileSync('git',['status','--porcelain'],{cwd:root,encoding:'utf8'}).trim();
report.sourceFileSha256={};
for(const relative of ['scripts/record_dashboard.mjs','scripts/offline_system_demo.py','scripts/portfolio_demo.py','tests/browser/demo-process.mjs',
  ...(await readdir(path.join(root,'job_search/web'))).filter(name=>/\.(css|js|html)$/.test(name)).map(name=>'job_search/web/'+name)].sort()) {
  report.sourceFileSha256[relative]=createHash('sha256').update(await readFile(path.join(root,relative))).digest('hex');
}
report.sourceContentSha256=createHash('sha256').update(JSON.stringify(report.sourceFileSha256)).digest('hex');
let demo, browser, cdp, page, currentShot, pendingFrames=Promise.resolve();
const filmFrames=[];
const wait = ms => new Promise(resolve=>setTimeout(resolve,ms));

async function encode(args) {
  await new Promise((resolve,reject)=>{
    const child=spawn(process.env.FFMPEG_BIN || 'ffmpeg', args, {stdio:['ignore','ignore','pipe']});
    let stderr=''; child.stderr.on('data',chunk=>stderr=(stderr+chunk).slice(-16000));
    child.once('error',reject); child.once('exit',code=>code?reject(new Error(`FFmpeg exited ${code}: ${stderr}`)):resolve());
  });
}
async function asset(name, extra={}) {
  const buffer=await readFile(path.join(output,name));
  const item={name,bytes:buffer.length,sha256:createHash('sha256').update(buffer).digest('hex'),...extra};
  if(name.endsWith('.png'))Object.assign(item,{width:buffer.readUInt32BE(16),height:buffer.readUInt32BE(20)});
  report.assets.push(item); return item;
}
async function stable() {
  await page.evaluate(()=>document.fonts.ready);
  await page.waitForFunction(()=>consoleState.initialized && !document.querySelector('#application-workspace[aria-busy="true"]'));
  assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal overflow');
  await page.waitForTimeout(200);
}
async function route(hash, ready) {
  await page.evaluate(hash=>{location.hash=hash;window.scrollTo({top:0,behavior:'instant'});},hash);
  await page.waitForFunction(()=>consoleState.hash===location.hash);
  if(ready)await ready();
  await stable();
}
async function advance(step) {
  const id=randomUUID();
  const temporary=path.join(stateDir,`demo-command-${id}.tmp`);
  await writeFile(temporary,JSON.stringify({id,step}));
  await rename(temporary,path.join(stateDir,'demo-command.json'));
  for(let i=0;i<150;i++) {
    const status=JSON.parse(await readFile(path.join(stateDir,'demo-status.json'),'utf8'));
    if(status.command_id===id){assert.equal(status.status,'ready',JSON.stringify(status));return status;}
    await wait(100);
  }
  throw new Error(`Fixture command ${step} timed out`);
}
async function snapshot(name, locator) {
  await stable();
  // Never screenshot while the screencast is running: element capture can
  // temporarily change viewport emulation to include off-screen content.
  assert(!currentShot);
  if(locator) await locator.screenshot({path:path.join(output,`${name}.png`),scale:'device',animations:'disabled'});
  else await page.screenshot({path:path.join(output,`${name}.png`),scale:'device',animations:'disabled'});
  await asset(`${name}.png`,{kind:'screenshot',route:new URL(page.url()).hash});
}
async function frameAt(locator, inset=84) {
  await locator.evaluate((element,inset)=>window.scrollTo({top:Math.max(0,scrollY+element.getBoundingClientRect().top-inset),behavior:'instant'}),inset);
  await stable();
}
async function snapshotRegion(name, start, end, widthRoot=start) {
  await stable();assert(!currentShot);
  const first=await start.boundingBox(),last=await end.boundingBox(),width=await widthRoot.boundingBox();
  const scroll=await page.evaluate(()=>scrollY);
  const clip={x:width.x,y:first.y+scroll,width:width.width,height:last.y+last.height-first.y};
  assert(clip.height>0 && clip.height<1000,`${name} crop is too tall`);
  await page.screenshot({path:path.join(output,`${name}.png`),fullPage:true,clip,scale:'device',animations:'disabled'});
  await asset(`${name}.png`,{kind:'screenshot',route:new URL(page.url()).hash,composition:'Unmodified live component crop'});
}
async function shot(name, seconds, action) {
  await stable();
  // Default screencasts downsample to CSS pixels. Enlarge the backing surface
  // while retaining the real 800×640 CSS layout and coordinate space.
  await cdp.send('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:2,mobile:false,scale:2});
  await cdp.send('Emulation.setVisibleSize',pixels);
  assert.deepEqual(await page.evaluate(()=>({width:innerWidth,height:innerHeight})),viewport);
  // Let the compositor replace its previous backing surface before accepting
  // the first frame; otherwise a one-frame zoom can leak into an edited cut.
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await wait(200);
  const record={name,duration:seconds,frames:[],started:performance.now()};
  currentShot=record;
  await cdp.send('Page.startScreencast',{format:'png',maxWidth:pixels.width,maxHeight:pixels.height,everyNthFrame:1});
  const deadline=performance.now()+5000;
  while(!record.frames.length && performance.now()<deadline)await wait(20);
  assert(record.frames.length,`No compositor frames for ${name}`);
  record.started=performance.now();
  if(action){await wait(seconds*1000*.52);await action();}
  await wait(Math.max(0,seconds*1000-(performance.now()-record.started)));
  await cdp.send('Page.stopScreencast');
  await pendingFrames;
  currentShot=null;
  const firstTimestamp=record.frames[0].timestamp;
  const kept=record.frames.filter(frame=>frame.timestamp-firstTimestamp<seconds);
  kept.forEach((frame,index)=>{
    const next=kept[index+1];
    filmFrames.push({...frame,duration:Math.max(.001,(next?next.timestamp-firstTimestamp:seconds)-(frame.timestamp-firstTimestamp))});
  });
  report.shots.push({name,seconds,route:new URL(page.url()).hash,sourceFrames:kept.length,
    timestamps:kept.map(frame=>+(frame.timestamp-firstTimestamp).toFixed(4)),
    capture:'Native Chromium compositor; unchanged frames held for their actual duration.'});
  await cdp.send('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:2,mobile:false,scale:1});
  await cdp.send('Emulation.setVisibleSize',viewport);
}

try {
  demo=await startDemo({root,stateDir,scenario:'portfolio'});
  report.fixture={...demo.ready};
  const origin=new URL(demo.url).origin;
  browser=await chromium.launch({channel:'chromium',headless:true});
  const context=await browser.newContext({viewport,deviceScaleFactor:2,colorScheme:'dark',timezoneId:'America/Chicago'});
  await context.addInitScript(()=>localStorage.setItem('career-platform:theme','dark'));
  await context.route('**/*',route=>{
    const url=route.request().url();
    if(url.startsWith('data:') || url.startsWith('blob:') || new URL(url).origin===origin)return route.continue();
    report.requestViolations.push({origin:new URL(url).origin,method:route.request().method()});return route.abort();
  });
  page=await context.newPage();
  page.on('pageerror',error=>report.browserErrors.push(error.message));
  await page.goto(demo.url+'/#shortlist');
  await page.locator('#shortlist-list .card').first().waitFor();
  cdp=await context.newCDPSession(page);
  cdp.on('Page.screencastFrame',event=>{
    const target=currentShot;
    // ACK immediately so writing PNGs never stalls the browser compositor.
    cdp.send('Page.screencastFrameAck',{sessionId:event.sessionId}).catch(()=>{});
    if(!target)return;
    pendingFrames=pendingFrames.then(async()=>{
      const buffer=Buffer.from(event.data,'base64');
      assert.equal(buffer.readUInt32BE(16),pixels.width);assert.equal(buffer.readUInt32BE(20),pixels.height);
      const file=path.join(framesDir,`${String(report.shots.length).padStart(2,'0')}-${String(target.frames.length).padStart(5,'0')}.png`);
      await writeFile(file,buffer);target.frames.push({file,timestamp:event.metadata.timestamp});
    });
  });
  const fixture=JSON.parse(await readFile(path.join(stateDir,'demo-status.json'),'utf8'));
  const hero=fixture.hero_application_id || fixture.ids?.applications?.northstar;
  assert(hero,'Portfolio fixture must expose hero_application_id');
  report.fixture={scenario:fixture.scenario,fixtureNow:fixture.fixture_now,counts:fixture.counts};
  await frameAt(page.locator('#shortlist .section-heading'));
  await snapshot('shortlist');
  await shot('shortlist',4);

  const heroRole=page.locator('#shortlist-list .card').filter({hasText:'Northstar Labs'}).first();
  await heroRole.getByRole('button',{name:'Platform Engineer',exact:true}).click();
  await page.locator('#job-preview-body .job-preview-description').waitFor();
  await snapshot('preview');
  await shot('preview',3);
  await page.getByRole('button',{name:'Close job preview'}).click();

  await route('#applications',()=>page.locator('#application-list .application-link').first().waitFor());
  await frameAt(page.locator('#application-list thead'));
  await snapshotRegion('applications',page.locator('#application-list thead'),page.locator('#application-list tbody tr').nth(2),page.locator('#application-list table'));
  await shot('applications',4);

  await route(`#applications/${hero}/overview`,()=>page.locator('#workspace-company').filter({hasText:'Northstar Labs'}).waitFor());
  await frameAt(page.locator('#workspace-overview > h4'));
  await snapshotRegion('application-history',page.locator('#workspace-overview > h4'),page.locator('#timeline .timeline-event').last(),page.locator('#workspace-overview'));
  await shot('application-record',4);

  await route(`#review/event_proposal/${fixture.ids.hero_interview_review}`,()=>page.locator('#attention-list article').first().waitFor());
  const proposal=page.locator('#attention-list article.review-selected');
  await proposal.waitFor();await proposal.scrollIntoViewIfNeeded();
  const proposalElementId=await proposal.getAttribute('id');
  await snapshot('review',proposal);
  await shot('review',4,async()=>{
    // Keyboard activation uses the real focused button without screen-coordinate
    // ambiguity from Chromium's high-density screencast backing surface.
    await proposal.getByRole('button',{name:'Confirm update',exact:true}).press('Enter');
    await page.waitForFunction(id=>!document.getElementById(id),proposalElementId);
  });

  await route(`#applications/${hero}/messages`,()=>page.locator('#workspace-messages .message-body').first().waitFor());
  await frameAt(page.locator('#workspace-messages'));
  await snapshot('messages',page.locator('#workspace-messages'));
  await shot('messages',4);

  await route(`#review/outlook_reply_draft/${fixture.ids.hero_reply_action}`,()=>page.locator('#attention-list article').first().waitFor());
  const reply=page.locator('#attention-list article.review-selected');
  await reply.waitFor();await reply.scrollIntoViewIfNeeded();
  const replyElementId=await reply.getAttribute('id');
  await snapshot('reply',reply);
  await shot('reply-draft',4,async()=>{
    await reply.getByRole('button',{name:'Create Outlook draft',exact:true}).press('Enter');
    await page.waitForFunction(id=>!document.getElementById(id),replyElementId);
  });
  const executed=await advance('execute');assert(executed.drafts_created>=1,'Approved action did not create its simulated draft');
  report.executedDrafts=executed.drafts_created;

  await route(`#applications/${hero}/overview`,()=>page.locator('#workspace-status').filter({hasText:'Interviewing'}).waitFor());
  await frameAt(page.locator('#application-workspace'));
  await shot('interviewing',3);

  // Editorial stills supply the depth that cannot fit a short film.
  await route('#settings/operations',()=>page.locator('#readiness-list li').first().waitFor());
  await page.locator('.ops-activity').filter({hasText:'Activity counts and release details'}).locator('summary').click();
  await frameAt(page.locator('#health-detail'));
  await snapshot('operations',page.locator('.ops-activity').filter({hasText:'Activity counts and release details'}));
  await route(`#applications/${hero}/documents`,()=>page.locator('#workspace-documents .application-document').first().waitFor());
  await frameAt(page.locator('#workspace-documents'));
  await snapshot('documents',page.locator('#workspace-documents'));
  await route('#settings/career-profile',()=>page.locator('#career-editor details').first().waitFor());
  await frameAt(page.locator('#career .section-heading'));
  await snapshot('career-profile');
  await route(`#applications/${hero}/overview`,()=>page.locator('#workspace-company').filter({hasText:'Northstar Labs'}).waitFor());
  await page.locator('#workspace-posting-history > summary').click();
  await snapshot('posting-history',page.locator('#workspace-posting-history'));

  assert.deepEqual(report.browserErrors,[]);assert.deepEqual(report.requestViolations,[]);
  assert.equal(await page.locator('.brand').innerText(),'Career Platform');
  const manifest=path.join(framesDir,'frames.txt');
  const concat=filmFrames.map(frame=>`file '${path.basename(frame.file)}'\nduration ${frame.duration.toFixed(6)}`).join('\n')+`\nfile '${path.basename(filmFrames.at(-1).file)}'\n`;
  await writeFile(manifest,concat);
  await encode(['-y','-f','concat','-safe','0','-i',manifest,'-vf','fps=30','-t','30','-an','-c:v','libx264','-preset','slow','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',path.join(output,'walkthrough.mp4')]);
  await asset('walkthrough.mp4',{kind:'video',...pixels,duration:30,frameRate:30});
  for(const [relative,expected] of Object.entries(report.sourceFileSha256)) {
    assert.equal(createHash('sha256').update(await readFile(path.join(root,relative))).digest('hex'),expected,`Capture source changed during recording: ${relative}`);
  }
  report.sourceVerified=true;
  report.duration=30;report.sourceFrameCount=filmFrames.length;report.passed=true;
  console.log(`Recorded eight shots, 30 seconds, ${filmFrames.length} compositor frames and ${report.assets.length-1} screenshots: ${output}`);
} catch(error) {
  report.error=error.stack || String(error);report.fixtureDiagnostics=demo?.diagnostics();
  if(page)await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
  throw error;
} finally {
  await writeFile(path.join(output,'results.json'),JSON.stringify(report,null,2)+'\n');
  await browser?.close();demo?.process.kill('SIGINT');
}
