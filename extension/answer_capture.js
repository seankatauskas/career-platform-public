(function (root) {
  'use strict';
  // Reading a user's answers has a different contract from assigning autofill.
  const MAX_FIELDS = 400, MAX_VALUE = 64000, MAX_BYTES = 1024 * 1024;
  const TEXT_TYPES = new Set(['text','email','tel','url','search','number','date','datetime-local','month','week','time','range','color']);
  const SECRET = /password|passcode|captcha|csrf|authenticat(?:ion|or).*code|one.?time.?code|security.?code|credit.?card|card.?number|social.?security|\bssn\b/i;
  const encoder = new TextEncoder();
  const SELECTED_LABELS='[class*="singleValue"],[class*="single-value"],[class*="multiValueLabel"],[class*="multi-value__label"],[data-selected-value]';
  function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
  function clip(value, limit) { return Array.from(String(value)).slice(0,limit).join(''); }
  function text(element) { return clean(element?.innerText || element?.textContent); }
  function labelContent(element) {
    if(!element) return '';
    const copy=element.cloneNode(true);
    copy.querySelectorAll('input,textarea,select,[contenteditable],[role="textbox"],[role="combobox"]').forEach(control=>control.remove());
    copy.querySelectorAll(SELECTED_LABELS).forEach(control=>control.remove());
    return clean(copy.textContent);
  }
  function refs(element, attribute) {
    const scope = element.getRootNode();
    return (element.getAttribute(attribute) || '').split(/\s+/).map(id => text(scope.getElementById?.(id))).filter(Boolean).join(' ');
  }
  function label(element) {
    return refs(element, 'aria-labelledby') || clean(element.getAttribute('aria-label'))
      || Array.from(element.labels || []).map(labelContent).filter(Boolean).join(' ')
      || labelContent(element.closest('label')) || clean(element.getAttribute('placeholder'));
  }
  function visible(element) {
    if (element.hidden || element.closest('[hidden],[aria-hidden="true"],[inert]')) return false;
    const style = element.ownerDocument.defaultView.getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
  }
  function controls(scope) {
    const found = Array.from(scope.querySelectorAll('input,textarea,select,[contenteditable=""],[contenteditable="true"],[contenteditable="plaintext-only"],[role="textbox"],[role="combobox"],[role="checkbox"],[role="radio"],[role="listbox"]'));
    for (const host of scope.querySelectorAll('*')) if (host.shadowRoot) found.push(...controls(host.shadowRoot));
    return found;
  }
  function collect(documentNode) {
    const fields = [], occurrences = new Map();
    let omitted = 0, truncated = 0;
    for (const element of controls(documentNode)) {
      try {
      const tag = element.tagName.toLowerCase(), type = (element.type || '').toLowerCase(), role = element.getAttribute('role');
      if (tag==='input' && ['hidden','password','submit','button','reset','image'].includes(type)) continue;
      // Upload widgets often hide the native file input after attachment.
      if ((!visible(element) && !(type==='file' && element.files?.length)) || element.parentElement?.closest('[contenteditable="true"],[contenteditable="plaintext-only"]')) continue;
      // A custom wrapper around a native input is represented by the input.
      if (!['input','textarea','select'].includes(tag) && element.querySelector('input:not([type="hidden"]),textarea,select,[contenteditable="true"]')) continue;
      const group = element.closest('fieldset,[role="group"],[role="radiogroup"]');
      const section = clip(clean(text(group?.querySelector('legend')) || (group && (refs(group, 'aria-labelledby') || group.getAttribute('aria-label')))),2000);
      let prompt = label(element);
      if (!prompt) {
        const container = element.closest('[data-field],.application-question,.field,.form-field,[class*="fieldEntry"],[class*="FieldEntry"],[class*="question"]');
        prompt = text(container?.querySelector('label,[class*="label"],[class*="Label"]'));
      }
      prompt = clip(clean(prompt || section || element.name || element.id || 'Unlabeled field'),2000);
      if (SECRET.test(`${prompt} ${element.name || ''} ${element.id || ''} ${element.autocomplete || ''}`)) continue;
      let control, value;
      if (tag === 'textarea') { control = 'textarea'; value = element.value; }
      else if (tag === 'select') { control = 'select'; value = Array.from(element.selectedOptions).map(option => option.label || option.textContent || option.value); }
      else if (type === 'checkbox' || type === 'radio') { control = type; value = element.checked; }
      else if (type === 'file') { control = 'file'; value = Array.from(element.files || []).map(file => file.name); }
      else if (role === 'checkbox' || role === 'radio') { control = role; value = element.getAttribute('aria-checked') === 'true'; }
      else if (element.isContentEditable || (role === 'textbox' && tag !== 'input')) { control = 'richtext'; value = element.innerText || element.textContent || ''; }
      else if (role === 'combobox' || role === 'listbox') {
        control = 'select';
        const scope = element.getRootNode();
        const list = scope.getElementById?.(element.getAttribute('aria-controls')) || element;
        value = Array.from(list.querySelectorAll('[aria-selected="true"]')).map(text);
        // React-style selects keep their selected chips outside an empty search
        // input, and may remove the option list entirely when it is closed.
        let container=element.parentElement;
        for(let depth=0; tag==='input' && !value.length && container && depth<3; depth++,container=container.parentElement) {
          if(container.matches('form,body')) break;
          value=Array.from(container.querySelectorAll(SELECTED_LABELS)).map(text).filter(Boolean);
        }
        if (!value.length) value = [element.value || element.getAttribute('aria-valuetext') || text(element)];
      } else if (tag === 'input' && TEXT_TYPES.has(type || 'text')) { control = 'text'; value = element.value; }
      else continue;
      const bounded = item => { const v = String(item), shortened=clip(v,MAX_VALUE); if (v!==shortened) truncated++; return shortened; };
      if(Array.isArray(value) && value.length>400) {value=value.slice(0,400);truncated++;}
      value = Array.isArray(value) ? value.map(bounded) : typeof value === 'boolean' ? value : bounded(value);
      const parts=[section,prompt,clip(element.name || element.id || control,2000)];
      const identity = JSON.stringify(parts);
      const occurrence = occurrences.get(identity) || 0; occurrences.set(identity,occurrence+1);
      fields.push({field_key:JSON.stringify([...parts,occurrence]),prompt,section,control,value});
      } catch (_) { omitted++; } // One unreadable widget must not discard the rest.
    }
    const priority = f => ['textarea','richtext'].includes(f.control) ? 0 : f.control === 'text' ? 1 : 2;
    fields.sort((a,b) => priority(a)-priority(b));
    const kept = []; let bytes = 0;
    for (const field of fields) {
      const size = encoder.encode(JSON.stringify(field)).length;
      if (kept.length >= MAX_FIELDS || bytes+size > MAX_BYTES) { omitted++; continue; }
      kept.push(field); bytes += size;
    }
    return {version:1,fields:kept,omitted_fields:omitted,truncated_values:truncated};
  }
  root.JobAnswerCapture = {collect,MAX_FIELDS,MAX_BYTES};
})(globalThis);
