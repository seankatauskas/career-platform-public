(function (root) {
  "use strict";

  const PRIVATE = /\b(race|racial|ethnic|ethnicity|gender|sex|sexual orientation|pronouns?|transgender|nonbinary|male|female|18 or older|over 18|hispanic|latino|native american|pacific islander|equal employment|equal opportunity|eeo|demographic|disabilit(?:y|ies)|veteran|military status|protected class|work authori[sz]ation|legally authori[sz]ed|right to work|eligible to work|employment eligibility|sponsorship|sponsor|immigration|visa|citizen|citizenship)\b/i;
  const FORBIDDEN = /\b(password|passcode|captcha|salary|base salary|compensation|pay expectation|desired pay|expected pay|desired annual|desired base|date of birth|birth date|marital status|religion|certif(?:y|ication)|attest(?:ation)?|acknowledge|electronic signature|signature|i agree|consent|background check|terms and conditions|privacy policy|truthful|accurate information|upload|resume|curriculum vitae|cover letter|portfolio file|submit application|final submit)\b/i;
  const ALLOWED_TEXT_TYPES = new Set(["text", "email", "tel", "url", "search", "month"]);
  const CAPTURE_KINDS = new Set(["approved_answer", "private_answer"]);

  function normalize(value) {
    return String(value || "").normalize("NFKC").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  }

  function isForbidden(value) { return FORBIDDEN.test(normalize(value)); }
  function isPrivate(value) { return PRIVATE.test(normalize(value)) && !isForbidden(value); }
  function isSensitive(value) { return isPrivate(value) || isForbidden(value); }

  function labelText(element) {
    const values = [];
    if (element.labels) {
      Array.from(element.labels).forEach((label) => values.push(label.textContent || ""));
    }
    ["aria-label", "placeholder", "name", "id", "autocomplete"].forEach((name) => {
      if (element.getAttribute) values.push(element.getAttribute(name) || "");
    });
    const fieldset = element.closest ? element.closest("fieldset") : null;
    const legend = fieldset && fieldset.querySelector ? fieldset.querySelector("legend") : null;
    if (legend) values.unshift(legend.textContent || "");
    return values.join(" ").replace(/\s+/g, " ").trim().slice(0, 500);
  }

  function optionLabel(element) {
    const labels = [];
    if (element.labels) {
      Array.from(element.labels).forEach((label) => labels.push(label.textContent || ""));
    }
    if (element.getAttribute) labels.push(element.getAttribute("aria-label") || "");
    labels.push(element.textContent || "", element.value || "");
    return labels.join(" ").replace(/\s+/g, " ").trim().slice(0, 500);
  }

  function controlType(element) {
    const tag = String(element.tagName || "").toLowerCase();
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    if (tag !== "input") return null;
    const type = String(element.type || "text").toLowerCase();
    if (type === "radio") return "radio_group";
    if (type === "checkbox") return "checkbox_group";
    return ALLOWED_TEXT_TYPES.has(type) ? "text" : null;
  }

  function safeControl(element, metadata) {
    if (!controlType(element) || element.disabled || element.readOnly) return false;
    return !isForbidden(metadata);
  }

  function genericKind(metadata) {
    const value = normalize(metadata);
    if (isForbidden(value)) return null;
    if (isPrivate(value)) return "private_answer";
    if (/\bfirst name\b/.test(value) || /\bgiven name\b/.test(value)) return "first_name";
    if (/\blast name\b/.test(value) || /\bfamily name\b/.test(value) || /\bsurname\b/.test(value)) return "last_name";
    if (/\bfull name\b/.test(value) || value === "name") return "full_name";
    if (/\be mail\b/.test(value) || /\bemail\b/.test(value)) return "email";
    if (/\bphone\b/.test(value) || /\btelephone\b/.test(value) || /\bmobile\b/.test(value)) return "phone";
    if (/\blinked ?in\b/.test(value)) return "linkedin_url";
    if (/\bpersonal (?:site|website)\b/.test(value) || /\bportfolio url\b/.test(value)) return "portfolio_url";
    if (/\baddress line 2\b/.test(value) || /\baddress2\b/.test(value)) return "address_line2";
    if (/\bstreet address\b/.test(value) || /\baddress line 1\b/.test(value) || /\baddress1\b/.test(value)) return "address_line1";
    if (/\bpostal\b/.test(value) || /\bzip(?: code)?\b/.test(value)) return "postal_code";
    if (/\bcity\b/.test(value)) return "city";
    if (/\bstate\b/.test(value) || /\bprovince\b/.test(value)) return "state";
    if (/\bcountry\b/.test(value)) return "country";
    if (/\b(?:employer|company|organization)\b/.test(value)) return "work_employer";
    if (/\bjob title\b/.test(value) || /\bposition title\b/.test(value)) return "work_title";
    if (/\bstart month\b/.test(value)) return "work_start_month";
    if (/\bstart year\b/.test(value)) return "work_start_year";
    if (/\bend month\b/.test(value)) return "work_end_month";
    if (/\bend year\b/.test(value)) return "work_end_year";
    if (/\b(?:role|work) (?:summary|description)\b/.test(value)) return "work_summary";
    return "approved_answer";
  }

  function workContainer(element) {
    return element.closest ? element.closest("[data-work-experience], .work-experience, .experience, fieldset") : null;
  }

  function groupFor(element, controls) {
    const type = String(element.type || "").toLowerCase();
    const name = element.getAttribute ? element.getAttribute("name") || "" : "";
    const fieldset = element.closest ? element.closest("fieldset") : null;
    return controls.filter((candidate) => {
      if (String(candidate.type || "").toLowerCase() !== type) return false;
      if (candidate.form !== element.form) return false;
      const candidateName = candidate.getAttribute ? candidate.getAttribute("name") || "" : "";
      const candidateFieldset = candidate.closest ? candidate.closest("fieldset") : null;
      if (fieldset && candidateFieldset !== fieldset) return false;
      return name ? candidateName === name : candidateFieldset === fieldset;
    });
  }

  function describe(rootNode, ats, overrideKind) {
    const elements = new Map();
    const descriptors = [];
    const containers = [];
    const visited = new Set();
    const controls = Array.from(rootNode.querySelectorAll("input, textarea, select"));
    controls.forEach((element) => {
      if (visited.has(element)) return;
      const control = controlType(element);
      if (!control) return;
      const grouped = control === "radio_group" || control === "checkbox_group"
        ? groupFor(element, controls)
        : [element];
      grouped.forEach((item) => visited.add(item));
      let metadata = labelText(element);
      const options = [];
      const optionElements = new Map();
      if (control === "select") {
        Array.from(element.options || []).forEach((option, index) => {
          const label = String(option.textContent || option.label || option.value || "").trim();
          if (!label || option.disabled || !String(option.value || "").trim()) return;
          const optionId = `option-${index}`;
          options.push({ option_id: optionId, label: label.slice(0, 500) });
          optionElements.set(optionId, option);
        });
      } else if (control === "radio_group" || control === "checkbox_group") {
        grouped.forEach((option, index) => {
          const label = optionLabel(option);
          if (!label || option.disabled) return;
          const optionId = `option-${index}`;
          options.push({ option_id: optionId, label });
          optionElements.set(optionId, option);
        });
        metadata = `${metadata} ${options.map((item) => item.label).join(" ")}`.trim().slice(0, 500);
      }
      if (!safeControl(element, metadata)) return;
      const kind = (overrideKind && overrideKind(element, metadata)) || genericKind(metadata);
      if (!kind) return;
      const fieldId = `field-${descriptors.length}`;
      const descriptor = { field_id: fieldId, kind, prompt: metadata, control, options };
      if (kind.startsWith("work_")) {
        const container = workContainer(element);
        let historyIndex = container ? containers.indexOf(container) : 0;
        if (container && historyIndex < 0) {
          containers.push(container);
          historyIndex = containers.length - 1;
        }
        descriptor.history_index = Math.max(0, historyIndex);
      }
      descriptors.push(descriptor);
      elements.set(fieldId, { element, grouped, optionElements, descriptor, metadata });
    });
    return { ats, descriptors, elements };
  }

  function setNativeValue(element, value) {
    const prototype = element.tagName && String(element.tagName).toLowerCase() === "textarea"
      ? root.HTMLTextAreaElement && root.HTMLTextAreaElement.prototype
      : root.HTMLInputElement && root.HTMLInputElement.prototype;
    const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
  }

  function setNativeChecked(element, value) {
    const prototype = root.HTMLInputElement && root.HTMLInputElement.prototype;
    const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, "checked");
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.checked = value;
  }

  function dispatch(element) {
    ["input", "change"].forEach((name) => {
      if (element.dispatchEvent && typeof Event !== "undefined") {
        element.dispatchEvent(new Event(name, { bubbles: true }));
      }
    });
  }

  function applyOne(record, assignment) {
    const { element, metadata, descriptor, optionElements } = record;
    if (!safeControl(element, metadata) || isForbidden(metadata)) return false;
    if (descriptor.control === "text" || descriptor.control === "textarea") {
      if (String(element.value || "").trim() || typeof assignment.value !== "string") return false;
      setNativeValue(element, assignment.value);
      dispatch(element);
      return true;
    }
    const current = descriptor.control === "select"
      ? String(element.value || "").trim()
      : Array.from(record.grouped).some((item) => Boolean(item.checked));
    if (current) return false;
    let selected = Array.isArray(assignment.option_ids) ? assignment.option_ids : [];
    if (!selected.length && typeof assignment.value === "string") {
      const wanted = normalize(assignment.value);
      selected = descriptor.options
        .filter((item) => normalize(item.label) === wanted)
        .map((item) => item.option_id);
    }
    if (!selected.length || (descriptor.control !== "checkbox_group" && selected.length !== 1)) return false;
    if (selected.some((optionId) => !optionElements.has(optionId))) return false;
    if (descriptor.control === "select") {
      const option = optionElements.get(selected[0]);
      element.value = option.value;
      dispatch(element);
      return true;
    }
    selected.forEach((optionId) => {
      const option = optionElements.get(optionId);
      setNativeChecked(option, true);
      dispatch(option);
    });
    return true;
  }

  function applyAssignments(description, assignments) {
    let filled = 0;
    (assignments || []).forEach((assignment) => {
      const record = description.elements.get(assignment.field_id);
      if (record && applyOne(record, assignment)) filled += 1;
    });
    return filled;
  }

  function snapshot(description) {
    const answers = [];
    description.elements.forEach((record, fieldId) => {
      const descriptor = record.descriptor;
      if (!CAPTURE_KINDS.has(descriptor.kind) || isForbidden(record.metadata)) return;
      if (descriptor.control === "text" || descriptor.control === "textarea") {
        const value = String(record.element.value || "").trim();
        if (value) answers.push({ field_id: fieldId, value });
        return;
      }
      const selected = [];
      record.optionElements.forEach((element, optionId) => {
        if ((descriptor.control === "select" && element.selected) ||
            (descriptor.control !== "select" && element.checked)) selected.push(optionId);
      });
      if (selected.length) answers.push({ field_id: fieldId, option_ids: selected });
    });
    return answers;
  }

  const api = {
    normalize, isSensitive, isPrivate, isForbidden, safeControl, genericKind,
    describe, applyOne, applyAssignments, snapshot
  };
  root.JobAutofillCommon = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
