# Interface design language

Career Platform uses graphite surfaces, off-white type, and muted brass selection cues. The homepage illustration and the working interface should describe the same product. Favor legible records, clear selection, and predictable controls over decorative dashboard elements.

## Surfaces and hierarchy

The dark workspace background is `#141414`. Navigation and lists use `#181817` / `#1a1a19`; the selected application record uses `#252523`. Borders are quiet neutral gray. Brass (`#d0c6b1`) identifies selection, current progress, focus, and primary actions. Error, warning, and operational readiness retain distinct semantic colors and text labels.

Light mode uses the same structure with neutral paper and pale brass selection. The dashboard preserves the existing saved theme preference and light default. The ranking and salary tools follow the operating system's light/dark preference; the extension popup uses the graphite theme.

## Typography and components

Use the local Helvetica/Arial stack. Body and control text use 14–15px, metadata at least 12px, component headings 16–20px, and page headings 24px. Use weight and contrast before introducing another size. Monospace is reserved for identifiers and technical payloads.

Space surfaces on an 8px rhythm. Panels use 6–8px corners, thin borders, and no decorative shadows or gradients. Lists should recede behind the selected record. Role names lead application rows, with company names underneath. Application headers put the current status beside the record label. History is a vertical timeline with the latest event distinguished. Messages read as correspondence; draft review actions are separated by a divider rather than another nested card.

The same palette applies to the dashboard, ranking interface, salary tools, and extension popup. Keyboard focus remains visible. Narrow layouts stack records and controls without clipping content. Color must not be the sole indicator of application or operation status.

Application lists and records are separate destinations at every width. Records have a maximum reading width of 960px. At widths up to 1180px, navigation becomes a horizontal row. All applications returns to the list and its existing filters; selecting a role opens its record at the top of the page. Browser Back follows these routes. Correspondence uses 15px text in this layout, and primary record controls use 14px text. The same layout serves tablets, smaller windows, and blog recordings.

## Media

Capture the actual interface using the isolated fictional demo in [demo.md](demo.md). Record in dark mode. The portfolio film uses the ordinary 800 × 640 compact layout and deliberate cuts; detailed stills can frame an actual component more tightly. Preserve the demo-data disclosure and the distinction between real local services and simulated providers. Do not use production applications, mail, or personal records for public screenshots.

## Navigation and daily work

The primary navigation is Applications, Shortlist, Review, and Settings. Applications is the default landing page. Its total belongs within the page; only Review has a navigation badge, counting unresolved decisions. A persistent bell opens system notifications even when empty. Paused or unused capabilities do not create an alert. Theme switching uses a moon/sun icon with a text alternative.

Application records have Overview, Messages, and Documents tabs. Overview contains pending decisions, interview details, application history, and collapsed posting history. Role titles open records; the shared job-description preview is available inside each record. Posted, updated, and applied dates share one metadata line. Later lifecycle stages take precedence over submission evidence. Opening a role never creates an application.

Shortlist preserves separate Codex picks and model picks. List history and caller ordering remain intact. Posting window and remote filters stay visible; ranking controls and scores are secondary. Review uses one normalized queue for cards, badges, and application notices, with reconciliation first, deadlines next, then oldest items. Decision labels describe the actual effect. Email reply drafts remain available; preparation and tailored resume generation controls are retired from the everyday interface.

Both shortlist sources show a quiet “Applied recently” text link beside the company when a recorded submission or confirmation falls within the rolling 180-day window. Use secondary text, regular weight, and a subtle underline without a filled badge or border. The link opens the latest qualifying application, including applications to other roles at that company, and explains “Applied to this company within the last 180 days” and shows its date in the tooltip and accessible label. Drafts and unconfirmed submission attempts do not qualify. Rejected or withdrawn applications still count as prior applications. The indicator is refreshed when a saved or cached shortlist is loaded.

The shared “Exclude recently applied companies” checkbox defaults to enabled for both Codex and model picks and uses the same company-history indicator. It filters the loaded list immediately, preserves its original ranks and saved contents, and reports how many roles are hidden. The choice persists in this browser across reloads and source changes. If every role is hidden, the empty state explains how to show them again.

Settings holds connections, career profile and saved resumes, operations, and read-only earlier drafts. Saved facts are shown before editing controls. Operations shows problems before healthy or paused services. Documents resolve the resume recorded at submission, never the current preferred resume. Missing files retain an explicit unavailable state.

## Implementation coordination

The orchestrator owns routing, shared components, API integration, and release checks. Parallel agents own Applications, Shortlist/Review, and Settings/Operations modules in isolated worktrees. Their templates, rendering, and namespaced styles are separate files; integration tests exercise the combined routes and data contracts. Run the offline console, operations, review-queue, and settings browser suites alongside the Python system check before updating the local runtime. Compare runtime files with the pre-change baseline and back up touched files so concurrent work is preserved.
