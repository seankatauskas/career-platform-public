# Unified costs and credits

Settings → Operations → Costs and credits reads a saved host snapshot for AWS,
Runpod, and OpenRouter. Checking or refreshing the dashboard never makes billing
API requests, starts inference, or reads provider credentials. Production remains
unchanged until an authorized release installation and explicit collector setup.

## What the figures mean

| Provider | Figures | Scope and reporting window |
| --- | --- | --- |
| AWS | Charges before credits/refunds, signed credit adjustments, signed refunds, net reported cost | Entire authenticated AWS account/billing view, current UTC month through completed UTC days; today's incomplete day is excluded. Uses `UnblendedCost`, grouped by record type. |
| Runpod | Prepaid account balance, lifetime account usage, current hourly rate | Entire API-key account, including workloads outside this application. Hourly rate is a point-in-time provider value, not a forecast. |
| OpenRouter | Lifetime purchased credits, lifetime account usage, remaining prepaid balance | Account-wide, only when a separate management key is configured. Balance is purchased credits minus usage. |
| OpenRouter inference key | Lifetime usage and provider-reported monthly usage | Only the configured inference key, not other keys or an account total. BYOK provider charges are outside these figures. |

AWS credits/refunds retain their signs: net cost equals the other charges and
adjustments plus credits plus refunds. The pre-credit number includes any other
record types such as taxes, fees, and discounts; it is not exclusively EC2 usage.
Applied promotional credits do **not** reveal remaining promotional credit.
AWS may mark its period estimated, and net reported cost is not a final invoice.
The first UTC day of a month has no completed days: the AWS card reports that
condition instead of displaying a manufactured zero.

These quantities are deliberately not combined into a grand total. They cover
different periods and scopes, and a balance is money remaining rather than money
spent. No local token-count-to-dollar approximation is presented as provider billing.

Every card includes last successful collection and, when different, the latest
attempt. Failed updates retain prior figures with their original timestamps.
Figures older than 36 hours are labeled stale. Missing, malformed, unauthorized,
or unavailable data is not converted to zero. OpenRouter can still show key-only
usage when account credit reporting is not configured.

## Host setup and permissions

The collector is host-only: `job_search.cost_collector` is excluded from the app
image. The dashboard receives only a read-only directory mount and
`JOB_SEARCH_COST_SNAPSHOT=/run/job-search-costs/snapshot.json`. The app-side reader
is `job_search.cost_snapshot`; `GET /api/v1/ops/costs` uses the existing dashboard
access boundary. No refresh or credential-setting mutation endpoint is provided.

Follow [the AWS host setup recipe](../../infra/aws/README.md#optional-unified-cost-monitor)
for IAM and systemd installation. Configuration is an optional `costs` object in
the private operations configuration, not in a checked-in runtime file:

```json
{
  "enabled": true,
  "aws_enabled": true,
  "runpod_key_file": "/var/lib/job-search/private/runpod-api-key",
  "openrouter_key_file": "/var/lib/job-search/private/openrouter-api-key",
  "openrouter_management_key_file": null,
  "thresholds": {}
}
```

The object above is the value of `costs`; preserve the other operations settings.
The `enabled` flag defaults to false. `aws_enabled` defaults to true when the
collector is enabled. Provider inference-key paths default to the existing host
private directory; set either to `null` to omit that provider. A missing credential
for an enabled provider appears as a setup error rather than an empty balance.

AWS uses the instance role and only `ce:GetCostAndUsage`; the collector never
enables Cost Explorer or changes billing settings. The report covers what that
role/account is allowed to see, which may include unrelated services or linked
accounts when run in a payer account. Cost Explorer must already be available.
Runpod makes one fixed read-only GraphQL query. OpenRouter makes one `/key` request
for the inference key, plus `/credits` only when a management key path is supplied.
Runpod can deny individual fields, such as lifetime usage, while allowing the
account balance. Those known field-level permission denials produce a partial
card with only the permitted figures; they never become zero usage or require
broadening the API key. Other GraphQL errors fail the update closed.

Do not create an OpenRouter management key solely for convenience without
considering its broader authority. It is optional; the existing inference key is
enough for clearly labeled key usage. A configured management key must be a
root-owned mode-0600 regular file in production. It is never mounted into a
container or added to the Hermes secret. Existing inference keys may be root/app
owned mode 0600/0640. Symlinks, group-writable, executable-group, and world-accessible
key files are refused. HTTP redirects are refused so Authorization headers cannot
be forwarded to another origin. Neither credentials nor raw API/CLI errors are
written to the snapshot or logs.

The systemd timer wakes periodically but the collector makes requests at most
once per 24 hours. It shares the existing operations lock, verifies the data
volume, and refuses incomplete deployment/recovery operations. A private attempt
marker is atomically written and fsynced **before** requests, so termination or a
failed snapshot write cannot cause repeated paid polling. A malformed/future marker
fails closed; inspect the host clock and marker during operator recovery instead
of deleting it merely to force another paid call. A normal failed provider update
is retried at the next daily opportunity, not whenever the dashboard is opened.

The sanitized snapshot lives at `<data_root>/costs/snapshot.json`, mode 0640 with
the application group. It is atomically replaced and fsynced. The attempt marker
is root-private mode 0600. Costs are derived data outside the application ledger;
no application schema or event history changes are required.

## Optional warnings

No financial warning thresholds are enabled by default. The host configuration
accepts nonnegative USD values under `costs.thresholds`:

- `aws_monthly_charges_usd`: warn when pre-credit charges reach this value.
- `runpod_balance_usd`: warn when account balance falls to this value.
- `openrouter_balance_usd`: warn when account balance falls to this value;
  unavailable when account credit reporting is not configured.

Warnings appear in the dashboard, including whether they depend on stale figures.
An old month's AWS figure does not trigger a new month's warning. These are
**informational warnings**, not spending caps or automatic resource shutdowns;
they do not send notifications or alter the existing inference usage controls.

## Cost of checking and documentation sources

AWS charges for each Cost Explorer paginated API request. The normal small-account
query is one page per day (about $0.30 for 30 days at $0.01 per request); pagination
is bounded at ten pages per attempt. That is an estimate for the query itself,
not the hosting cost, and extra pages cost more. No hourly granularity, forecasts,
or repeated browser-triggered queries are enabled. AWS refreshes its underlying
data at least daily, but some upstream billing records can take longer than 24
hours, so collection time is not proof all recent usage has been billed.

Official contracts checked for this implementation:

- [AWS GetCostAndUsage](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html)
- [AWS Cost Explorer reporting lag](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html)
- [AWS Cost Explorer API pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)
- [Runpod GraphQL specification](https://graphql-spec.dev.runpod.io/)
- [OpenRouter account credits](https://openrouter.ai/docs/api/api-reference/credits/get-credits)
- [OpenRouter current API key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)

Offline verification: `python3 -m tests.test_cost_dashboard` and
`node tests/browser/test_ops_browser.mjs`. These use synthetic billing values,
failure fixtures, the real dashboard assets, and existing access checks; they do
not query live billing accounts or verify production IAM permissions.
