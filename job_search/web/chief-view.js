// Chief-of-staff controls use the dashboard's authenticated api/CSRF helper.
let chiefPreferences = null;
let chiefEpoch = 0;
let chiefHistoryOffset = 0;
const chiefCommands = new Map();

async function chiefMutation(path, payload) {
  const fingerprint = path + JSON.stringify(payload);
  if (!chiefCommands.has(fingerprint)) chiefCommands.set(fingerprint, crypto.randomUUID());
  try {
    const result = await api(`/api/v1/chief/${path}`, {method:"POST", body:JSON.stringify({...payload,idempotency_key:chiefCommands.get(fingerprint)})});
    chiefCommands.delete(fingerprint);
    return result;
  } catch(error) {
    if (error.status >= 400 && error.status < 500) chiefCommands.delete(fingerprint);
    throw error;
  }
}

function initializeChief() {
  $("#settings-chief").innerHTML = `
    <div class="section-heading"><div><p class="kicker"><a href="#settings">← Settings</a></p><h2>Your chief of staff</h2></div><button id="chief-refresh" class="quiet" type="button">Refresh</button></div>
    <p class="section-note">Start and finish your day with a clear plan. Important developments and approaching deadlines can reach you between briefings.</p>
    <p id="chief-status" role="status" aria-live="polite"></p>
    <section class="settings-group" aria-labelledby="chief-preferences-heading"><h3 id="chief-preferences-heading">When to hear from Hermes</h3>
      <form id="chief-preferences"><div class="chief-fields">
        <label>Notification mode<select name="mode"><option value="important_developments">Important developments and deadlines</option><option value="risk_only">Only approaching deadlines between briefings</option><option value="briefings_only">Scheduled briefings only</option></select></label>
        <label>Time zone<input name="timezone" value="America/Chicago" readonly></label>
        <label>Morning briefing<input name="morning_time" type="time" value="07:00" required></label>
        <label>Evening briefing<input name="evening_time" type="time" value="19:00" required></label>
        <label class="chief-check"><input name="enabled" type="checkbox">Enable notifications</label>
        <label class="chief-check"><input name="shadow" type="checkbox">Preview only: do not deliver notifications</label>
        <label class="chief-check"><input name="overnight_enabled" type="checkbox">Allow important alerts overnight</label>
        <label class="chief-check"><input name="final_nudge_enabled" type="checkbox">One final deadline reminder</label>
        <label class="chief-check"><input name="ai_enabled" type="checkbox">Use grounded AI to write briefings</label>
        <label class="chief-check"><input name="ready_replies_enabled" type="checkbox">Prepare replies for me to review</label>
        <label class="chief-check"><input name="quiet_hours_enabled" type="checkbox">Use quiet hours</label>
        <label>Quiet hours begin<input name="quiet_start" type="time" value="22:00" required></label>
        <label>Quiet hours end<input name="quiet_end" type="time" value="07:00" required></label>
      </div><details><summary>Alert frequency and deadlines</summary><div class="chief-fields">
        <label>Maximum alerts per day<input name="maximum_alerts_per_day" type="number" min="1" max="30" value="6" required></label>
        <label>Minutes between alerts<input name="minimum_alert_gap_minutes" type="number" min="0" max="1440" value="30" required></label>
        <label>First reminder: minutes before deadline<input name="risk_window_minutes" type="number" min="5" max="1440" value="120" required></label>
        <label>Final reminder: minutes before deadline<input name="final_nudge_minutes" type="number" min="1" max="120" value="30" required></label>
      </div></details><p class="meta">Monday morning includes the week ahead. Friday evening includes your weekly recap. Prepared emails always require your approval.</p>
      <button type="submit">Save preferences</button></form>
    </section>
    <section class="settings-group" aria-labelledby="chief-preview-heading"><div class="section-heading"><h3 id="chief-preview-heading">Your next briefing</h3><div class="actions"><select id="chief-preview-slot" aria-label="Briefing to preview"><option value="morning">Morning</option><option value="evening">Evening</option><option value="week_ahead">Week ahead</option><option value="week_recap">Weekly recap</option></select><button id="chief-preview-button" type="button" class="quiet">Preview</button></div></div><p class="meta">Previewing does not deliver a notification.</p><div id="chief-preview"></div></section>
    <section class="settings-group" aria-labelledby="chief-actions-heading"><h3 id="chief-actions-heading">Emails ready for your review</h3><p class="meta">Check the full recipient list and message before sending. Editing cancels the previous version and creates a new review.</p><div id="chief-actions" class="stack"></div></section>
    <section class="settings-group" aria-labelledby="chief-attention-heading"><h3 id="chief-attention-heading">Needs your attention</h3><p class="meta">Acknowledging an item does not complete its task. Snoozing changes the next reminder, not the deadline.</p><div id="chief-attention" class="stack"></div></section>
    <section class="settings-group" aria-labelledby="chief-delivery-heading"><h3 id="chief-delivery-heading">Telegram delivery reviews</h3><p class="meta">These messages have no confirmed delivery receipt. Record that you received one, or abandon it. Neither choice sends an email or retries the Telegram message.</p><div id="chief-delivery-recovery" class="stack"></div></section>
    <section class="settings-group" aria-labelledby="chief-commitments-heading"><h3 id="chief-commitments-heading">Interview time reviews</h3><p class="meta">Resolve an uncertain confirmation using a time you actually offered. This records the agreed time; it does not accept a calendar invitation.</p><div id="chief-commitments" class="stack"></div></section>
    <section class="settings-group" aria-labelledby="chief-history-heading"><h3 id="chief-history-heading">Briefing history</h3><div id="chief-history" class="stack"></div><button id="chief-history-more" type="button" class="quiet" hidden>Load older briefings</button></section>`;
  $("#chief-refresh").addEventListener("click",loadChief);
  $("#chief-preview-button").addEventListener("click",previewChief);
  $("#chief-history-more").addEventListener("click",()=>loadChiefHistory(true));
  const prefsForm=$("#chief-preferences");
  prefsForm.elements.quiet_hours_enabled.addEventListener('change',()=>{if(prefsForm.elements.quiet_hours_enabled.checked)prefsForm.elements.overnight_enabled.checked=false;});
  prefsForm.elements.overnight_enabled.addEventListener('change',()=>{if(prefsForm.elements.overnight_enabled.checked)prefsForm.elements.quiet_hours_enabled.checked=false;});
  $("#chief-preferences").addEventListener("submit",async (event)=>{
    event.preventDefault();
    if (!chiefPreferences) return;
    const form = event.currentTarget;
    const changes = {};
    for (const element of form.elements) if (element.name) changes[element.name] = element.type === "checkbox" ? element.checked : element.type === "number" ? Number(element.value) : element.value;
    const button = form.querySelector("button"); button.disabled = true;
    try { chiefPreferences = await chiefMutation("preferences",{changes,expected_revision:chiefPreferences.revision}); $("#chief-status").textContent="Preferences saved."; }
    catch(error) { $("#chief-status").textContent = `${error.message} Refresh to load the latest preferences before saving again.`; }
    finally { button.disabled = false; }
  });
}

