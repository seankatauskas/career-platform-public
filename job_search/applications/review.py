"""Source-grouped read models and explicit, resumable review reconciliation.

Uses public owner operations. A group is presentation, never blanket approval.
"""
from copy import deepcopy

from job_search.commands import DomainError, digest
from .understanding.reconciliation import non_actionable_contact, verification, receipt_fact, non_actionable_request, only_nonactionable


def collect(read):
    items, after = [], None
    while True:
        page = read(after)
        items.extend(page["items"])
        if len(items) > 10000:
            raise DomainError("invalid_input", "Review exceeds the supported inventory size")
        after = page.get("next_cursor")
        if not after:
            return items


def identity(proposal):
    evidence = proposal.get("evidence", [])
    mail = sorted({e["source_id"] for e in evidence if e.get("owner") == "correspondence"})
    if mail:
        return "email:" + mail[0]
    attempts = sorted({(e.get("device_id", ""), e.get("attempt_ref", "")) for e in evidence if e.get("attempt_ref")})
    if len(attempts) == 1:
        return "browser:" + digest([proposal.get("application_id"), *attempts[0]])
    return "proposal:" + proposal["id"]


class ReviewService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.apps, self.mail, self.understanding = runtime.applications, runtime.correspondence, runtime.understanding

    def _inventory(self, con):
        groups = {}
        proposals = collect(lambda after: self.understanding.list_pending(con, limit=100, after=after))
        for p in proposals:
            # Sources imported before explicit owner labels still use their preserved identity.
            p = deepcopy(p)
            for e in p["evidence"]:
                if e.get("owner") is None:
                    try:
                        source = self.mail.get(con, e["source_id"], e["revision"])
                        e.update(owner="correspondence", sha256=source["source_sha256"])
                    except DomainError:
                        pass
            key = identity(p)
            if p.get("analysis_id"):
                analysis = self.understanding.get_analysis(con, p["analysis_id"])
                primary = analysis["descriptor"].get("context", {}).get("mail_projection", {}).get("message_id")
                if primary:
                    key = "email:" + primary
            if key.startswith("browser:"):
                try:
                    first = self.apps.get_record(con, "observations", p["evidence"][0]["source_id"])
                    key = "browser:" + digest([p.get("application_id"), first["pursuit_no"], first["device_id"], first["attempt_ref"]])
                except DomainError as exc:
                    if exc.code != "not_found":
                        raise
                    key = "proposal:" + p["id"]
                    p["blockers"] = list(dict.fromkeys(p["blockers"] + ["missing_browser_source"]))
            group = groups.setdefault(key, {"id": key, "kind": "evidence_group", "proposals": [], "processing": [], "evidence": []})
            group["proposals"].append(p)
        issues = collect(lambda after: self.understanding.processing_issues(con, limit=100, after=after))
        for issue in issues:
            source = self.mail.get(con, issue["source_id"])
            if source["revision"] != issue["revision"]:
                continue
            key = "email:" + issue["source_id"]
            group = groups.setdefault(key, {"id": key, "kind": "evidence_group", "proposals": [], "processing": [], "evidence": []})
            group["processing"].append(issue)
        for group in groups.values():
            apps = {}
            refs = {}
            for p in group["proposals"]:
                if p.get("application_id"):
                    app = self.apps.get_application(con, p["application_id"])
                    apps[app["id"]] = self.runtime.queries._application(con, app)
                for e in p["evidence"]:
                    refs[(e.get("owner"), e["source_id"], str(e["revision"]))] = e
            for issue in group["processing"]:
                for e in issue["sources"]:
                    refs[(e.get("owner"), e["source_id"], str(e["revision"]))] = e
            group["evidence"] = list(refs.values())
            group["applications"] = list(apps.values())
            group["application"] = next(iter(apps.values())) if len(apps) == 1 else None
            group["source_at"] = max([p["recorded_at"] for p in group["proposals"]] + [i["recorded_at"] for i in group["processing"]])
            group["source_type"] = group["id"].split(":", 1)[0]
            if group["source_type"] == "email":
                source = self.mail.get(con, group["id"][6:])
                group["source"] = {k: source.get(k) for k in ("id", "revision", "source_sha256", "occurred_at", "version")}
                group["source_at"] = source.get("occurred_at") or group["source_at"]
                group["association"] = self.mail.association(con, source["id"])
                if group["association"]:
                    linked = self.apps.get_application(con, group["association"]["application_id"])
                    apps[linked["id"]] = self.runtime.queries._application(con, linked)
                    group["applications"] = list(apps.values())
                    group["application"] = next(iter(apps.values())) if len(apps) == 1 else None
                    for issue in group["processing"]:
                        issue["application_id"] = linked["id"]
                for p in group["proposals"]:
                    if any(e.get("source_id") == source["id"] and e.get("revision") != source["revision"] for e in p["evidence"]):
                        p["blockers"] = list(dict.fromkeys(p["blockers"] + ["source_revision_changed"]))
            for p in group["proposals"]:
                p["before"] = {}
                if p.get("application_id"):
                    p["before"]["application"] = self.apps.stage(con, p["application_id"])
                for field, kind in (("submission_id", "submissions"), ("task_id", "tasks"), ("interview_id", "interviews")):
                    if p["input"].get(field):
                        try:
                            record = self.apps.get_record(con, kind, p["input"][field])
                            p["before"][kind] = record
                            if record["application_id"] != p.get("application_id"):
                                raise DomainError("invalid_input", "Wrong record target")
                        except DomainError:
                            p["blockers"] = list(dict.fromkeys(p["blockers"] + ["requires_target_record_resolution"]))
                p["prerequisites"] = [self.understanding.get(con, identifier) for identifier in p["dependencies"]]
                if p["operation"] == "close_application" and p.get("application_id"):
                    p["closure"] = self.apps.preview_closure(con, p["application_id"])
            group["preview_digest"] = digest(group)
        return list(groups.values())

    def page(self, *, limit=25, cursor=None, application_id=None, group_id=None, action_cursor=None):
        from .queries import _cursor, _page, _limit
        _limit(limit)
        query = {"operation": "review_groups", "application_id": application_id, "group_id": group_id}
        after = _cursor(cursor, query)
        action_query = {"operation": "review_group_actions", "application_id": application_id}
        action_after = _cursor(action_cursor, action_query)
        with self.runtime.executor.read() as con:
            groups = self._inventory(con)
            if application_id:
                groups = [g for g in groups if any(a["id"] == application_id for a in g["applications"]) or (g.get("association") or {}).get("application_id") == application_id]
            actions = collect(lambda after: self.runtime.actions.list_review_actions(con, limit=100, after=after or ""))
            if application_id:
                actions = [a for a in actions if a["application_id"] == application_id]
            totals = {"decisions": sum(bool(g["proposals"]) for g in groups) + len(actions),
                      "operations": sum(len(g["proposals"]) for g in groups),
                      "processing": sum(bool(g["processing"]) for g in groups), "cards": len(groups) + len(actions)}
            def priority(g):
                due = [p["input"].get("due_at") for p in g["proposals"] if p["input"].get("due_at")]
                return (0, min(due)) if due else (1, "") if any(p["operation"] == "close_application" for p in g["proposals"]) else (2, "")
            groups.sort(key=lambda g: (g["source_at"], g["id"]), reverse=True)
            groups.sort(key=priority)
            if group_id:
                groups = [g for g in groups if g["id"] == group_id or any(p["id"] == group_id for p in g["proposals"])]
            if after:
                positions = [i for i, g in enumerate(groups) if g["id"] == after]
                if not positions:
                    raise DomainError("version_conflict", "Review changed; refresh the first page")
                groups = groups[positions[0] + 1:]
            result = _page({"items": groups[:limit], "next_cursor": groups[limit-1]["id"] if len(groups) > limit else None}, query)
            if action_after:
                actions = [a for a in actions if a["action_id"] > action_after]
            action_page = _page({"items": actions[:limit], "next_cursor": actions[limit-1]["action_id"] if len(actions)>limit else None}, action_query)
            result.update(totals=totals, actions=action_page["items"], next_action_cursor=action_page["next_cursor"], policy=self.apps.recording_policy(con))
            return result

    def _checked(self, con, group_id, expected_digest):
        group = next((g for g in self._inventory(con) if g["id"] == group_id), None)
        if group is None or group["preview_digest"] != expected_digest:
            raise DomainError("version_conflict", "This evidence group changed; refresh its preview")
        return group

    def correct_review_target(self, tx, *, group_id, expected_digest, application_id, reason, receipt_scope="preserve"):
        if tx.context.principal.kind != "human" or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise DomainError("not_authorized", "An attributed human correction is required")
        group = self._checked(tx.connection, group_id, expected_digest)
        if group["source_type"] != "email" or receipt_scope not in {"preserve", "application"}:
            raise DomainError("invalid_input", "Select an email and a supported receipt scope")
        if any("source_revision_changed" in p["blockers"] for p in group["proposals"]):
            raise DomainError("version_conflict", "Reanalyze the current source before correcting its target")
        app = self.apps.get_application(tx.connection, application_id)
        association = group.get("association")
        if association and association["application_id"] != app["id"]:
            raise DomainError("dependency_unresolved", "Use association correction to review previously accepted consequences")
        source = self.mail.get(tx.connection, group_id[6:])
        ref = {"owner": "correspondence", "source_id": source["id"], "revision": source["revision"], "sha256": source["source_sha256"]}
        versions = {"application:" + app["id"]: app["version"], "message:" + source["id"]: source["version"]}
        link = None
        if not association:
            link = self.understanding.propose(tx, operation="link_message", application_id=app["id"],
                input={"message_id": source["id"], "application_id": app["id"], "expected_version": 0}, evidence=[ref],
                expected_versions=versions, lineage_key=digest({"review_target": expected_digest, "application": app["id"]}))
        replacements = []
        mapping = {}
        for p in group["proposals"]:
            if p["status"] != "pending":
                raise DomainError("version_conflict", "Refresh stale proposals before correcting their target")
            self.understanding.reconcile(tx, proposal_id=p["id"], expected_version=p["version"], reason=reason)
            if p["operation"] == "link_message":
                mapping[p["id"]] = link["id"] if link else None
                continue
            value = {**p["input"], "application_id": app["id"]}
            blockers = [b for b in p["blockers"] if b not in {"needs_target", "unresolved_association", "target_disagrees_with_selected_association"}]
            operation = p["operation"]
            if receipt_scope == "application" and operation in {"confirm_submission", "record_submission", "record_submission_receipt"}:
                operation = "record_submission_receipt"
                value = {"application_id": app["id"], "evidence": [ref], "occurred_at": source.get("occurred_at")}
                blockers = [b for b in blockers if b != "requires_target_record_resolution"]
            else:
                for field, kind, prefix in (("submission_id", "submissions", "submission"), ("task_id", "tasks", "task"),
                        ("interview_id", "interviews", "interview"), ("assessment_id", "assessments", "assessment"), ("offer_id", "offers", "offer")):
                    if not value.get(field):
                        continue
                    try:
                        record = self.apps.get_record(tx.connection, kind, value[field])
                    except DomainError:
                        record = {"application_id": None}
                    if record["application_id"] != app["id"] or record["pursuit_no"] != app["pursuit_no"]:
                        blockers.append("requires_target_record_resolution")
                    else:
                        value["expected_version"] = record["version"]
                        versions[prefix + ":" + record["id"]] = record["version"]
            if operation == "close_application":
                preview = self.apps.preview_closure(tx.connection, app["id"])
                value.update(expected_version=preview["expected_version"], expected_records=preview["expected_records"])
                blockers = [b for b in blockers if b != "requires_closure_preview"]
            replacement = self.understanding.propose(tx, operation=operation, input=value, application_id=app["id"],
                evidence=p["evidence"], expected_versions=versions, dependencies=[link["id"]] if link else [], blockers=list(dict.fromkeys(blockers)),
                analysis_id=p["analysis_id"], finding_id=p["finding_id"], lineage_key=digest({"replaces": p["id"], "group": expected_digest, "input": value}))
            mapping[p["id"]] = replacement["id"]
            replacements.append(replacement)
        # Preserve dependencies unrelated to association, rebinding replaced siblings.
        for old in group["proposals"]:
            replacement_id = mapping.get(old["id"])
            if old["operation"] == "link_message" or not replacement_id:
                continue
            dependencies = [mapping.get(d, d) for d in old["dependencies"]]
            dependencies = list(dict.fromkeys([d for d in dependencies if d] + ([link["id"]] if link else [])))
            self.understanding.set_dependencies(tx, replacement_id, dependencies)
        return {"status": "replaced", "proposals": [self.understanding.get(tx.connection, p["id"]) for p in replacements], "association_proposal": link}

    def migration_preview(self, *, enable_browser=False):
        with self.runtime.executor.read() as con:
            groups = self._inventory(con)
            return {"version": 1, "installation": self.runtime.executor.installation_identity(con),
                    "policy": self.apps.recording_policy(con), "enable_browser": enable_browser, "groups": [self._migration_entry(con, g, enable_browser=enable_browser) for g in groups]}

    def refresh_preview(self):
        """Explicit bounded replay of old or blocked email decisions, never approval."""
        from .understanding.analyzer import SharedAnalyzer
        with self.runtime.executor.read() as con:
            groups = []
            for group in self._inventory(con):
                if group["source_type"] != "email":
                    continue
                source = group["source"]
                issue = self.understanding.processing_for_source(con, source["id"], source["revision"])
                if not issue or issue["status"] not in {"open", "succeeded"}:
                    continue
                analysis = self.understanding.get_analysis(con, issue["analysis_id"])
                if analysis["prompt_version"] == SharedAnalyzer.prompt_version:
                    continue
                groups.append({"group_id": group["id"], "expected_digest": group["preview_digest"],
                    "analysis_id": issue["analysis_id"], "prompt_version": analysis["prompt_version"],
                    "proposal_count": len(group["proposals"]), "processing_status": issue["status"]})
            return {"version": 1, "installation": self.runtime.executor.installation_identity(con),
                    "operation": "refresh_review_group", "prompt_version": SharedAnalyzer.prompt_version, "groups": groups}

    def refresh_review_group(self, tx, *, group_id, expected_digest):
        if tx.context.principal.kind != "human" or tx.context.origin == "inferred":
            raise DomainError("not_authorized", "Review refresh requires owner authority")
        group = self._checked(tx.connection, group_id, expected_digest)
        if group["source_type"] != "email":
            raise DomainError("invalid_input", "Only email analysis can be refreshed")
        source = group["source"]
        issue = self.understanding.processing_for_source(tx.connection, source["id"], source["revision"])
        if not issue:
            raise DomainError("dependency_unresolved", "Source has no recorded analysis")
        from .understanding.processing import decide
        result = decide(tx, issue_id=issue["issue_id"], expected_version=issue["version"],
            expected_analysis_id=issue["analysis_id"], reason="Refresh interpretation with current context; preserve review decisions",
            retry=True, refresh=True, replaces=[{"id": p["id"], "version": p["version"]} for p in group["proposals"]])
        return {"status": "queued", "processing": result, "preserved_proposals": len(group["proposals"])}

    def _migration_entry(self, con, group, *, enable_browser=False):
        policy = self.apps.recording_policy(con)
        planned = []
        for p in group["proposals"]:
            disposition = "retained_for_review"
            if group["source_type"] == "browser":
                from .recording_policy import eligibility
                disposition = "record_browser" if (policy["enabled"] or enable_browser) and not eligibility(self.apps, con, p) else "retained_browser_exception"
            elif "source_revision_changed" not in p["blockers"] and p["finding_id"] and p["analysis_id"]:
                analysis = self.understanding.get_analysis(con, p["analysis_id"])
                finding = next((f for f in analysis["findings"] if f["id"] == p["finding_id"]), None)
                if p["operation"] == "link_message" and only_nonactionable(analysis["findings"]):
                    disposition = "supersede_nonactionable"
                elif finding and (non_actionable_contact(finding["content"]) or verification(finding["content"]) or non_actionable_request(finding["content"])):
                    disposition = "supersede_nonactionable"
                elif self.runtime.workflows._already_recorded(con, p):
                    disposition = "supersede_restatement"
                elif finding and receipt_fact(finding["content"]) and not p["input"].get("submission_id") and p["operation"] != "record_submission_receipt":
                    disposition = "replace_receipt"
            planned.append({"proposal_id": p["id"], "version": p["version"], "operation": p["operation"], "disposition": disposition,
                "application_id": p["application_id"], "input": p["input"], "blockers": p["blockers"], "dependencies": p["dependencies"]})
        return {"group_id": group["id"], "source_type": group["source_type"], "evidence": group["evidence"], "expected_digest": group["preview_digest"], "expected_policy_version": policy["version"],
                "decisions": planned, "processing": [{"issue_id": i["issue_id"], "version": i["version"], "disposition": "queue_bounded_recovery" if i["status"] == "open" else "recovery_already_queued"} for i in group["processing"]]}

    def migrate_review_group(self, tx, *, group_id, expected_digest, expected_policy_version):
        if tx.context.principal.kind != "human":
            raise DomainError("not_authorized", "Backlog migration requires owner authority")
        group = self._checked(tx.connection, group_id, expected_digest)
        if self.apps.recording_policy(tx.connection)["version"] != expected_policy_version:
            raise DomainError("version_conflict", "Recording policy changed since migration preview")
        plan = self._migration_entry(tx.connection, group)
        by_id = {p["id"]: p for p in group["proposals"]}
        replacements = {}
        for step in plan["decisions"]:
            p = by_id[step["proposal_id"]]
            disposition = step["disposition"]
            if disposition == "record_browser":
                observation = self.apps.get_record(tx.connection, "observations", p["evidence"][0]["source_id"])
                self.runtime.workflows.process_browser(tx, observation)
                step["result_status"] = self.understanding.get(tx.connection, p["id"])["status"]
            elif disposition.startswith("supersede_"):
                self.understanding.reconcile(tx, proposal_id=p["id"], expected_version=p["version"], reason=disposition)
            elif disposition == "replace_receipt":
                self.understanding.reconcile(tx, proposal_id=p["id"], expected_version=p["version"], reason="Receipt corroborates an application, not another browser attempt")
                replacement = self.understanding.propose(tx, operation="record_submission_receipt", application_id=p["application_id"],
                    input={**{k: v for k, v in p["input"].items() if k in {"application_id", "occurred_at"}}, "evidence": p["input"].get("evidence") or p["evidence"]},
                    evidence=p["evidence"], expected_versions=p["expected_versions"], dependencies=p["dependencies"], blockers=p["blockers"],
                    analysis_id=p["analysis_id"], finding_id=p["finding_id"], lineage_key=digest({"receipt_replaces": p["id"], "version": p["version"]}))
                step["replacement_id"] = replacement["id"]
                replacements[p["id"]] = replacement["id"]
        for original in group["proposals"]:
            current = self.understanding.get(tx.connection, replacements.get(original["id"], original["id"]))
            if current["status"] == "pending" and any(d in replacements for d in current["dependencies"]):
                self.understanding.set_dependencies(tx, current["id"], [replacements.get(d, d) for d in current["dependencies"]])
        for issue in group["processing"]:
            if issue["status"] == "open":
                result = self.understanding.retry_automatically(tx, issue_id=issue["issue_id"], expected_version=issue["version"], expected_analysis_id=issue["analysis_id"])
                next(s for s in plan["processing"] if s["issue_id"] == issue["issue_id"])["result_status"] = result["status"]
        return {"status": "reconciled", **plan}
