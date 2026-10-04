# Daily public source snapshots

Development and AWS deployments remain in the private `seankatauskas/career-platform`
repository. `seankatauskas/career-platform-public` is an independent snapshot
repository with its own history: one initial commit, then at most one update per
Chicago calendar date when exported files change. Do not develop directly in it.

`public-snapshot.yml` runs at `0 22 * * *` with `timezone: America/Chicago` and also
supports manual dispatch. GitHub may delay scheduled runs. The publisher uses the
Chicago date when publication runs, honors daylight saving time, serializes runs,
and treats repeated runs on the same date as no-ops. Changes after that day's
publication wait until the next date. Unchanged snapshots produce no empty commits.

The workflow pins its source to `GITHUB_SHA`, exports committed files without
private Git history, and runs the exported offline/browser suites before obtaining
the publication key. The export preserves executable modes, documentation, demo
media, infrastructure source, and license attribution. Public CI remains active;
AWS and publishing workflows become inactive examples. Active private workflows
take precedence over older files with matching names in the examples directory.

The private-file guard checks committed bytes, including uncommitted replacements
that might otherwise conceal a secret. Personal resumes, databases, credentials,
Terraform state and live tfvars are rejected. Unknown paths, symlinks and submodules
also require explicit review. Keep real runtime data outside Git. The scanner is
an additional check, not proof that arbitrary new content is safe to disclose;
review screenshots, fixtures, documentation and newly tracked files before merging.

The review skill, its interface reference, and the Codex review container's Dockerfile
and ignore file are explicitly allowed in `scripts/prepare-public-showcase.py`.
Additional skill or container paths still require review before adding them to the
export allowlist.

## Credentials and boundaries

`PUBLIC_MIRROR_SSH_KEY` is an Actions secret in the **private** repository. Its public
key is installed as a write-enabled deploy key only on `career-platform-public`.
The public repository contains no AWS environment, deployment credential, or mirror
private key. The publishing job has read-only `GITHUB_TOKEN` permissions and no
OIDC permission. It loads the SSH key only for the final push, verifies GitHub host
keys from GitHub's HTTPS metadata endpoint, and deletes temporary key files on exit.

Initial setup uses a fresh `main` checkout with no commits. Run:

```sh
python3 scripts/publish-public-snapshot.py --public-checkout /path/to/public-checkout
```

The initial commit retains the MIT license and attribution but none of the private
commit objects, branches, tags, pull requests or Actions logs. Keep export receipts
and private source revisions in private Actions logs only.

## Failure and recovery

Inspect **Publish daily public snapshot** in the private repository's Actions tab.
An export or test failure does not push. A failed push can be retried by rerunning
the workflow: the publisher reads the destination's latest commit and skips an
already published date. Pushes are ordinary fast-forward updates to `main` only;
unexpected destination commits or concurrent changes fail instead of being forced.
Use the last successful public commit until the failing check is corrected.

Rotate the deploy key by adding the replacement public key to the public repository,
replacing the private Actions secret, testing a run, then removing the old key.
Disable only `public-snapshot.yml` to pause publication; AWS operations are independent.

Validate changes with `python3 -m tests.test_public_snapshot`. Also run the usual
collector and exported system suites before publishing. Test fixtures cover initial
history, daily coalescing, retries, unchanged days, file deletion, executable files,
dirty destinations, committed secrets, symlinks and workflow export precedence.
