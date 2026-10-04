"""Offline round/revision/reconciliation regressions with isolated databases."""
from tempfile import TemporaryDirectory
from datetime import datetime, timezone, timedelta

from job_search.contracts import MutationContext
from job_search.db import connect
from job_search.lifecycle.service import LifecycleService
from job_search.outlook.calendar import GraphCalendarClient
from tests.test_job_search_ledger import make_service, start, context
from tests.test_job_search_outlook import session_with, response


def future(days=10, hour=15):
    return (
        (datetime.now(timezone.utc) + timedelta(days=days))
        .replace(hour=hour, minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def setup(directory):
    path, ledger = make_service(directory)
    app = start(ledger)["application"]["application_id"]
    return path, ledger, LifecycleService(ledger), app


def details(**kw):
    return {
        "status": "confirmed",
        "starts_at": future(),
        "ends_at": future(hour=16),
        "time_zone": "UTC",
        **kw,
    }


def accepted(service, app, key="first", **kw):
    result = service.propose_interview_revision(
        app, details(**kw), context("propose-" + key)
    )
    return service.decide_interview_revision(
        result["revision"]["revision_id"],
        "accepted",
        "Reviewed",
        context("accept-" + key),
    )


def test_revisions_replace_reminders_atomically_and_do_not_self_conflict():
    with TemporaryDirectory() as directory:
        path, _, service, app = setup(directory)
        first = accepted(service, app)
        assert first["applied"] and first["availability"]["status"] == "not_checked"
        current = first["round"]["round_id"]
        revised = accepted(
            service,
            app,
            "reschedule",
            round_id=current,
            status="rescheduled",
            starts_at=future(hour=15),
            ends_at=future(hour=17),
        )
        assert revised["applied"]
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='pending'"
                ).fetchone()[0]
                == 2
            )
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='dismissed'"
                ).fetchone()[0]
                == 2
            )
        cancelled = accepted(
            service, app, "cancel", round_id=current, status="cancelled"
        )
        assert cancelled["applied"]
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='pending'"
                ).fetchone()[0]
                == 0
            )
        assert len(service.list_interview_rounds()["rounds"]) == 1
        assert (
            service.list_interview_rounds(statuses=["confirmed", "rescheduled"])[
                "rounds"
            ]
            == []
        )


def test_other_interview_overlap_stays_reviewable_and_retry_succeeds():
    with TemporaryDirectory() as directory:
        _, ledger, service, app = setup(directory)
        first = accepted(service, app)
        app2 = start(ledger, "second-job", "start-second")["application"][
            "application_id"
        ]
        conflict = accepted(service, app2, "conflicting")
        assert not conflict["applied"] and conflict["revision"]["status"] == "conflict"
        accepted(
            service,
            app,
            "cancel-first",
            round_id=first["round"]["round_id"],
            status="cancelled",
        )
        retry = service.decide_interview_revision(
            conflict["revision"]["revision_id"],
            "accepted",
            "Conflict removed",
            context("retry"),
        )
        assert retry["applied"]


def event(version="v2", modified="2026-10-02T00:00:00Z", **kw):
    return {
        "remote_id": "event-1",
        "ical_uid": "uid-1",
        "change_key": version,
        "modified_at": modified,
        "starts_at": future(days=12),
        "ends_at": future(days=12, hour=16),
        "organizer": "recruiter@example.test",
        "is_organizer": False,
        "is_cancelled": False,
        **kw,
    }


def linked(service, app):
    return accepted(
        service,
        app,
        calendar_account_id="account",
        calendar_event_id="event-1",
        calendar_uid="uid-1",
        calendar_modified_at="2026-10-01T00:00:00Z",
        calendar_change_key="v1",
        organizer="recruiter@example.test",
    )


def reconcile(service, *events):
    return service.reconcile_calendar_events(
        "account", events, MutationContext("calendar", "system", "outlook_calendar")
    )


