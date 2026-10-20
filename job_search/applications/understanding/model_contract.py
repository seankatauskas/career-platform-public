"""Bounded context and provider schemas; stored findings retain exact source spans."""
from .contracts import KINDS
from .recipes import FACT_FIELDS, TERMINAL_OUTCOMES
from .evidence import source_blocks
from copy import deepcopy

def model_payload(context):
    """Inference needs semantic context, not the execution/recovery descriptor.

    The full descriptor remains authoritative for projection and version checks.
    Keep every supplied source and current record; omit copied job snapshots,
    submission answers/documents, provenance, and precomputed mutation previews.
    """
    candidates = []
    for candidate in context.candidates:
        job = candidate.get("job") or {}
        snapshot = job.get("recorded_snapshot") or {}
        summary = {key: job[key] for key in ("id", "title", "employer", "sources", "job_url", "posted_at", "location") if job.get(key) is not None}
        for key in ("title", "employer", "job_url", "posted_at", "location"):
            if not summary.get(key) and snapshot.get(key) is not None:
                summary[key] = snapshot[key]
        candidates.append({"id": candidate["id"], "job": summary,
            "retrieval_reasons": list(candidate.get("retrieval_reasons", [])),
            "lifecycle": {key: value for key, value in candidate.get("lifecycle", {}).items()
                          if key in {"disposition", "outcome", "pursuit_no"}}})
    semantic_fields = {"id", "status", "kind", "created_at", "occurred_at", "description", "due_at"} | set().union(*FACT_FIELDS.values())
    records = {application_id: {
        kind: [{key: value for key, value in record.items() if key in semantic_fields} for record in items]
        for kind, items in groups.items()}
        for application_id, groups in context.context.get("records", {}).items()}
    return {"sources": source_blocks(context), "candidates": candidates,
            "candidate_retrieval": dict(context.context.get("candidate_retrieval", {"method": "unspecified"})),
            "catalog_candidates": list(context.context.get("catalog_candidates", [])),
            "catalog_retrieval": dict(context.context.get("catalog_retrieval", {})),
            "coverage": dict(context.coverage), "records": records,
            "thread_associations": context.context.get("thread_associations", []),
            "thread_coverage": context.context.get("thread_coverage", {"complete": False}),
            "association": dict(context.context.get("mail_projection", {}))}


def identity_payload(payload, findings):
    """Identity sees source semantics, never extraction's forced-null decisions.

    Retrieval matches are recall machinery, not competing employer assertions.
    Existing reviewed links remain explicit; unassigned extraction placeholders
    carry no identity judgment and must not anchor the matching stage.
    """
    result = deepcopy(payload)
    previous = result.pop('association', {}).get('target_application_id')
    if previous:
        result['reviewed_message_association'] = {'target_application_id': previous}
    for candidate in result['candidates']:
        candidate.pop('retrieval_reasons', None)
    result['findings'] = []
    for key, (group, finding) in findings.items():
        semantic = {k: deepcopy(v) for k, v in finding.items() if k not in {'target_id', 'confidence'}}
        if isinstance(semantic.get('value'), dict):
            semantic['value'] = {k: v for k, v in semantic['value'].items() if v is not None}
        result['findings'].append({'finding_id': key, 'category': group, 'finding': semantic})
    return result


def _object(properties):
    """Strict structured providers require every declared field to be required."""
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def understanding_schema(context):
    text = {"type": "string", "maxLength": 2000}
    confidence = _nullable({"type": "number", "minimum": 0, "maximum": 1})
    target = {"type": ["string", "null"], "enum": [candidate["id"] for candidate in context.candidates] + [None]}
    evidence = evidence_schema(context)
    common = {"target_id": target, "evidence": evidence, "confidence": confidence}
    catalog_target = {"type": ["string", "null"], "enum": [c["id"] for c in context.context.get("catalog_candidates", [])] + [None]}
    facts = []
    for kind, fields in FACT_FIELDS.items():
        properties = {field: _nullable(text) for field in sorted(fields)}
        if kind == "submission":
            properties["status"] = {"type": ["string", "null"], "enum": [
                "attempted", "submitted", "received", "confirmed", "failed", "retracted", "pending_verification", None]}
        if kind == "outcome":
            properties["outcome"] = {"type": "string", "enum": sorted(TERMINAL_OUTCOMES)}
        record_field = {"submission": "submission_id", "assessment": "assessment_id", "interview": "interview_id", "offer": "offer_id"}.get(kind)
        if record_field:
            ids = [record["id"] for groups in context.context.get("records", {}).values()
                   for record in groups.get(kind + "s", [])]
            properties[record_field] = {"type": ["string", "null"], "enum": list(dict.fromkeys(ids)) + [None]}
        if "participants" in properties:
            properties["participants"] = _nullable({"type": "array", "items": text, "maxItems": 100})
        if "employer_confirmed" in properties:
            properties["employer_confirmed"] = _nullable({"type": "boolean"})
        if "terms" in properties:
            # Offer terms deliberately permit arbitrary evidenced keys. A JSON
            # string avoids an open object in the provider's strict schema; only
            # this transport field is decoded back into the existing domain DTO.
            properties["terms"] = _nullable({"type": "string", "description": "JSON-encoded object containing exact evidenced offer terms; null when unknown."})
        facts.append(_object({"kind": {"type": "string", "enum": [kind]},
            "value": _object(properties) if kind in {"submission", "outcome"} else {"anyOf": [text, _object(properties)]}, **common}))
    groups = {
        "associations": {"anyOf": [
            _object({"kind": {"type": "string", "enum": ["application"]}, **common}),
            _object({"kind": {"type": "string", "enum": ["job"]}, **common, "target_id": catalog_target})]},
        "facts": {"anyOf": facts},
        "requests": _object({"kind": {"type": "string", "enum": sorted(KINDS["requests"])}, **common,
            "outcome": {"type": "string", "minLength": 1, "maxLength": 2000},
            "requirement": {"type": "string", "enum": ["required", "optional", "unclear"]},
            "responsible_party": {"type": "string", "enum": ["applicant", "employer", "other", "unclear"]},
            "channel": {"type": "string", "enum": ["email", "booking_link", "portal", "phone", "other", "unclear"]}}),
        "temporal_facts": _object({"kind": {"type": "string", "enum": sorted(KINDS["temporal_facts"])},
            "wording": text, "normalized": _nullable(text), "timezone": _nullable(text),
            "missing": {"type": "array", "items": text, "maxItems": 20}, "evidence": evidence, "confidence": confidence})}
    return _object({"relevance": {"type": "string", "enum": ["career_related", "unrelated", "uncertain"]},
        **{group: {"type": "array", "maxItems": 20, "items": schema} for group, schema in groups.items()},
        "uncertainties": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1, "maxLength": 2000}}})



