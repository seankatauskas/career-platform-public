(function (root) {
  'use strict';
  const uploaded = new WeakMap();
  const resumeLabel = /\b(resume|cv|curriculum vitae)\b/i;
  const otherLabel = /cover[\s_-]*letter|portfolio|additional|autofill/i;
  function metadata(input) {
    return [input.id, input.name, input.getAttribute('aria-label'),
      ...Array.from(input.labels || []).map(l => l.textContent)].join(' ').replace(/[_-]/g, ' ');
  }
  function candidates(doc) {
    return Array.from(doc.querySelectorAll('input[type="file"]')).filter(input => {
      if (input.disabled) return false;
      let label = metadata(input);
      // Ashby can use generated IDs and a separate label within its field wrapper.
      for (let parent = input.parentElement, depth = 0;
           !resumeLabel.test(label) && parent && depth < 4; parent = parent.parentElement, depth++) {
        if (parent.querySelectorAll('input[type="file"]').length !== 1 || parent.tagName === 'FORM') break;
        label += ' ' + Array.from(parent.querySelectorAll('label')).map(l => l.textContent).join(' ');
      }
      return resumeLabel.test(label) && !otherLabel.test(label);
    });
  }
  function inspect(doc) {
    const inputs = candidates(doc);
    if (inputs.length !== 1) return {status: 'manual', message: 'Attach your resume manually; the resume field could not be identified uniquely.'};
    const input = inputs[0];
    if (input.files?.length || uploaded.has(input)) return {status: 'existing', message: 'Existing resume attachment left unchanged.'};
    const accept = (input.accept || '').toLowerCase().split(',').map(s => s.trim());
    if (input.accept && !accept.some(s => ['.pdf', 'application/pdf', 'application/*', '*/*'].includes(s)))
      return {status: 'manual', message: 'This field does not accept PDF resumes; attach manually.'};
    return {status: 'ready'};
  }
  async function attach(doc, resume) {
    const state = inspect(doc);
    if (state.status !== 'ready') return state;
    if (!resume || typeof resume.content_base64 !== 'string' || resume.content_base64.length > 28 * 1024 * 1024)
      throw new Error('Saved resume is unavailable. Attach it manually.');
    const bytes = Uint8Array.from(atob(resume.content_base64), c => c.charCodeAt(0));
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
      .map(b => b.toString(16).padStart(2, '0')).join('');
    if (hash !== resume.sha256 || bytes.length > 20 * 1024 * 1024 ||
        new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('Saved resume failed verification. Attach it manually.');
    // Recheck after asynchronous hashing; never replace a user's intervening upload.
    const current = inspect(doc);
    if (current.status !== 'ready') return current;
    const input = candidates(doc)[0];
    const file = new File([bytes], resume.filename, {type: 'application/pdf'});
    const transfer = new DataTransfer(); transfer.items.add(file);
    input.files = transfer.files;
    uploaded.set(input, hash);
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    // This confirms delivery to the file control, not the employer's upload result.
    return {status: 'provided', filename: file.name, sha256: hash,
      message: `Resume ${file.name} supplied to the form. Check that the website finishes uploading it.`};
  }
  root.JobResumeUpload = {inspect, attach};
})(globalThis);