def test_calendar_update_cancel_replay_and_arrival_order():
    with TemporaryDirectory() as directory:
        path, _, service, app = setup(directory)
        linked(service, app)
        changed = reconcile(service, event())["results"][0]
        assert changed["applied"] and changed["round"]["status"] == "rescheduled"
        cancelled = reconcile(
            service, event("v3", "2026-10-03T00:00:00Z", is_cancelled=True)
        )["results"][0]
        assert cancelled["applied"] and cancelled["round"]["status"] == "cancelled"
        # Replay and older versions never resurrect cancelled rounds.
        assert (
            reconcile(service, event("v3", "2026-10-03T00:00:00Z", is_cancelled=True))[
                "results"
            ][0]["status"]
            == "unchanged"
        )
        old = reconcile(service, event("older", "2026-09-30T00:00:00Z"))["results"][0]
        assert not old["applied"]
        assert service.list_interview_rounds()["rounds"][0]["status"] == "cancelled"
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='pending'"
                ).fetchone()[0]
                == 0
            )


def test_equal_time_divergence_private_hold_and_organizer_changes_require_review():
    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        linked(service, app)
        same_time = reconcile(service, event("different", "2026-10-01T00:00:00Z"))[
            "results"
        ][0]
        assert same_time["revision"]["status"] == "conflict"
        hold = reconcile(service, event("hold", is_organizer=True))["results"][0]
        assert not hold["applied"]
        organizer = reconcile(
            service, event("new-organizer", organizer="unknown@example.test")
        )["results"][0]
        assert not organizer["applied"]
        assert (
            service.list_interview_rounds()["rounds"][0]["calendar_change_key"] == "v1"
        )


def test_past_end_is_not_completion_and_upcoming_filters_precede_limit():
    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        accepted(
            service,
            app,
            "past",
            starts_at="2020-01-01T15:00:00Z",
            ends_at="2020-01-01T16:00:00Z",
        )
        next_round = accepted(service, app, "future")
        page = service.list_interview_rounds(
            starts_after="2026-10-01T00:00:00Z", statuses=["confirmed"], limit=1
        )
        assert page["rounds"][0]["round_id"] == next_round["round"]["round_id"]
        assert (
            service.list_interview_rounds(starts_before="2021-01-01T00:00:00Z")[
                "rounds"
            ][0]["status"]
            == "confirmed"
        )
        assert len(service.list_interview_rounds(limit=1)["rounds"]) == 1
        assert service.list_interview_rounds(limit=1)["next_offset"] == 1


def test_live_calendar_conflicts_are_reviewable_and_external_identity_excluded():
    from job_search.contracts import CalendarBlock

    class Calendar:
        def read_calendar_view(self, *args):
            return [
                CalendarBlock(
                    "external",
                    future(),
                    future(hour=16),
                    "busy",
                    False,
                    False,
                    "version",
                )
            ]

    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        service.calendar = Calendar()
        conflict = accepted(service, app)
        assert (
            not conflict["applied"] and conflict["availability"]["status"] == "checked"
        )
        assert conflict["availability"]["conflicts"] == ["external"]
        same = accepted(service, app, "same-event", calendar_event_id="external")
        assert same["applied"]


def test_graph_interview_reader_uses_modified_time_without_select_and_strips_body():
    raw = {
        "id": "calendar-1",
        "iCalUId": "uid",
        "changeKey": "version",
        "lastModifiedDateTime": "2026-10-01T00:00:00Z",
        "start": {"dateTime": future(), "timeZone": "UTC"},
        "end": {"dateTime": future(hour=16), "timeZone": "UTC"},
        "organizer": {"emailAddress": {"address": "recruiter@example.test"}},
        "isOrganizer": False,
        "body": {"content": "private body"},
        "subject": "private subject",
    }
    session, _, http = session_with(response(200, {"value": [raw]}), response(200, raw))
    reader = GraphCalendarClient(session)
    events = reader.read_interview_events(future(days=1), future(days=14))
    assert len(events) == 1 and events[0]["modified_at"] == "2026-10-01T00:00:00Z"
    assert "body" not in events[0] and "subject" not in events[0]
    assert (
        "%24select" not in http.requests[0][1] and "$select" not in http.requests[0][1]
    )
    assert "ImmutableId" in http.requests[0][2]["Prefer"]
    assert reader.read_interview_event("calendar-1")["remote_id"] == "calendar-1"
    assert http.requests[1][0] == "GET"


