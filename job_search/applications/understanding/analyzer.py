"""Extract independently verified findings, then resolve their application identity.

Only the provider boundary uses block citations. Persistence and approval retain
exact source spans and the existing domain contract. No stage accepts changes.
"""
from copy import deepcopy
import json

from job_search.commands import DomainError, encode, digest
from .contracts import KINDS, validate_analysis
from .evidence import resolve_evidence
from .instructions import instructions, semantic_instructions, identity_instructions
from .model_contract import model_payload, identity_payload, understanding_schema, extraction_schema, verification_schema, support_inventory_schema, canonical_identity_schema, canonical_record_schema, _object
from .validation import check, OutputError, safe_path


ANALYSIS_FAILURE_REASONS = {
    'context_budget_exceeded': 'Complete evidence and the required response exceed the configured inference budget.',
    'invalid_json': 'The model response was not a valid JSON object.',
    'evidence_mismatch': 'A cited quote could not be located unambiguously in its exact source revision.',
    'output_truncated': 'The provider stopped before completing the model response.',
    'output_too_large': 'The model response exceeded the bounded analysis size.',
    'invalid_output': 'The model response did not satisfy the analysis contract.',
}


def _fail(code, diagnostic=None):
    error = DomainError(code, ANALYSIS_FAILURE_REASONS[code])
    error.validation_diagnostic = diagnostic
    raise error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate analysis field')
        result[key] = value
    return result


def _empty(relevance='career_related'):
    return dict(relevance=relevance, **{kind: [] for kind in KINDS}, uncertainties=[])


def _issue(exc, stage, category='analysis', position=0):
    reason = getattr(exc, 'reason', None) or getattr(exc, 'code', 'invalid_contract')
    if reason == 'invalid_input':
        reason = 'invalid_contract'
    if reason == 'invalid_output':
        reason = 'invalid_contract'
    issue = dict(stage=stage, category=category, position=position, reason=reason)
    path = safe_path(getattr(exc, 'path', ()))
    if path:
        issue['path'] = path
    return issue


class AnalysisResult(dict):
    """Canonical findings plus content-free recovery metadata for the runtime."""
    def __init__(self, value, issues, repairs, corrections=()):
        super().__init__(value)
        self.validation_diagnostic = {'version': 2, 'issues': issues, 'repairs': repairs}
        if corrections or repairs > 2:
            self.validation_diagnostic.update(version=3, corrections=list(corrections))