function chiefAction(label, handler) {
  const button = node("button","quiet",label); button.type="button";
  button.addEventListener("click",async()=>{
    button.disabled=true;
    try {await handler();}
    catch(error) {$("#chief-status").textContent=error.message;}
    finally {button.disabled=false;}
  });
  return button;
}

function renderChiefBriefing(target, row) {
  target.replaceChildren();
  target.append(node("h4","",row.title || "Your briefing"),node("p","chief-prose",row.body || "No briefing text yet."));
  const coverage = row.snapshot?.coverage || row.coverage;
  if (coverage) {
    const details=node("details","chief-coverage");details.append(node("summary","","What this briefing covers"));
    const list=node("dl","settings-list");
    for(const [key,value] of Object.entries(coverage)) {
      const entry=node("div");entry.append(node("dt","",key.replaceAll("_"," ")),node("dd","",typeof value === "object" ? JSON.stringify(value) : String(value)));list.append(entry);
    }
    details.append(list);target.append(details);
  }
  const facts=row.snapshot?.facts || [];
  if(facts.length) {
    const details=node("details");details.append(node("summary","","Supporting facts"));
    for(const fact of facts) {
      const article=node("article","chief-fact");article.append(node("p","chief-prose",fact.title || fact.summary || fact.body || fact.label || fact.ref || "Recorded fact"));
      if(fact.application_id) {const link=node("a","","Open application");link.href=`#applications/${encodeURIComponent(fact.application_id)}/overview`;article.append(link);}
      if(fact.source_at || fact.observed_at) article.append(node("p","meta",`Observed ${opsTime(fact.source_at || fact.observed_at)}`));
      details.append(article);
    }
    target.append(details);
  }
}

async function previewChief() {
  const button=$("#chief-preview-button");button.disabled=true;
  try {renderChiefBriefing($("#chief-preview"),await api(`/api/v1/chief/preview?slot=${encodeURIComponent($("#chief-preview-slot").value)}`));}
  catch(error) {$("#chief-preview").replaceChildren(node("p","meta",error.message));}
  finally {button.disabled=false;}
}