def test_worker_follows_linked_event_identity_outside_old_window():
    from job_search.lifecycle.calendar_worker import InterviewCalendarTaskHandler
    from job_search.worker import TaskContext

    class Calendar:
        def read_interview_event(self, remote_id):
            assert remote_id == "event-1"
            return event(is_cancelled=True)

    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        linked(service, app)
        worker = InterviewCalendarTaskHandler(
            service, Calendar(), account_id="account", limit=1
        )
        result = worker(
            {},
            TaskContext(
                "work", "outlook.calendar.sync", 1, "2026-10-01T00:00:00Z", lambda: True
            ),
        )
        assert result.result["processed"] == 1
        assert service.list_interview_rounds()["rounds"][0]["status"] == "cancelled"


def test_legacy_import_preserves_existing_reminders_until_revision():
    from tests.test_job_search_secure_mail import (
        make_archive,
        start_application,
        FixedTemporalExtractor,
    )
    from job_search.mail.temporal import TemporalSource, TemporalProposalEngine
    from job_search.mail.context import CandidateApplication

    with TemporaryDirectory() as directory:
        ledger, archive, _ = make_archive(directory)
        app = start_application(ledger)
        text = (
            "BEGIN UNTRUSTED EMAIL\nSeptember 3 at 10 AM Central\nEND UNTRUSTED EMAIL"
        )
        archived = archive.archive_message(
            account_id="account",
            immutable_message_id="message",
            sanitized_text=text,
            truncated=False,
            context=context("archive"),
        )
        source = TemporalSource(
            archived["archive"]["archive_id"], text, "2026-09-01T00:00:00Z"
        )
        engine = TemporalProposalEngine(
            ledger, FixedTemporalExtractor("September 3 at 10 AM Central"), "test-v1"
        )
        proposed = engine.propose(
            source,
            [
                CandidateApplication(
                    app, "greenhouse", "job-1", "Example Labs", "Engineer"
                )
            ],
        )[0]["proposal"]
        result = ledger.decide_temporal_proposal(
            proposed["temporal_proposal_id"], "accepted", "Reviewed", context("legacy")
        )
        service = LifecycleService(ledger)
        imported = service.import_accepted_schedule(
            result["schedule"]["interview_schedule_id"], context("import")
        )
        assert imported["created"] and not imported["round"]["details"].get(
            "employer_confirmed"
        )
        assert not service.import_accepted_schedule(
            result["schedule"]["interview_schedule_id"], context("reimport")
        )["created"]
        revised = accepted(
            service,
            app,
            "import-reschedule",
            round_id=imported["round"]["round_id"],
            status="rescheduled",
        )
        assert revised["applied"]
        with connect(ledger.store.db_path) as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM local_reminders WHERE status='pending'"
                ).fetchone()[0]
                == 0
            )
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='pending'"
                ).fetchone()[0]
                == 2
            )


