(function(root) {
  'use strict';
  const hosts = {
    ashby: ['jobs.ashbyhq.com'],
    greenhouse: ['boards.greenhouse.io', 'job-boards.greenhouse.io', 'job-boards.eu.greenhouse.io'],
    lever: ['jobs.lever.co', 'jobs.eu.lever.co']
  };
  function identify(value) {
    let u; try { u = new URL(value); } catch (_) { return null; }
    if (u.protocol !== 'https:' || u.username || u.password || u.port) return null;
    const ats = Object.keys(hosts).find(k => hosts[k].includes(u.hostname));
    if (!ats) return null;
    let p; try { p = u.pathname.split('/').filter(Boolean).map(decodeURIComponent); } catch (_) { return null; }
    let board = '', id = '';
    if (ats === 'greenhouse') {
      if ((p.length === 3 || (p.length === 4 && p[3] === 'confirmation')) && p[1] === 'jobs') [board, , id] = p;
      if (['embed/job_app', 'embed/job_app/confirmation', 'embed/job_board/job'].includes(p.join('/'))) {
        board = u.searchParams.get('for') || ''; id = u.searchParams.get('token') || u.searchParams.get('gh_jid') || '';
      }
      if (!/^\d+$/.test(id)) return null;
    } else {
      if (![2,3].includes(p.length) || (p.length === 3 && !['application', 'apply', 'thanks'].includes(p[2]))) return null;
      [board, id] = p;
      if (!/^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(id)) return null;
      id = id.toLowerCase();
    }
    if (!/^[a-z\d_.-]{1,200}$/i.test(board)) return null;
    return {ats, job_id: id, board, canonical_url: `${u.origin}/${board}${ats === 'greenhouse' ? '/jobs' : ''}/${id}`};
  }
  function sameJob(a, b) { return !!a && !!b && a.ats === b.ats && a.job_id === b.job_id && a.board.toLowerCase() === b.board.toLowerCase(); }
  function submissionRequest(details, job, exactEndpoint = false) {
    if (details.method !== 'POST' || !job) return false;
    const u = new URL(details.url);
    if (u.protocol !== 'https:') return false;
    if (job.ats === 'lever' && hosts.lever.includes(u.hostname))
      return u.pathname === `/${job.board}/${job.job_id}/apply`;
    if (job.ats === 'greenhouse' && [...hosts.greenhouse, 'boards-api.greenhouse.io'].includes(u.hostname))
      return (exactEndpoint ? /\/(?:submit_app|applications)\/?$/ : /\/(?:submit_app|applications)(?:\/|$)/).test(u.pathname)
        || u.pathname === `/v1/boards/${job.board}/jobs/${job.job_id}`
        || u.pathname === `/embed/${job.board}/jobs/${job.job_id}`;
    if (job.ats === 'ashby' && hosts.ashby.includes(u.hostname))
      return u.pathname === '/api/non-user-graphql' && [
        'ApiSubmitApplication', 'SubmitApplication',
        'ApiSubmitSingleApplicationFormAction', 'ApiSubmitMultipleFormsAction'
      ].includes(u.searchParams.get('op'));
    return false;
  }
  function outcome(doc, url, job) {
    const current = identify(url);
    if (job && sameJob(current, job) && /\/thanks\/?$/.test(new URL(url).pathname)) return 'success_route';
    if (current?.ats === 'ashby' && sameJob(current, job)) {
      // Ashby prefixes its customizable acknowledgment with a separate "Success"
      // heading. Detect its scoped status container, including localized copy.
      for (const el of doc.querySelectorAll('.ashby-application-form-success-container [role="status"]')) {
        if (el.getClientRects().length && el.textContent.trim()) return 'success_dom';
      }
      for (const el of doc.querySelectorAll('.ashby-application-form-failure-container, .ashby-application-form-blocked-application-container')) {
        if (el.getClientRects().length && el.textContent.trim()) return 'validation_error';
      }
    }
    // Scope text checks to visible status/headline elements, not job descriptions.
    const nodes = doc.querySelectorAll('[role="alert"], [role="status"], h1, h2, h3, .application-confirmation, .application-success, .post-apply-message, #application_confirmation');
    for (const el of nodes) {
      if (!el.getClientRects().length) continue;
      const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (text.length > 600) continue;
      if (/^(?:thank you|thanks) for applying[!.\s]|^(?:thank you|thanks) for your (?:interest|application)[!.\s]|(?:your application (?:has been |was )?(?:successfully )?(?:submitted|received)|we(?:'ve| have)? received your application)/i.test(text)) return 'success_dom';
      if (/application (?:could not|couldn't|failed to) (?:be )?(?:submitted|sent)|unable to submit|please (?:fix|correct) (?:the )?(?:errors|fields)|please complete (?:the )?captcha/i.test(text)) return 'validation_error';
    }
    return null;
  }
  root.JobTracking = {hosts, identify, sameJob, submissionRequest, outcome};
  if (typeof module !== 'undefined') module.exports = root.JobTracking;
})(typeof globalThis !== 'undefined' ? globalThis : this);