function renderChiefActions(result) {
  const target=$("#chief-actions");target.replaceChildren();
  const rows=result.proposals || [];
  if(!rows.length) target.append(node("p","meta","No emails waiting for review."));
  for(const proposal of rows) {
    const card=node("article","chief-card");card.append(node("h4","",proposal.subject || "Reply"));
    const recipients=(proposal.recipients || []).map(r=>typeof r === "string" ? r : r.address || r.email || "Unknown recipient").join(", ");
    card.append(node("p","meta",`From account: ${proposal.account_id || "Recorded account"}`),node("p","",`To: ${recipients}`),node("p","chief-prose",proposal.body || ""),node("p","meta",`Review expires ${opsTime(proposal.expires_at)}`));
    const confirm=node("label","chief-check");const input=document.createElement("input");input.type="checkbox";confirm.append(input,document.createTextNode("I reviewed the recipients and full message."));card.append(confirm);
    const values={proposal_id:proposal.proposal_id,payload_hash:proposal.payload_hash,source_hash:proposal.source_hash};
    const controls=node("div","actions");
    const send=chiefAction("Send email",async()=>{
      if(!input.checked) return;
      await chiefMutation("actions/decide",{...values,decision:"approved"});
      $("#chief-status").textContent="Approved for sending. Delivery is confirmed separately.";
      await loadChiefActions();
    });send.disabled=true;input.addEventListener("change",()=>{send.disabled=!input.checked;});
    controls.append(send,chiefAction("Cancel",async()=>{await chiefMutation("actions/decide",{...values,decision:"rejected"});await loadChiefActions();}),chiefAction("Review in Telegram",async()=>{await chiefMutation("actions/review",{proposal_id:proposal.proposal_id});$("#chief-status").textContent="Review queued for your private Telegram conversation.";}));card.append(controls);
    const editor=node("details");editor.append(node("summary","","Edit this reply"));const label=node("label","","Message");const area=document.createElement("textarea");area.rows=8;area.maxLength=12000;area.value=proposal.body || "";label.append(area);editor.append(label,chiefAction("Save new version",async()=>{
      if(!area.value.trim()) throw new Error("Enter a message before saving.");
      await chiefMutation("actions/edit",{...values,body:area.value});$("#chief-status").textContent="Previous version cancelled. Review the new message before sending.";await loadChiefActions();
    }));card.append(editor);target.append(card);
  }
}

async function loadChiefActions() {renderChiefActions(await api("/api/v1/chief/actions"));}
async function loadChiefDeliveryRecovery() {
  const result=await api('/api/v1/chief/delivery-recovery');const target=$('#chief-delivery-recovery');target.replaceChildren();
  if(!(result.items || []).length) target.append(node('p','meta','No uncertain Telegram deliveries.'));
  for(const row of result.items || []) {
    const card=node('article','chief-card');card.append(node('h4','',row.title),node('p','meta',`Delivery started ${opsTime(row.delivery_started_at)}. The result is uncertain.`));
    const base={ticket_id:row.ticket_id,expected_revision:row.delivery_revision,payload_sha256:row.payload_sha256,source_version:row.source_version,identity:row.identity};
    const controls=node('div','actions');
    for(const [label,decision] of [['I received it','received'],['Abandon','abandon']]) controls.append(chiefAction(label,async()=>{const saved=await chiefMutation('delivery-recovery',{...base,decision});$('#chief-status').textContent=saved.message;await loadChiefDeliveryRecovery();}));
    card.append(controls);target.append(card);
  }
  if(result.complete===false) target.append(node('p','meta','More delivery reviews remain. Resolve these and refresh.'));
}
async function loadChiefCommitments() {
  const result=await api('/api/v1/chief/commitments');const target=$('#chief-commitments');target.replaceChildren();
  const rows=(result.commitments || []).filter(row=>!['linked_invite','cancelled','dismissed'].includes(row.status));
  if(!rows.length) target.append(node('p','meta','No interview confirmations need review.'));
  for(const row of rows) {
    const card=node('article','chief-card');card.append(node('h4','',`Interview confirmation · ${row.status}`));
    if(row.starts_at) card.append(node('p','',`Recorded time: ${opsTime(row.starts_at)}–${opsTime(row.ends_at)}`));
    if(row.confirmation_evidence_id) card.append(node('p','meta',`Confirmation evidence: ${row.confirmation_evidence_id}`));
    if(row.conflict_json && row.conflict_json!=='{}') card.append(node('p','meta','This confirmation has a recorded conflict. Review the application conversation before selecting a time.'));
    const label=node('label','','Choose the confirmed time from your sent availability');const select=document.createElement('select');select.append(node('option','','Choose a time'));
    select.options[0].value='';
    (row.offered_slots || []).forEach((slot,index)=>{const option=node('option','',`${opsTime(slot.starts_at)}–${opsTime(slot.ends_at)}`);option.value=String(index);select.append(option);});label.append(select);card.append(label);
    const base={commitment_id:row.commitment_id,expected_version:row.version};const controls=node('div','actions');
    controls.append(chiefAction('Record confirmed time',async()=>{
      if(select.value==='') throw new Error('Choose an offered time supported by the confirmation.');
      const slot=row.offered_slots[Number(select.value)];await chiefMutation('commitments/review',{...base,decision:'confirmed',starts_at:slot.starts_at,ends_at:slot.ends_at});await loadChiefCommitments();
    }),chiefAction('Mark cancelled',async()=>{await chiefMutation('commitments/review',{...base,decision:'cancelled'});await loadChiefCommitments();}),chiefAction('Dismiss this interpretation',async()=>{await chiefMutation('commitments/review',{...base,decision:'dismissed'});await loadChiefCommitments();}));card.append(controls);target.append(card);
  }
}
function renderChiefAttention(result) {
  const target=$("#chief-attention");target.replaceChildren();
  const rows=result.items || [];
  if(!rows.length) target.append(node("p","meta","Nothing needs your attention right now."));
  for(const item of rows) {
    const card=node("article","chief-card");card.append(node("h4","",item.title || item.topic || "Career update"),node("p","chief-prose",item.summary || item.payload?.body || ""));
    if(item.due_at) card.append(node("p","meta",`Deadline ${opsTime(item.due_at)}`));
    const base={candidate_id:item.candidate_id,expected_revision:item.revision};const controls=node("div","actions");
    controls.append(chiefAction("Got it",async()=>{await chiefMutation("acknowledge",base);renderChiefAttention(await api("/api/v1/chief/candidates"));}));
    const label=node("label","","Remind me at (your device’s time zone)");const until=document.createElement("input");until.type="datetime-local";label.append(until);controls.append(label,chiefAction("Snooze",async()=>{
      if(!until.value) throw new Error("Choose when to remind you.");
      await chiefMutation("snooze",{...base,until:new Date(until.value).toISOString()});renderChiefAttention(await api("/api/v1/chief/candidates"));
    }));card.append(controls);target.append(card);
  }
  if(result.complete===false) target.append(node("p","meta","Showing the first attention items. Acknowledge or snooze them, then refresh for more."));
}