def test_sparse_cancellation_preserves_metadata_and_old_manual_revision_is_stale():
    with TemporaryDirectory() as directory:
        path, _, service, app = setup(directory)
        first = accepted(
            service,
            app,
            round_kind="Technical panel",
            time_zone="America/Chicago",
            participants=["panel@example.test"],
        )
        round_id = first["round"]["round_id"]
        old = service.propose_interview_revision(
            app,
            {
                "round_id": round_id,
                "status": "rescheduled",
                "starts_at": future(days=11),
                "ends_at": future(days=11, hour=16),
            },
            context("old-revision"),
        )
        cancel = service.propose_interview_revision(
            app, {"round_id": round_id, "status": "cancelled"}, context("sparse-cancel")
        )
        snapshot = cancel["revision"]["details"]
        assert (
            snapshot["participants"] == ["panel@example.test"]
            and snapshot["round_kind"] == "Technical panel"
        )
        assert (
            snapshot["time_zone"] == "America/Chicago"
            and snapshot["starts_at"] == future()
        )
        result = service.decide_interview_revision(
            cancel["revision"]["revision_id"],
            "accepted",
            "Cancel confirmed",
            context("cancel-accepted"),
        )
        assert result["applied"]
        stale = service.decide_interview_revision(
            old["revision"]["revision_id"],
            "accepted",
            "Old review",
            context("old-accepted"),
        )
        assert not stale["applied"] and stale["revision"]["status"] == "stale"
        assert service.list_interview_rounds()["rounds"][0]["status"] == "cancelled"
        import sqlite3

        with connect(path) as con:
            try:
                con.execute(
                    "UPDATE interview_revisions SET details_json='{}' WHERE revision_id=?",
                    (old["revision"]["revision_id"],),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("approved interview proposal content was mutable")


def test_unknown_evidence_is_rejected_before_creating_a_round():
    from job_search.contracts import ContractError

    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        try:
            service.propose_interview_revision(
                app, details(evidence_id="foreign-evidence"), context("foreign")
            )
        except ContractError:
            pass
        else:
            raise AssertionError("unassociated evidence was accepted")
        assert service.list_interview_rounds()["rounds"] == []


def test_same_calendar_key_divergent_payload_needs_review_and_user_can_resolve():
    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        linked(service, app)
        conflict = reconcile(service, event("v1", "2026-10-01T00:00:00Z"))["results"][0]
        assert conflict["revision"]["status"] == "conflict" and not conflict["applied"]
        decided = service.decide_interview_revision(
            conflict["revision"]["revision_id"],
            "accepted",
            "Verified changed time",
            context("choose-divergent"),
        )
        assert decided["applied"]
        assert (
            reconcile(service, event("v1", "2026-10-01T00:00:00Z"))["results"][0][
                "status"
            ]
            == "unchanged"
        )


def test_calendar_discovery_proposes_first_link_then_refreshes_after_review():
    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        first = accepted(service, app)
        observed = event(starts_at=future(), ends_at=future(hour=16))
        result = service.discover_calendar_interviews(
            "account",
            [observed],
            MutationContext("discovery", "system", "outlook_calendar"),
        )
        assert result["count"] == 1 and result["review_required"]
        assert service.list_interview_rounds()["rounds"][0]["calendar_event_id"] == ""
        revision = result["proposals"][0]["revision"]
        repeat = service.discover_calendar_interviews(
            "account",
            [observed],
            MutationContext("discovery-repeat", "system", "outlook_calendar"),
        )
        assert (
            repeat["proposals"][0]["revision"]["revision_id"] == revision["revision_id"]
        )
        accepted_link = service.decide_interview_revision(
            revision["revision_id"],
            "accepted",
            "Correct recruiter invitation",
            context("approve-link"),
        )
        assert (
            accepted_link["applied"]
            and accepted_link["round"]["round_id"] == first["round"]["round_id"]
        )
        updated = reconcile(service, event("v3", "2026-10-03T00:00:00Z"))["results"][0]
        assert updated["applied"] and updated["round"]["status"] == "rescheduled"


def test_private_holds_are_not_calendar_link_candidates():
    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        accepted(service, app)
        result = service.discover_calendar_interviews(
            "account",
            [event(starts_at=future(), ends_at=future(hour=16), is_organizer=True)],
            MutationContext("discovery", "system", "outlook_calendar"),
        )
        assert result["count"] == 0


def test_failed_task_creation_rolls_back_revision_and_reminder_replacement():
    with TemporaryDirectory() as directory:
        path, _, service, app = setup(directory)
        first = accepted(service, app)
        proposed = service.propose_interview_revision(
            app,
            {
                "round_id": first["round"]["round_id"],
                "status": "rescheduled",
                "starts_at": future(days=13),
                "ends_at": future(days=13, hour=16),
            },
            context("atomic-propose"),
        )

        def fail_task(*args):
            raise RuntimeError("task insert failed")

        service._create_task = fail_task
        try:
            service.decide_interview_revision(
                proposed["revision"]["revision_id"],
                "accepted",
                "Reviewed",
                context("atomic-accept"),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("fixture failure did not abort")
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT status FROM interview_revisions WHERE revision_id=?",
                    (proposed["revision"]["revision_id"],),
                ).fetchone()[0]
                == "pending"
            )
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='pending'"
                ).fetchone()[0]
                == 2
            )
            assert (
                con.execute(
                    "SELECT count(*) FROM interview_reminders WHERE status='dismissed'"
                ).fetchone()[0]
                == 0
            )
        assert service.list_interview_rounds()["rounds"][0]["starts_at"] == future()


