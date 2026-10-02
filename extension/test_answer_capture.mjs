import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright-core';

const script=await readFile(new URL('./answer_capture.js',import.meta.url),'utf8');
const browser=await chromium.launch({channel:'chromium',headless:true});
try {
  const page=await browser.newPage();
  await page.route('**/*',route=>route.abort());
  await page.setContent(`<label>Question about company infrastructure<textarea id="essay"></textarea></label>
    <label>Email<input id="email" type="email" value="fiction@example.test"></label>
    <div aria-label="Project" contenteditable="true">First paragraph<br>Second paragraph</div>
    <fieldset><legend>Work authorization</legend><label>Yes<input type="radio" name="authorized" checked></label><label>No<input type="radio" name="authorized"></label></fieldset>
    <div role="checkbox" aria-label="Consent" aria-checked="true">I agree</div>
    <button role="combobox" aria-label="Office" aria-controls="offices">Remote</button>
    <div id="offices" hidden><div role="option" aria-selected="true">Remote</div></div>
    <label>Languages<div class="select-control"><div><span class="select__multi-value__label">English</span><span class="select__multi-value__label">Spanish</span><div><input role="combobox" id="languages" aria-controls="closed-options"></div></div></div></label>
    <select aria-label="Skills" multiple><option selected>Python</option><option selected>JavaScript</option></select>
    <label>Password<input type="password" value="secret-password"></label>
    <label>Security code<input value="secret-code"></label>
    <label>Social Security number<input value="secret-ssn"></label>
    <input type="hidden" value="secret-token"><input style="display:none" value="secret-hidden">
    <label>Resume<input id="resume" type="file" hidden></label><div id="shadow"></div>`);
  await page.addScriptTag({content:script});
  const prose='My company experience.\n\nCafe\u0301, Kubernetes, and \u{1f680}.';
  await page.locator('#essay').fill(prose);
  await page.locator('#resume').setInputFiles({name:'My_Resume.pdf',mimeType:'application/pdf',buffer:Buffer.from('fixture')});
  await page.evaluate(()=>{document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<label>Shadow answer<textarea>Inside an open shadow root</textarea></label>';});
  const capture=()=>page.evaluate(()=>JobAnswerCapture.collect(document));
  const first=await capture(), byPrompt=prompt=>first.fields.find(f=>f.prompt===prompt);
  assert.equal(first.fields[0].value,prose);
  assert.equal(byPrompt('Question about company infrastructure').value,prose);
  assert.match(byPrompt('Project').value,/First paragraph\nSecond paragraph/);
  assert.equal(byPrompt('Shadow answer').value,'Inside an open shadow root');
  assert.equal(byPrompt('Yes').section,'Work authorization');
  assert.equal(byPrompt('Yes').value,true); assert.equal(byPrompt('No').value,false);
  assert.equal(byPrompt('Consent').value,true);
  assert.deepEqual(byPrompt('Office').value,['Remote']);
  assert.deepEqual(byPrompt('Skills').value,['Python','JavaScript']);
  assert.deepEqual(byPrompt('Languages').value,['English','Spanish']);
  assert.deepEqual(byPrompt('Resume').value,['My_Resume.pdf']);
  assert(!JSON.stringify(first).includes('secret-'));
  await page.locator('#essay').fill('');
  const cleared=(await capture()).fields.find(f=>f.prompt==='Question about company infrastructure');
  assert.equal(cleared.field_key,byPrompt('Question about company infrastructure').field_key);
  assert.equal(cleared.value,'');
  await page.evaluate(()=>{
    const huge=document.createElement('textarea');huge.setAttribute('aria-label','Long Unicode');huge.value='\u{1f680}'.repeat(64001);document.body.append(huge);
    for(let i=0;i<410;i++) {const input=document.createElement('input');input.setAttribute('aria-label',`Extra ${i}`);input.value='x';document.body.prepend(input);}
  });
  const bounded=await capture();
  assert.equal(bounded.fields.length,400); assert(bounded.omitted_fields>0);
  assert.equal(bounded.truncated_values,1);
  assert.equal(bounded.fields.find(f=>f.prompt==='Long Unicode').value,'\u{1f680}'.repeat(64000));
  assert.equal(bounded.fields.find(f=>f.prompt==='Shadow answer').value,'Inside an open shadow root');
  console.log('ok (answer capture: readable controls, exact Unicode, open shadow roots, hidden upload, secret exclusion, stable edits, prose priority and explicit limits)');
} finally {await browser.close();}
