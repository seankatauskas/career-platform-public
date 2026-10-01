// Isolated page contract checks. The full dashboard suites cover routing and shared bootstrap.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '../../extension/node_modules/playwright-core/index.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = path.join(root, '.cache/settings-views');
await mkdir(output, {recursive:true});
const setup = `
const $ = selector => document.querySelector(selector);
const node = (tag, cls='', text) => { const el=document.createElement(tag); el.className=cls; if(text!==undefined)el.textContent=text; return el; };
const clear = el => {el.replaceChildren();el.classList.remove('empty');};
const meta = parts => node('p','meta',parts.filter(Boolean).join(' · '));
const notice = text => {document.querySelector('#notice').textContent=text;};
const key = () => crypto.randomUUID();
const readableResumeReason = value => String(value||'Import unavailable').replaceAll('_',' ');
const renderHeaderNotification = () => {};
const state = { careerProfile:null, careerContent:null, careerDirty:false, careerEpoch:0, applications:[{application_id:'earlier',current_phase:'preparing',title_snapshot:'Systems Engineer',employer_snapshot:'Example company'},{application_id:'submitted',current_phase:'submitted',title_snapshot:'Submitted job'}] };
const calls=[];
const content={identity:{name:'Example Candidate',email:'candidate@example.test'},summary:'Infrastructure engineer',education:[],experience:[{company:'Example company',role:'Engineer',bullets:[{text:'Built reliable systems'}]}],projects:[],skills:[{category:'Languages',items:[{text:'Python'}]}]};
let profile={configured:true,draft_revision_id:'revision-1',approved_revision_id:null,draft:{content}};
async function api(url, options={}) { calls.push({url,options});
 if(url==='/api/v1/settings')return {timezone:'America/Chicago',demo_mode:true};
 if(url==='/api/v1/browser/devices')return {devices:[{device_id:'browser-1',created_at:'2026-09-20T16:00:00Z'}]};
 if(url.startsWith('/api/v1/resume-lab/standards'))return {standards:[{name:'My resume',standard_version_id:'version-1',document_url:'/api/v1/resume-lab/standards/version-1/document',preview_url:'/api/v1/resume-lab/standards/version-1/document?disposition=inline'},{name:'Source only',standard_version_id:'version-2'}]};
 if(url==='/api/v1/career-profile' && !options.method)return profile;
 if(url==='/api/v1/career-profile' && options.method==='POST'){const body=JSON.parse(options.body);profile={...profile,draft_revision_id:'revision-2',draft:{content:body.content}};return profile;}
 if(url==='/api/v1/career-profile/approve'){profile={...profile,approved_revision_id:profile.draft_revision_id};return profile;}
 throw Error('Unexpected request '+url);
}
const resumeMutation = (id, kind, url, payload) => api(url,{method:'POST',body:JSON.stringify(payload)});
const loadApplications = async()=>{};
`;
const server=createServer(async(req,res)=>{
 try {
  if(req.url==='/') {res.setHeader('Content-Type','text/html');res.end(`<html><head><link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/settings-view.css"></head><body><main style="margin:0;padding:24px"><span id="demo-badge" hidden>Demo</span><p id="notice"></p><section id="settings"></section><section id="career" hidden></section><section id="ops" hidden></section></main><script>${setup}</script><script src="/settings-view.js"></script><script>initializeSettingsView();loadSettingsPage();</script></body></html>`);return;}
  const file=req.url.slice(1);
  if(!['styles.css','settings-view.css','settings-view.js'].includes(file)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile(path.join(root,'job_search/web',file)));
 }catch(error){res.writeHead(500);res.end(String(error));}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({channel:'chromium',headless:true});
try {
 const page=await browser.newPage({viewport:{width:1280,height:1000}});const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(`http://127.0.0.1:${server.address().port}`);
 await page.getByText('Connect another browser',{exact:true}).waitFor();
 assert.equal(await page.locator('.settings-group').count(),4);
 assert.equal(await page.locator('#settings-list').isVisible(),false);
 assert.equal(await page.locator('.settings-device').innerText().then(text=>text.includes('Browser connected')),true);
 await page.evaluate(()=>loadSettingsPage('stored-records'));
 assert.equal(await page.locator('#stored-records-list a').innerText(),'Systems Engineer');
 assert.equal(await page.locator('#stored-records-list a').getAttribute('href'),'#applications/earlier/overview');
 assert.equal(await page.locator('#stored-records-list a').count(),1);
 await page.evaluate(()=>loadSettingsPage('resumes'));
 assert.equal(await page.getByRole('link',{name:'View document'}).getAttribute('href'),'/api/v1/resume-lab/standards/version-1/document?disposition=inline');
 assert.match(await page.locator('#saved-resume-list').innerText(),/downloadable document is not recorded/);
 await page.evaluate(async()=>{$('#settings').hidden=true;$('#career').hidden=false;await loadCareerProfile();});
 assert.match(await page.locator('.career-saved-summary').first().innerText(),/Example Candidate/);
 assert.equal(await page.getByLabel('Full name',{exact:true}).isVisible(),false);
 await page.getByText('Edit contact details',{exact:true}).click();
 await page.getByLabel('Full name',{exact:true}).fill('Updated Candidate');
 assert.equal(await page.evaluate(()=>state.careerDirty),true);
 await page.getByRole('button',{name:'Refresh profile'}).click();
 assert.equal(await page.getByLabel('Full name',{exact:true}).inputValue(),'Updated Candidate');
 assert.match(await page.locator('#notice').innerText(),/Save your profile edits/);
 await page.getByRole('button',{name:'Save draft',exact:true}).click();
 await page.waitForFunction(()=>!state.careerDirty);
 assert.equal(await page.evaluate(()=>JSON.parse(calls.find(call=>call.url==='/api/v1/career-profile'&&call.options.method==='POST').options.body).expected_revision_id),'revision-1');
 await page.getByLabel('I reviewed this saved profile').check();
 await page.getByRole('button',{name:'Approve saved facts'}).click();
 await page.waitForFunction(()=>state.careerProfile.approved_revision_id==='revision-2');
 assert.equal(await page.evaluate(()=>JSON.parse(calls.find(call=>call.url==='/api/v1/career-profile/approve').options.body).revision_id),'revision-2');
 for(const width of [390,768,1280,1440]){
  await page.setViewportSize({width,height:1000});
  for(const theme of ['light','dark']){
   await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
   for(const view of ['settings','career','ops']){
    await page.evaluate(view=>{for(const name of ['settings','career','ops'])$('#'+name).hidden=name!==view;if(view==='settings')renderSettingsSubview();},view);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,view+' '+width+' '+theme);
   }
  }
 }
 assert.deepEqual(errors,[]);
 await page.evaluate(()=>{$('#ops').hidden=true;$('#career').hidden=false;});
 await page.screenshot({path:path.join(output,'career-dark.png'),fullPage:true});
 console.log('ok (settings, saved resumes, stored drafts, profile editing/approval, 24 responsive page/theme checks)');
}finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