def test_cancellation_retires_already_published_reminder_outbox():
    from job_search.notifications import NotificationIntent

    with TemporaryDirectory() as directory:
        path, ledger, service, app = setup(directory)
        first = accepted(service, app)
        with connect(path) as con:
            reminder = dict(
                con.execute(
                    "SELECT * FROM interview_reminders WHERE round_id=? LIMIT 1",
                    (first["round"]["round_id"],),
                ).fetchone()
            )
        notification = ledger.publish_notification(
            NotificationIntent(
                topic="reminder.due",
                source_id="interview:" + reminder["reminder_id"],
                title="Interview reminder",
                body="Upcoming interview",
                application_id=app,
                context={"reminder_id": "interview:" + reminder["reminder_id"]},
            )
        )
        assert notification["created"]
        service.complete_interview_reminder(
            reminder["reminder_id"], "completed", context("published")
        )
        proposed = service.propose_interview_revision(
            app,
            {"round_id": first["round"]["round_id"], "status": "cancelled"},
            context("retire-propose"),
        )
        service.decide_interview_revision(
            proposed["revision"]["revision_id"],
            "accepted",
            "Cancelled by organizer",
            context("retire-accept"),
        )
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT status FROM notification_outbox WHERE topic='reminder.due'"
                ).fetchone()[0]
                == "cancelled"
            )


def test_link_and_metadata_revisions_preserve_tasks_reminders_and_notifications():
    with TemporaryDirectory() as directory:
        path, ledger, service, app = setup(directory)
        first = accepted(service, app)

        def counts():
            with connect(path) as con:
                return (
                    con.execute("SELECT count(*) FROM interview_reminders").fetchone()[
                        0
                    ],
                    con.execute(
                        "SELECT count(*) FROM application_events WHERE event_type='interview_scheduled'"
                    ).fetchone()[0],
                    con.execute("SELECT count(*) FROM notification_outbox").fetchone()[
                        0
                    ],
                )

        before = counts()
        linked_result = accepted(
            service,
            app,
            "add-identity",
            round_id=first["round"]["round_id"],
            calendar_account_id="account",
            calendar_event_id="event-1",
            calendar_uid="uid-1",
            calendar_modified_at="2026-10-01T00:00:00Z",
            calendar_change_key="v1",
            organizer="recruiter@example.test",
        )
        assert linked_result["applied"] and counts() == before
        changed = reconcile(
            service,
            event(
                "metadata",
                "2026-10-02T00:00:00Z",
                starts_at=future(),
                ends_at=future(hour=16),
                location="Room 12",
            ),
        )["results"][0]
        assert changed["applied"] and counts() == before
        assert changed["round"]["task_id"] == first["round"]["task_id"]


def test_confirmed_interview_supersedes_only_associated_availability_task():
    from tests.test_lifecycle_core import evidence
    from job_search.contracts import ApplicationEventType

    with TemporaryDirectory() as directory:
        path, ledger, service, app = setup(directory)
        eid, proposal = evidence(
            ledger, app, event_type=ApplicationEventType.INTERVIEW_REQUESTED
        )
        ledger.decide_event_proposal(
            proposal,
            "accepted",
            app,
            "Availability requested",
            context("request-approved"),
        )
        unrelated = service.create_task(
            app,
            {
                "kind": "send_availability",
                "owner": "applicant",
                "note": "Different round",
            },
            context("unrelated"),
        )["task"]
        first = accepted(service, app, evidence_id=eid)
        assert first["round"]["task_id"]
        tasks = {t["task_id"]: t for t in service.list_tasks(app)}
        assert tasks[unrelated["task_id"]]["status"] == "open"
        assert len([t for t in tasks.values() if t["status"] == "superseded"]) == 1
        attendance = tasks[first["round"]["task_id"]]
        assert (
            attendance["kind"] == "attend_interview" and attendance["status"] == "open"
        )
        reminder = service.list_due_interview_reminders(future(days=20))[0]
        service.complete_interview_reminder(
            reminder["reminder_id"], "completed", context("reminder-notified")
        )
        assert {t["task_id"]: t for t in service.list_tasks(app)}[
            attendance["task_id"]
        ]["status"] == "open"
        done = service.propose_interview_revision(
            app,
            {"round_id": first["round"]["round_id"], "status": "completed"},
            context("attended"),
        )
        service.decide_interview_revision(
            done["revision"]["revision_id"],
            "accepted",
            "I attended",
            context("attended-approved"),
        )
        assert {t["task_id"]: t for t in service.list_tasks(app)}[
            attendance["task_id"]
        ]["status"] == "completed"