async function loadChiefHistory(append=false) {
  const result=await api(`/api/v1/chief/history?limit=20&offset=${append ? chiefHistoryOffset : 0}`);
  const target=$("#chief-history");if(!append) target.replaceChildren();
  if(!(result.items || []).length && !append) target.append(node("p","meta","Your saved briefings will appear here."));
  for(const row of result.items || []) {
    const entry=node("details","chief-card");entry.append(node("summary","",`${row.title || "Briefing"} · ${row.local_date || opsTime(row.created_at)} · ${row.status || "saved"}`));
    const body=node("div");entry.append(body);let loaded=false;
    entry.addEventListener("toggle",async()=>{if(entry.open && !loaded) {try {renderChiefBriefing(body,await api(`/api/v1/chief/briefing/${encodeURIComponent(row.briefing_id)}`));loaded=true;} catch(error) {body.textContent=error.message;}}});target.append(entry);
  }
  chiefHistoryOffset=result.next_offset;$("#chief-history-more").hidden=result.complete!==false || chiefHistoryOffset==null;
}

async function loadChief(briefingId=null) {
  const epoch=++chiefEpoch;$("#chief-refresh").disabled=true;$("#chief-status").textContent="Loading your chief of staff…";
  const results=await Promise.allSettled([api("/api/v1/chief/preferences"),loadChiefActions(),api("/api/v1/chief/candidates"),loadChiefHistory(),previewChief(),loadChiefCommitments(),loadChiefDeliveryRecovery()]);
  if(epoch!==chiefEpoch)return;
  if(results[0].status==="fulfilled") {
    chiefPreferences=results[0].value;const form=$("#chief-preferences");
    for(const element of form.elements) if(element.name && element.name in chiefPreferences) {if(element.type==="checkbox") element.checked=Boolean(chiefPreferences[element.name]);else element.value=chiefPreferences[element.name];}
  }
  if(results[2].status==="fulfilled") renderChiefAttention(results[2].value);
  const errors=results.filter(r=>r.status==="rejected");$("#chief-status").textContent=errors.length ? `Some information could not be loaded. ${errors[0].reason.message}` : "";$("#chief-refresh").disabled=false;
  if(typeof briefingId==='string' && /^[A-Za-z0-9._:-]{1,256}$/.test(briefingId)) {
    try {renderChiefBriefing($("#chief-preview"),await api(`/api/v1/chief/briefing/${encodeURIComponent(briefingId)}`));$("#chief-preview").scrollIntoView({block:"start"});}
    catch(error) {$("#chief-status").textContent=error.message;}
  }
}