def evidence_schema(context):
    ids = [block["block_id"] for source in source_blocks(context) for block in source["blocks"]]
    return {"type": "array", "minItems": 1, "maxItems": 5, "items": _object({
        "block_id": {"type": "string", "enum": ids}})}


def extraction_schema(context):
    from dataclasses import replace
    isolated = replace(context, candidates=(), context={})
    return understanding_schema(isolated)


def binding_schema(context, findings):
    """Make incompatible record kinds and cross-application IDs inexpressible."""
    variants = []
    for key, (group, finding) in findings.items():
        targets = [c['id'] for c in context.candidates]
        if group == 'associations':
            targets += [c['id'] for c in context.context.get('catalog_candidates', [])]
        record_kind = finding.get('kind') if group == 'facts' and isinstance(finding.get('value'), dict) else None
        typed_records = record_kind in {'interview', 'assessment', 'offer'}
        # Most findings have no record selector. One target enum avoids
        # multiplying the full evidence vocabulary by every candidate.
        target_groups = [[t] for t in [*targets, None]] if typed_records else [[*targets, None]]
        for target_group in target_groups:
            target = target_group[0] if len(target_group) == 1 else None
            records = []
            if target and group == 'facts' and finding['kind'] in {'interview', 'assessment', 'offer'} and isinstance(finding.get('value'), dict):
                records = [r['id'] for r in context.context.get('records', {}).get(target, {}).get(finding['kind'] + 's', [])]
            variants.append(_object({
                'finding_id': {'type': 'string', 'enum': [key]},
                'target_id': {'type': ['string', 'null'], 'enum': target_group},
                'record_id': {'type': ['string', 'null'], 'enum': list(dict.fromkeys(records)) + [None]},
                'evidence': evidence_schema(context)}))
    return _object({'bindings': {'type': 'array', 'maxItems': 80, 'items': {'anyOf': variants}},
        'uncertainties': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}})


def verification_schema(ids):
    return _object({'checks': {'type': 'array', 'maxItems': 80, 'items': _object({
        'finding_id': {'type': 'string', 'enum': ids},
        'verdict': {'type': 'string', 'enum': ['supported', 'unsupported', 'uncertain']}})},
        'coverage': {'type': 'string', 'enum': ['complete', 'possibly_missing']}})


def support_inventory_schema(context, topics):
    choices = {'personal_communication': ['substantive_personal', 'routine_or_automated', 'uncertain'],
               'temporal_commitment': ['actionable_commitment', 'descriptive_response_estimate', 'uncertain']}
    return _object({'support': {'type': 'array', 'minItems': len(topics), 'maxItems': len(topics),
        'items': {'anyOf': [_object({'topic': {'type': 'string', 'enum': [topic]},
            'classification': {'type': 'string', 'enum': choices[topic]}, 'evidence': evidence_schema(context)})
                          for topic in topics]}}})


def canonical_identity_schema(context, findings):
    """One message has one reviewed identity; record selectors never carry targets."""
    targets = [c['id'] for c in context.candidates] + [c['id'] for c in context.context.get('catalog_candidates', [])]
    records = canonical_record_schema(context, findings)
    return _object({'target_id': {'type': ['string', 'null'], 'enum': targets + [None]},
        'resolution': {'type': 'string', 'enum': ['matched', 'ambiguous', 'conflicting']},
        'evidence': evidence_schema(context), 'records': records,
        'uncertainties': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}}})


def canonical_record_schema(context, findings, target=None, *, selected=False):
    variants = []
    for key, (group, finding) in findings.items():
        if group != 'facts' or finding['kind'] not in {'interview', 'assessment', 'offer'} or not isinstance(finding.get('value'), dict):
            continue
        applications = [target] if selected else [c['id'] for c in context.candidates]
        ids = [r['id'] for app in applications for r in context.context.get('records', {}).get(app, {}).get(finding['kind'] + 's', [])]
        variants.append(_object({'finding_id': {'type': 'string', 'enum': [key]},
            'record_id': {'type': ['string', 'null'], 'enum': list(dict.fromkeys(ids)) + [None]}}))
    return {'type': 'array', 'minItems': len(variants), 'maxItems': len(variants),
        'items': {'anyOf': variants} if variants else _object({
            'finding_id': {'type': 'string', 'enum': ['__none__']}, 'record_id': {'type': 'null'}})}
