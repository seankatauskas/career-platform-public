# Chief-of-staff notifications and approved career actions

Hermes shares an attention service with the dashboard and background workers. Source
events become auditable attention candidates; the attention policy decides whether
they belong in a briefing, deserve an interruption, or are obsolete.

## Starting behavior

- Daily briefings at **07:00 and 19:00 America/Chicago**, including weekends.
- Monday morning includes the week ahead; Friday evening includes the weekly recap.
- **Important developments** is the initial interruption mode: time-sensitive risks,
  offers, and interview invitations. Risk-only and briefings-only modes are available.
- Quiet hours start disabled. Acknowledgment and snooze affect attention, not task
  completion. An unresolved urgent obligation can receive one final nudge.
- New installations and upgrades start with attention in **shadow mode**. Existing
  automation switches remain authoritative. Shadow decisions and previews do not send.

Briefings use recorded career obligations and the primary Outlook calendar. Private
calendar items are generic commitments. Missing coverage stays visible. A passed
interview time, an unsent draft, or a notification receipt does not prove completion.

## Reply review

The core worker captures verified incoming-message reply context. The model worker
receives bounded, sanitized facts and no Outlook credentials. It can prepare a reply
proposal; it cannot approve or send one. Missing personal facts or meeting details
are surfaced as questions.

Telegram review shows the exact recipients, subject, body, and offered times. A
button or a direct reply to that review can approve the immutable proposal for
15 minutes. Edits require a replacement review. Bare ambiguous approvals do not send.
The trusted Telegram plugin checks the configured owner and private chat before
forwarding an interaction through a separate authenticated ingress.

Sending is a distinct worker capability. A timeout after dispatch is uncertain and
requires reconciliation; it is never blindly retried. Sent Items evidence confirms
sending and resolves the linked reply obligation.

## Confirmed meetings

Structured offered slots are retained with approved availability replies. After
observing the sent reply, the service can match an unambiguous recruiter confirmation
and create a private, busy appointment on **your primary calendar**. It creates no
attendees, invitations, RSVP, or new conferencing session.

Existing invitations and app-owned holds are reconciled before creation. Confirmed
location and meeting links are copied from evidence. Missing times, ambiguous
confirmations, and manually edited events require review. Conflicting commitments
are visible; another appointment is never silently moved. A calendar entry created
by this service is not itself evidence of employer confirmation.

## Configuration and activation

Notification preferences live in the application database and are revision checked.
The separate automation controls are `notifications`, `briefing_ai`, `outlook_send`,
and `calendar_commitments`. The latter three start paused on upgrade.

Optional runtime configuration:

```json
{
  "briefing_ai_enabled": true,
  "briefing_inference_config": "/run/job-search/briefing-inference.json",
  "interaction_port": 8768,
  "interaction_token_file": "/run/job-search/interaction-token",
  "telegram_bot_id": "123456",
  "telegram_user_id": "234567",
  "telegram_chat_id": "234567"
}
```

These example identifiers are placeholders. The interaction bearer must be a
separate owner-only file from the MCP bearer. `python -m job_search --config CONFIG
interaction-token-init` creates it without printing the value. A dedicated briefing
profile controls model egress; the mail worker's profile and credentials are not
implicitly reused. Existing inference budgets apply.

For standalone Compose, add `compose.chief.yaml` after the cloud and Hermes overlays
when interactions are configured, and `compose.briefing.yaml` when the dedicated
model profile is configured. AWS operations select these overlays from runtime
configuration. The broker has no public port and receives no Outlook credential.
The Hermes plugin reuses its existing Telegram receiver and restricts the model's
effective tools to the approved Career Platform MCP tools.

Microsoft consent can be extended with `python -m job_search --config CONFIG
outlook-auth --device-code --enable-drafts --enable-send --enable-holds`. Sending
requires [`Mail.Send`](https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0); personal appointments require
[`Calendars.ReadWrite`](https://learn.microsoft.com/en-us/graph/api/user-post-events?view=graph-rest-1.0). Background
workers never start interactive authentication.

Review representative shadow briefings first, then activate the desired controls
and turn off shadow mode in notification preferences. Activation records a baseline
so historical events cannot drain as individual alerts. Current unresolved
obligations remain visible in the first briefing. Uncertain deliveries appear in Settings → Chief of staff. An exact, audited
“Received” or “Abandon” decision resolves them without resending or approving a
recruiter email. A later delivery receipt can resolve uncertainty, but cannot
reactivate a cancelled notification.

## Ownership and verification

- `job_search/attention/` owns policy, portfolio snapshots, grounding, and provenance.
- `job_search/career_actions/` owns reply proposals, send recovery, agenda snapshots,
  and personal calendar commitments.
- `job_search/interactions/` owns exact review tickets and trusted Telegram ingress.
- `job_search/chief_runtime.py` composes these services and their worker boundaries.

Run the offline suites through `uv run --python 3.11 --with cryptography --with pypdf
--with reportlab python scripts/check-system.py --browser`. Image checks use
`tests/native_hermes_contract.py` as the Hermes user and
`tests/native_hermes_init_contract.py` as root inside the pinned Hermes image,
with networking disabled and a read-only repository mount. The release workflow requires
these native checks through `scripts/hermes-healthcheck-acceptance.py`. Production
Compose acceptance also starts the interactions broker and checks the dedicated
briefing-profile mount boundary using fictional configuration. The chief runtime and domain suites use temporary databases
and fake providers. Production activation and live Microsoft consent are separate
from offline verification. Follow the existing AWS release and recovery runbook. This release upgrades the
application database from deployed schema 16 to 19; returning to the previous release
requires its pre-upgrade state backup.

Broader interview preparation, search coaching, debriefs, and offer comparisons
remain outside this foundation.