class SharedAnalyzer:
    prompt_version = '16-independent-support-and-quarantine'

    def __init__(self, provider, *, max_output_tokens=8192):
        self.provider = provider
        self.max_output_tokens = max_output_tokens
        self.checkpoint = None

    def analyze_with_checkpoints(self, context, checkpoint):
        # A per-call analyzer keeps checkpoint ownership out of shared providers.
        session = SharedAnalyzer(self.provider, max_output_tokens=self.max_output_tokens)
        session.checkpoint = checkpoint
        return session.analyze(context)

    def _generate(self, prompt, payload, schema, name):
        from .checkpoints import bounded_result
        request_key = digest({'prompt': prompt, 'payload': payload, 'schema': schema,
                              'name': name, 'output_tokens': self.max_output_tokens})
        saved = self.checkpoint(request_key) if self.checkpoint else None
        if saved is None:
            try:
                value = self._request(prompt, payload, schema, name)
                saved = {'value': bounded_result(value, schema)}
            except DomainError as exc:
                if exc.code not in ANALYSIS_FAILURE_REASONS:
                    raise
                saved = {'error': exc.code}
            if self.checkpoint:
                self.checkpoint(request_key, saved)
        if 'error' in saved:
            _fail(saved['error'])
        return saved['value']

    def _request(self, prompt, payload, schema, name):
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': encode(payload)}]
        if self.provider.count_tokens_upper_bound(prompt + messages[1]['content'] + encode(schema)) + self.max_output_tokens > self.provider.max_input_tokens:
            _fail('context_budget_exceeded')
        from job_search.inference import InferenceTransportError
        try:
            response = self.provider.generate(messages, json_schema=schema, schema_name=name,
                                              max_output_tokens=self.max_output_tokens, temperature=0.0)
        except InferenceTransportError as exc:
            if str(exc) == 'inference request exceeds the configured context limit':
                _fail('context_budget_exceeded')
            raise
        if getattr(response, 'usage', {}).get('finish_reason') in {'length', 'max_tokens'}:
            _fail('output_truncated')
        if not isinstance(response.text, str):
            _fail('invalid_json')
        try:
            size = len(response.text.encode('utf-8'))
        except UnicodeError:
            _fail('invalid_json')
        if size > 65536:
            _fail('output_too_large')
        try:
            value = json.loads(response.text, object_pairs_hook=_unique_object)
            encode(value)  # Reject non-finite numbers before checkpointing.
        except (TypeError, ValueError, RecursionError, UnicodeError):
            _fail('invalid_json')
        return value

    def _candidate(self, raw, group, schema, context):
        # Validate the wire contract before translating to the stored contract.
        check(raw, schema)
        finding = deepcopy(raw)
        finding['evidence'] = resolve_evidence(finding['evidence'], context)
        if group == 'facts' and isinstance(finding['value'], dict):
            finding['value'] = {k: v for k, v in finding['value'].items() if v is not None}
            if finding['kind'] == 'offer' and isinstance(finding['value'].get('terms'), str):
                try:
                    terms = json.loads(finding['value']['terms'], object_pairs_hook=_unique_object)
                    if not isinstance(terms, dict):
                        raise ValueError()
                    finding['value']['terms'] = terms
                except (TypeError, ValueError, RecursionError):
                    raise OutputError('invalid_json')
        return finding

    def _finding(self, raw, group, schema, context):
        finding = self._candidate(raw, group, schema, context)
        single = _empty()
        single[group] = [finding]
        if group == 'temporal_facts' and finding.get('normalized') and (
                not finding.get('timezone') or finding.get('missing')):
            raise OutputError('incomplete_temporal', ('normalized',))
        validate_analysis(single, context)
        return finding

    def _extract(self, context, payload, prompt):
        schema = extraction_schema(context)
        # Extraction cannot see candidate rank, database IDs or existing records.
        extraction = {k: payload[k] for k in ('sources', 'coverage', 'thread_coverage')}
        extraction_prompt = prompt + ('\nPHASE: EXTRACT. Describe only what the complete email says. '
            'Application matching happens later. All target_id and record ID fields must be null. '
            'Extract application associations as unassigned findings with employer/role evidence. '
            'Return every independent fact, request and time expression; never guess an application ID.')
        repairs = 0
        try:
            value = self._generate(extraction_prompt, extraction, schema, 'mail_extraction_v3')
            # Validate the envelope separately so a bad finding does not erase siblings.
            self._envelope(value, schema)
        except DomainError as exc:
            if exc.code not in ANALYSIS_FAILURE_REASONS or exc.code in {'context_budget_exceeded', 'output_too_large'}:
                raise
            repairs = 1
            issue = _issue(exc, 'extraction')
            try:
                value = self._generate(extraction_prompt + '\nRepair the failed response using this validation error.',
                    {**extraction, 'validation_error': issue}, schema, 'mail_extraction_v3')
                self._envelope(value, schema)
            except DomainError as final:
                if final.code not in ANALYSIS_FAILURE_REASONS:
                    raise
                _fail(final.code if final.code in ANALYSIS_FAILURE_REASONS else 'invalid_output',
                      {'version': 2, 'issues': [_issue(final, 'extraction')], 'repairs': repairs})
        result = _empty(value['relevance'])
        result['uncertainties'] = value['uncertainties']
        valid, failed = {}, {}
        for group in KINDS:
            for position, raw in enumerate(value[group]):
                key = group + ':' + str(position)
                try:
                    valid[key] = (group, self._finding(raw, group, schema['properties'][group]['items'], context))
                except DomainError as exc:
                    failed[key] = (raw, _issue(exc, 'extraction', group, position))
        if failed and repairs == 0:
            repairs = 1
            variants = [_object({'finding_id': {'type': 'string', 'enum': [key]},
                'finding': schema['properties'][issue['category']]['items']}) for key, (_, issue) in failed.items()]
            repair_schema = _object({'repairs': {'type': 'array', 'maxItems': len(failed), 'items': {'anyOf': variants}}})
            try:
                repaired = self._generate(extraction_prompt + '\nRepair ONLY the listed failed findings. '
                    'Do not repeat or change successful findings. Return one replacement for every failed finding.',
                    {**extraction, 'failed_findings': [{'finding_id': key, 'finding': raw, 'error': issue}
                     for key, (raw, issue) in failed.items()]}, repair_schema, 'mail_finding_repair_v3')
                check(repaired, repair_schema)
                counts = {key: sum(r['finding_id'] == key for r in repaired['repairs']) for key in failed}
                for repair in repaired['repairs']:
                    key = repair['finding_id']
                    if counts[key] != 1:
                        continue
                    group = failed[key][1]['category']
                    try:
                        valid[key] = (group, self._finding(repair['finding'], group, schema['properties'][group]['items'], context))
                        del failed[key]
                    except DomainError as exc:
                        failed[key] = (repair['finding'], _issue(exc, 'extraction', group, int(key.split(':')[1])))
            except DomainError as exc:
                if exc.code not in ANALYSIS_FAILURE_REASONS:
                    raise
                # Keep exact original failures and all verified siblings.
        for group, finding in valid.values():
            result[group].append(finding)
        issues = [issue for _, issue in failed.values()]
        quarantine = {}
        for key, (raw, issue) in failed.items():
            group = issue['category']
            try:
                quarantine[key] = (group, self._candidate(raw, group, schema['properties'][group]['items'], context))
            except DomainError:
                # Wire violations or unverifiable evidence cannot be dismissed
                # by a semantic judgment. They remain original failures.
                continue
        if issues and not valid and not quarantine:
            code = 'evidence_mismatch' if all(i['reason'] in {'unknown_block', 'invalid_span', 'quote_not_found', 'quote_not_unique'} for i in issues) else 'invalid_output'
            _fail(code, {'version': 2, 'issues': issues, 'repairs': repairs})
        return result, valid, issues, repairs, quarantine

    @staticmethod
    def _envelope(value, schema):
        if not isinstance(value, dict):
            raise OutputError('wrong_type')
        if set(value) - set(schema['properties']):
            raise OutputError('unknown_field')
        missing = set(schema['properties']) - set(value)
        if missing:
            raise OutputError('missing_field', (sorted(missing)[0],))
        for key in ('relevance', 'uncertainties'):
            check(value[key], schema['properties'][key], (key,))
        for group in KINDS:
            if not isinstance(value[group], list):
                raise OutputError('wrong_type', (group,))
            if len(value[group]) > 20:
                raise OutputError('invalid_length', (group,))
        if value['relevance'] == 'unrelated' and any(value[group] for group in KINDS):
            raise OutputError('invalid_contract')

    def _bind(self, context, payload, prompt, result, valid):
        pending = {key: item for key, item in valid.items() if item[0] != 'temporal_facts'}
        # A message can have one reviewed association. Extraction hypotheses
        # never become separate competing accepted links.
        original_associations = result['associations']
        result['associations'] = original_associations[:1]
        if not pending:
            return [], 0
        authored = [s for s in context.sources if s.role == 'authored' and s.direction in {'incoming', 'browser'}]
        if len(authored) != 1:
            result['uncertainties'] = (result['uncertainties'] + ['One canonical email identity requires exactly one current authored source.'])[:10]
            return [dict(stage='identity', category='analysis', position=0, reason='multiple_authored_sources')], 0
        if not (context.candidates or payload['catalog_candidates']):
            return [], 0
        if any(payload[k].get('complete') is False for k in ('candidate_retrieval', 'catalog_retrieval')):
            result['uncertainties'] = (result['uncertainties'] + ['Application matching is incomplete; findings remain unassigned.'])[:10]
            return [dict(stage='identity', category='analysis', position=0, reason='incomplete_retrieval')], 0
        schema = canonical_identity_schema(context, pending)
        binding_prompt = prompt + ('\nPHASE: IDENTITY. Resolve ONE canonical application identity for the current authored email. '
            'Earlier sources are context, not additional emails to link. Extraction did not choose any targets; resolve identity afresh here. '
            'Preserve the semantic content of verified findings, not any previous target decision. '
            'Select one target_id for the whole email using its full body, role identifiers and all competing applications/catalog roles. '
            'Never assign separate targets to individual findings. Candidate alternatives are not pursuits asserted by the email. '
            'If the CURRENT AUTHORED EMAIL itself explicitly covers multiple pursuits or conflicting employers, '
            'use resolution conflicting and target_id null. If the email does not uniquely distinguish plausible roles, '
            'use resolution ambiguous and target_id null. Only resolution matched can have a nonnull target. '
            'Do not resolve ambiguity by first candidate, majority of findings, role recency or absence of browser evidence. '
            'Supply identity evidence from sources, not candidate metadata. Catalog selections do not assign facts to an application. '
            'records selects existing interviews, assessments or offers for the chosen application only. '
            'Return one record selection for every eligible typed finding, with record_id null when the fact describes a new record. '
            'Submission receipts, requests and associations never select records. Every change still requires human approval. '
            'Return exactly fields target_id, resolution, evidence, records and uncertainties.')
        data = identity_payload(payload, pending)
        selected, issues, repairs = None, {}, 0
        for attempt in range(2):
            try:
                wire = self._generate(binding_prompt + ('\nCorrect the response using validation_errors; preserve source facts.' if attempt else ''),
                    {**data, **({'validation_errors': list(issues.values())} if attempt else {})}, schema, 'mail_identity_v4')
                if not isinstance(wire, dict) or set(wire) != set(schema['properties']):
                    raise OutputError('invalid_contract')
                # Record errors are repaired independently after the canonical
                # identity has passed its own contract.
                for field in ('target_id', 'resolution', 'evidence', 'uncertainties'):
                    check(wire[field], schema['properties'][field], (field,))
                if (wire['resolution'] == 'matched') != (wire['target_id'] is not None):
                    raise OutputError('invalid_contract', ('target_id',))
                spans = resolve_evidence(wire['evidence'], context)
                selected = {**wire, 'evidence': spans}
                issues.clear()
                break
            except DomainError as exc:
                if exc.code not in ANALYSIS_FAILURE_REASONS:
                    raise
                issues['identity'] = _issue(exc, 'identity')
                repairs = 1
        if selected is None:
            return list(issues.values()), repairs
        target = selected['target_id']
        result['uncertainties'] = list(dict.fromkeys(result['uncertainties'] + selected['uncertainties']))[:10]
        if selected['resolution'] == 'conflicting':
            result['uncertainties'] = (result['uncertainties'] + ['The email describes conflicting application identities; select an application manually.'])[:10]
            return [dict(stage='identity', category='analysis', position=0, reason='conflicting_identity')], repairs
        existing = context.context.get('mail_projection', {}).get('target_application_id')
        if existing and target and target != existing:
            result['uncertainties'] = (result['uncertainties'] + ['The email identity conflicts with its reviewed application link.'])[:10]
            return [dict(stage='identity', category='analysis', position=0, reason='existing_association_conflict')], repairs
        if target is None:
            result['uncertainties'] = (result['uncertainties'] + ['The email does not uniquely identify an application.'])[:10]
            return [], repairs
        catalog = target.startswith('catalog:')
        result['associations'] = [dict(kind='job' if catalog else 'application', target_id=target,
                                       confidence=None, evidence=selected['evidence'])]
        if catalog:
            # Catalog roles do not authorize a new application or any writes to
            # application records. The human create/link workflow remains owner.
            return [], repairs
        typed = {key: item for key, item in pending.items() if item[0] == 'facts'
                 and item[1]['kind'] in {'interview', 'assessment', 'offer'} and isinstance(item[1].get('value'), dict)}
        records, record_errors = {}, {}
        rows = selected['records']
        record_schema = canonical_record_schema(context, typed, target, selected=True)
        def validate_records(rows, wanted):
            if not isinstance(rows, list) or len(rows) > 80:
                record_errors['records'] = dict(stage='identity', category='analysis', position=0, reason='invalid_contract')
                for key, (group, _) in wanted.items():
                    record_errors[key] = dict(stage='identity', category=group, position=int(key.split(':')[1]), reason='invalid_contract')
                return
            unknown = any(not isinstance(row, dict) or not isinstance(row.get('finding_id'), str)
                          or row['finding_id'] not in wanted for row in rows)
            if unknown:
                record_errors['records'] = dict(stage='identity', category='analysis', position=0, reason='unexpected_binding')
            else:
                record_errors.pop('records', None)
            for key, (group, finding) in wanted.items():
                choices = [row for row in rows if isinstance(row, dict) and row.get('finding_id') == key]
                try:
                    if len(choices) != 1:
                        raise OutputError('duplicate_binding' if choices else 'missing_binding')
                    check(choices[0], record_schema['items'])
                    self._check_binding({'target_id': target, 'record_id': choices[0]['record_id']}, group, finding, context)
                    records[key] = choices[0]['record_id']
                    record_errors.pop(key, None)
                except DomainError as exc:
                    record_errors[key] = _issue(exc, 'identity', group, int(key.split(':')[1]))
        validate_records(rows, typed)
        if record_errors and not repairs:
            repairs = 1
            wanted = {key: item for key, item in typed.items() if key not in records}
            repair_schema = _object({'records': canonical_record_schema(context, wanted, target, selected=True)})
            try:
                repaired = self._generate(semantic_instructions() + '\nPHASE: RECORD CORRECTION. Canonical identity is fixed. '
                    'Return exactly records, with only the failed typed record selections. Never rewrite identity or successful selections.',
                    {**data, 'canonical_target_id': target, 'validation_errors': list(record_errors.values()),
                     'findings': [f for f in data['findings'] if f['finding_id'] in wanted]},
                    repair_schema, 'mail_record_repair_v1')
                if not isinstance(repaired, dict) or set(repaired) != {'records'}:
                    raise OutputError('invalid_contract')
                validate_records(repaired['records'], wanted)
            except DomainError as exc:
                if exc.code not in ANALYSIS_FAILURE_REASONS:
                    raise
                record_errors['records'] = _issue(exc, 'identity')
        for key, (group, finding) in pending.items():
            if group == 'associations' or key in typed and key not in records:
                continue
            finding['target_id'] = target
            if key in records and records[key]:
                finding['value'][finding['kind'] + '_id'] = records[key]
        return list(record_errors.values()), repairs

    def _support_inventory(self, context, payload, findings):
        topics = []
        if any(group == 'facts' and finding['kind'] == 'contact' for group, finding in findings.values()):
            topics.append('personal_communication')
        if any(group == 'temporal_facts' for group, _ in findings.values()):
            topics.append('temporal_commitment')
        if not topics:
            return {}, []
        schema = support_inventory_schema(context, topics)
        prompt = semantic_instructions() + (
            '\nPHASE: SOURCE SUPPORT INVENTORY. Independently classify the full current authored email. '
            'No extracted claim or candidate application is supplied. Read the source itself. '
            'For personal_communication, substantive_personal requires actual substantive personal communication '
            'with the applicant about the application, such as a recruiter outreach, specific question, interview discussion '
            'or individualized next step. An automated acknowledgment or rejection, team signature, support address, '
            'a personal name in a signature, or promise to contact later does not independently establish this. '
            'Classify those as routine_or_automated. If the distinction cannot be established, use uncertain. '
            'For temporal_commitment, actionable_commitment means a real deadline, interview/assessment/offer time, '
            'or another concrete commitment meaningful to the applicant. A generic employer estimate of when a team '
            'will review or respond is descriptive_response_estimate, not a new deadline or scheduled event. '
            'If both meaningful commitments and routine estimates occur, use actionable_commitment; later verification '
            'will assess the individual time expressions. Use uncertain when support cannot be established. '
            'Earlier thread messages, quoted history and sent text cannot establish new incoming developments. '
            'Cite current authored source block IDs supporting each classification. '
            'Return exactly support, with one topic, classification and evidence entry per requested topic. No prose.')
        data = {k: payload[k] for k in ('sources', 'coverage', 'thread_coverage')}
        data['topics'] = topics
        try:
            value = self._generate(prompt, data, schema, 'mail_source_support_v1')
            check(value, schema)
            rows = value['support']
            if len(rows) != len(topics) or {r['topic'] for r in rows} != set(topics):
                raise OutputError('invalid_contract')
            current = {(s.source_id, s.revision) for s in context.sources
                       if s.role == 'authored' and s.direction in {'incoming', 'browser'}}
            result = {}
            for row in rows:
                spans = resolve_evidence(row['evidence'], context)
                if not any((e['source_id'], e['revision']) in current for e in spans):
                    raise OutputError('invalid_identity')
                result[row['topic']] = {**row, 'evidence': spans}
            return result, []
        except DomainError as exc:
            if exc.code not in ANALYSIS_FAILURE_REASONS:
                raise
            return {}, [_issue(exc, 'verification')]

    def _verify(self, context, payload, prompt, result, valid, quarantine=None):
        """Independently challenge source semantics before any identity binding.

        Successful rejection of an unsupported interpretation is a correction,
        while uncertainty, missing coverage and verifier failure stay partial.
        """
        publishable = set(valid)
        candidates = {**valid, **(quarantine or {})}
        support, inventory_issues = self._support_inventory(context, payload, candidates)
        instruction = prompt + (
            '\nPHASE: VERIFY. Independently read the complete source before evaluating the extracted findings. '
            'Every finding is an untrusted hypothesis. Decide supported, unsupported or uncertain for each finding_id. '
            'Check the full body rather than the subject alone. A receipt subject may have a rejection body. '
            'Generic acknowledgments, automated signatures, promises to review or contact the applicant, '
            'and support-channel instructions do not establish substantive personal recruiter contact. '
            'Keep real independent assessments, requests, interviews and outcomes alongside receipts. '
            'Do not infer new facts from earlier thread sources or outgoing/quoted content. '
            'Mark coverage possibly_missing if any substantive current fact, request, outcome or time expression '
            'is missing or materially misrepresented; otherwise complete. Do not add facts or bind targets. '
            'Grounded hypotheses may have unresolved domain mapping errors; those errors do not establish whether the underlying claim is true. '
            'Deliberately reject unsupported interpretations even if their normalization is also invalid. '
            'independent_support is a separate source-only assessment. Routine or automated acknowledgments and signatures '
            'cannot establish personal contact; descriptive employer response estimates are not actionable time commitments. '
            'Return exactly fields checks and coverage. Each check has exactly finding_id and verdict. '
            'Return exactly one check per supplied finding_id, and no others. No prose.')
        data = {k: payload[k] for k in ('sources', 'coverage', 'thread_coverage')}
        data['findings'] = [{'finding_id': key, 'category': group, 'finding': finding}
                            for key, (group, finding) in candidates.items()]
        data['independent_support'] = list(support.values())
        data['quarantined_findings'] = list(quarantine or {})
        issues, corrections, supported, verdicts = {}, [], {}, {}
        coverage, repairs = None, 0
        for attempt in range(2):
            wanted = {key: item for key, item in candidates.items() if key not in verdicts}
            if attempt and not issues:
                break
            repairs = attempt
            stage_schema = verification_schema(list(wanted) or ['__none__'])
            request = {**data, 'findings': [f for f in data['findings'] if f['finding_id'] in wanted]}
            if attempt:
                request['validation_errors'] = list(issues.values())
                request['previously_verified_findings'] = [{**f, 'verdict': verdicts[f['finding_id']]}
                    for f in data['findings'] if f['finding_id'] in verdicts]
            try:
                value = self._generate(instruction + ('\nRepair ONLY failed checks and response-contract errors. Do not repeat or change successful checks. Evaluate coverage using findings AND previously_verified_findings together.' if attempt else ''),
                    request, stage_schema, 'mail_semantic_verification_repair_v2' if attempt else 'mail_semantic_verification_v2')
                if (not isinstance(value, dict) or set(value) != {'checks', 'coverage'}
                        or not isinstance(value['checks'], list) or len(value['checks']) > 80):
                    raise OutputError('unknown_field' if isinstance(value, dict) and set(value) - {'checks', 'coverage'} else 'invalid_contract')
                check(value['coverage'], stage_schema['properties']['coverage'], ('coverage',))
                coverage = value['coverage']
                issues.pop('analysis', None)
                rows = value['checks']
                if any(not isinstance(row, dict) or not isinstance(row.get('finding_id'), str)
                       or row['finding_id'] not in wanted for row in rows):
                    issues['analysis'] = dict(stage='verification', category='analysis', position=0, reason='unexpected_binding')
                for key, (group, _) in wanted.items():
                    choices = [row for row in rows if isinstance(row, dict) and row.get('finding_id') == key]
                    try:
                        if len(choices) != 1:
                            raise OutputError('duplicate_binding' if choices else 'missing_binding')
                        check(choices[0], stage_schema['properties']['checks']['items'])
                        verdicts[key] = choices[0]['verdict']
                        issues.pop(key, None)
                    except DomainError as exc:
                        issues[key] = _issue(exc, 'verification', group, int(key.split(':')[1]))
            except DomainError as exc:
                if exc.code not in ANALYSIS_FAILURE_REASONS:
                    raise
                issues['analysis'] = _issue(exc, 'verification')
                # Preserve every independently verified sibling on correction.
        if coverage == 'possibly_missing':
            issues['coverage'] = dict(stage='verification', category='analysis', position=0, reason='possibly_missing_findings')
        for n, issue in enumerate(inventory_issues):
            issues['inventory:' + str(n)] = issue
        for key, verdict in verdicts.items():
            group, finding = candidates[key]
            position = int(key.split(':')[1])
            topic = ('personal_communication' if group == 'facts' and finding['kind'] == 'contact'
                     else 'temporal_commitment' if group == 'temporal_facts' else None)
            if topic:
                classification = support.get(topic, {}).get('classification', 'uncertain')
                if classification in {'routine_or_automated', 'descriptive_response_estimate'}:
                    verdict = 'unsupported'
                elif classification == 'uncertain':
                    verdict = 'uncertain'
                elif key not in publishable and verdict == 'unsupported':
                    # A source-only positive support assessment can contradict
                    # rejection of a malformed normalization. Keep that mapping
                    # problem visible instead of discarding a real commitment.
                    independent = support.get(topic, {}).get('evidence', [])
                    if any(a['source_id'] == b['source_id'] and a['revision'] == b['revision']
                           and max(a['start'], b['start']) < min(a['end'], b['end'])
                           for a in finding['evidence'] for b in independent):
                        verdict = 'uncertain'
            if group in {'facts', 'requests'}:
                current = {(s.source_id, s.revision) for s in context.sources
                           if s.role == 'authored' and s.direction in {'incoming', 'browser'}}
                if not any((e['source_id'], e['revision']) in current for e in finding['evidence']):
                    verdict = 'unsupported'
            if verdict == 'unsupported':
                result[group] = [f for f in result[group] if f is not finding]
                corrections.append(dict(stage='verification', category=group, position=position, reason='unsupported_finding_removed'))
            elif verdict == 'uncertain':
                issues[key] = dict(stage='verification', category=group, position=position, reason='uncertain_semantics')
            elif key in publishable:
                supported[key] = (group, finding)
        return supported, list(issues.values()), corrections, repairs

    @staticmethod
    def _check_binding(row, group, finding, context):
        target, record = row['target_id'], row['record_id']
        if target and target.startswith('catalog:') and group != 'associations':
            raise OutputError('catalog_target')
        if record:
            if group != 'facts' or finding['kind'] not in {'interview', 'assessment', 'offer'} or not isinstance(finding['value'], dict):
                raise OutputError('record_target_mismatch')
            records = context.context.get('records', {}).get(target, {}).get(finding['kind'] + 's', [])
            if record not in {r['id'] for r in records}:
                raise OutputError('record_target_mismatch')

    def analyze(self, context):
        payload, prompt = model_payload(context), semantic_instructions()
        result, valid, issues, repairs, quarantine = self._extract(context, payload, instructions())
        corrections = []
        if result['relevance'] != 'unrelated':
            valid, semantic_issues, corrections, semantic_repairs = self._verify(context, payload, prompt, result, valid, quarantine)
            corrected = {(c['category'], c['position']) for c in corrections}
            issues = [issue for issue in issues if (issue['category'], issue['position']) not in corrected]
            issues.extend(semantic_issues)
            repairs += semantic_repairs
            identity_issues, identity_repairs = self._bind(context, payload, identity_instructions(), result, valid)
            issues.extend(identity_issues)
            repairs += identity_repairs
        validate_analysis(result, context)
        return AnalysisResult(result, issues[:80], repairs, corrections[:80])