def test_terminal_closure_and_reopen_do_not_reactivate_old_interview_proposal():
    with TemporaryDirectory() as directory:
        _, ledger, service, app = setup(directory)
        first = accepted(service, app)
        old = service.propose_interview_revision(
            app,
            {
                "round_id": first["round"]["round_id"],
                "status": "rescheduled",
                "starts_at": future(days=12),
                "ends_at": future(days=12, hour=16),
            },
            context("before-close"),
        )
        correction = service.propose_correction(
            app,
            "phase",
            {
                "target_phase": "terminal",
                "target_outcome": "withdrawn",
                "reason": "No longer pursuing",
            },
            context("close-proposal", "hermes"),
        )["proposal"]
        service.decide_correction(
            correction["proposal_id"], "accepted", context("close-approved")
        )
        assert service.list_interview_rounds()["rounds"][0]["status"] == "cancelled"
        reopen = service.propose_correction(
            app,
            "reopen",
            {"target_phase": "active", "reason": "Employer reopened process"},
            context("reopen-proposal", "hermes"),
        )["proposal"]
        service.decide_correction(
            reopen["proposal_id"], "accepted", context("reopen-approved")
        )
        result = service.decide_interview_revision(
            old["revision"]["revision_id"],
            "accepted",
            "Old tab still open",
            context("old-after-reopen"),
        )
        assert not result["applied"] and result["revision"]["status"] == "stale"
        assert service.list_interview_rounds()["rounds"][0]["status"] == "cancelled"


def test_explicit_reminder_dismissal_cancels_queued_delivery_after_publication():
    from job_search.notifications import NotificationIntent

    with TemporaryDirectory() as directory:
        path, ledger, service, app = setup(directory)
        accepted(service, app)
        with connect(path) as con:
            reminder = con.execute(
                "SELECT reminder_id FROM interview_reminders LIMIT 1"
            ).fetchone()[0]
        ledger.publish_notification(
            NotificationIntent(
                topic="reminder.due",
                source_id="interview:" + reminder,
                title="Interview",
                body="Reminder",
                application_id=app,
                context={"reminder_id": "interview:" + reminder},
            )
        )
        service.complete_interview_reminder(
            reminder, "completed", context("was-queued")
        )
        dismissed = service.complete_interview_reminder(
            reminder, "dismissed", context("user-dismissed")
        )
        assert dismissed["status"] == "dismissed"
        with connect(path) as con:
            assert (
                con.execute(
                    "SELECT status FROM notification_outbox WHERE topic='reminder.due'"
                ).fetchone()[0]
                == "cancelled"
            )


def test_approval_rechecks_current_calendar_instead_of_proposal_time_availability():
    from job_search.contracts import CalendarBlock

    class ChangingCalendar:
        def __init__(self):
            self.conflict = False
            self.reads = 0

        def read_calendar_view(self, *args):
            self.reads += 1
            return (
                [
                    CalendarBlock(
                        "other-event",
                        future(),
                        future(hour=16),
                        "busy",
                        False,
                        False,
                        "version",
                    )
                ]
                if self.conflict
                else []
            )

    with TemporaryDirectory() as directory:
        _, _, service, app = setup(directory)
        calendar = ChangingCalendar()
        service.calendar = calendar
        proposal = service.propose_interview_revision(
            app, details(), context("free-at-proposal")
        )
        calendar.conflict = True
        result = service.decide_interview_revision(
            proposal["revision"]["revision_id"],
            "accepted",
            "Review",
            context("changed-at-approval"),
        )
        assert not result["applied"] and result["revision"]["status"] == "conflict"
        assert calendar.reads == 1 and result["availability"]["checked_at"]
        calendar.conflict = False
        accepted_result = service.decide_interview_revision(
            proposal["revision"]["revision_id"],
            "accepted",
            "Conflict removed",
            context("try-again"),
        )
        assert accepted_result["applied"] and calendar.reads == 2


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"ok ({len(tests)} interview lifecycle tests)")


if __name__ == "__main__":
    main()
