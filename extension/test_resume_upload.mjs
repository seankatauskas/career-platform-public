import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright-core';

const script=await readFile(new URL('./resume_upload.js',import.meta.url),'utf8');
const pdf=Buffer.from('%PDF-1.7\nSynthetic upload\n%%EOF');
const resume={filename:'resume.pdf',content_base64:pdf.toString('base64'),sha256:createHash('sha256').update(pdf).digest('hex')};
const browser=await chromium.launch({channel:'chromium',headless:true});
try {
  const page=await browser.newPage();
  await page.route('https://fixture.test/**',r=>r.fulfill({body:'<!doctype html><html><body></body></html>',contentType:'text/html'}));
  await page.goto('https://fixture.test');
  async function setup(html) {
    await page.setContent(html);
    await page.addScriptTag({content:script});
    await page.evaluate(()=>{window.changes=0;document.querySelectorAll('input').forEach(e=>e.addEventListener('change',()=>window.changes++));});
  }
  const layouts=[
    '<input type="file" id="resume" hidden accept=".pdf,.docx"><input type="file" id="cover_letter">',
    '<div><h3>Autofill from resume</h3><input type="file"></div><label for="_systemfield_resume">Resume</label><input type="file" id="_systemfield_resume"><label>Cover Letter<input type="file"></label>',
    '<label>Resume/CV<input type="file" name="resume" id="resume-upload-input"></label>'
  ];
  for(const html of layouts) {
    await setup(html);
    const result=await page.evaluate(r=>JobResumeUpload.attach(document,r),resume);
    assert.equal(result.status,'provided');
    assert.equal(await page.evaluate(()=>window.changes),1);
    assert.equal(await page.evaluate(()=>Array.from(document.querySelectorAll('input')).filter(e=>e.files.length).length),1);
    assert.equal((await page.evaluate(r=>JobResumeUpload.attach(document,r),resume)).status,'existing');
    assert.equal(await page.evaluate(()=>window.changes),1);
  }
  for(const html of ['<input type="file" id="cover_letter">','<input type="file" id="resume"><input type="file" name="resume">','<input type="file" id="resume" disabled>','<input type="file" id="resume" accept=".docx">']) {
    await setup(html);
    assert.equal((await page.evaluate(r=>JobResumeUpload.attach(document,r),resume)).status,'manual');
    assert.equal(await page.evaluate(()=>window.changes),0);
  }
  await setup(layouts[0]);
  await page.locator('#resume').setInputFiles({name:'my-choice.pdf',mimeType:'application/pdf',buffer:pdf});
  assert.equal((await page.evaluate(r=>JobResumeUpload.attach(document,r),resume)).status,'existing');
  assert.equal(await page.locator('#resume').evaluate(e=>e.files[0].name),'my-choice.pdf');
  await setup(layouts[0]);
  await assert.rejects(()=>page.evaluate(r=>JobResumeUpload.attach(document,r),{...resume,sha256:'0'.repeat(64)}),/verification/);
  assert.equal(await page.evaluate(()=>window.changes),0);
  console.log('ok (9 resume upload browser checks)');
} finally {await browser.close();}
