# Claude Dependabot Gate

A GitHub Action that decides whether a Dependabot pull request is safe to merge, and only passes it on evidence.

Dependabot's compatibility score tells you how the same update went in *other* public repos. This gate checks it against *your* code, on the commit that will actually land.

## What it does

For each Dependabot PR:

1. **Builds and tests the merge result.** It runs your `test-command` on the PR merged into the latest base branch, not on the PR branch alone.
2. **Has Claude investigate the impact.**
   - Claude reads what changed upstream: release notes, changelogs, migration guides and advisories, using web search and fetch.
   - It finds every place your code uses an API that changed, using read-only code search. It includes call sites it thinks are unaffected.
   - For each of those call sites, it checks whether an existing test actually exercises it.
3. **Gives a verdict:**

| Verdict | When |
|---|---|
| ✅ **pass** | Build and tests passed on the merge result, Claude rates the risk low with medium or high confidence, and every affected call site is covered by a test. |
| 👀 **needs-human** | No tests were run, or the workspace wasn't the merge result, or Claude rates the risk medium, or has low confidence, or found an affected call site with no test, or couldn't finish. |
| ❌ **fail** | The build or tests failed or timed out, or Claude found code the update breaks (high risk). |

Claude's opinion can block a PR or send it to a human, but it can never pass one on its own. A pass always needs passing tests on the merge result.

4. **Reports it:** one PR comment that's updated on every push, a `dependabot-review:pass|needs-human|fail` label, a check result and, optionally, auto-merge.

It works with any ecosystem Dependabot supports (npm, yarn, pnpm, NuGet, pip, Poetry, uv, Go, Maven, Gradle, Cargo, Docker, GitHub Actions and more).

## Setup

1. **Add the API key as a Dependabot secret.** Workflows triggered by Dependabot can't read normal Actions secrets. Add `ANTHROPIC_API_KEY` under **Settings > Secrets and variables > Dependabot**, or once at the organization level.
2. **Add the workflow.** Copy [examples/dependabot-review.yml](examples/dependabot-review.yml) to `.github/workflows/`, then set the toolchain step and `test-command`.
3. **Make the check required.** In branch protection or a ruleset for `main`, require the `dependabot-gate` status check.
4. **Make sure what you tested is what lands.** See the next section.
5. **If `GH-ABN/depbotreviewer` is private:** in its **Settings > Actions > General > Access**, allow repositories in the organization to use it.

## Making sure what you tested is what lands

The PR's merge result is computed against `main` *as it was when the check ran*. If other PRs merge afterwards, the tested state is out of date. There are two ways to close that gap:

- **Merge queue (recommended).** Turn on the merge queue for `main`. When a PR enters the queue, GitHub builds the exact commit that will land, and the workflow's `merge_group` trigger re-runs your build and tests on it. Claude isn't called again, so this costs no tokens. The merge queue is available for organization repositories: public ones, and private ones on GitHub Enterprise Cloud.
- **Require branches to be up to date.** Turn on **Require branches to be up to date before merging**. Dependabot PRs then have to be updated with `main` before merging. Each update triggers a fresh check, including a new Claude review.

## Auto-merge (optional)

With `auto-merge: true`, the action turns on GitHub auto-merge when the verdict is **pass**, and turns it off again if a later push doesn't pass. GitHub then merges once every required check is green, through the merge queue if you use one.

It needs:
- `contents: write` permission;
- **Allow auto-merge** turned on in the repository settings;
- branch protection with required checks. Without them there's nothing to wait for, and GitHub refuses to turn auto-merge on.

Merges made with the default `GITHUB_TOKEN` don't trigger `push` workflows. If you deploy on push to `main`, pass a GitHub App token or a fine-grained PAT as `github-token`.

## Inputs

| Input | Default | Description |
|---|---|---|
| `anthropic-api-key` | (required) | Anthropic API key. |
| `github-token` | `github.token` | Token used to read the PR and write the comment, labels and auto-merge. |
| `test-command` | (empty) | Shell command that builds and tests the PR. **Required for a pass.** |
| `test-timeout-minutes` | `20` | Timeout for `test-command`. A timeout counts as a failure. |
| `fail-on` | `fail` | `fail`: the check fails only on a failing verdict, so people can still merge needs-human PRs after reading the review. `needs-human`: anything short of a pass fails. `never`: the check always succeeds. |
| `auto-merge` | `false` | Turn on auto-merge for passing PRs. |
| `auto-merge-method` | `squash` | `squash`, `merge` or `rebase`. |
| `model` | `claude-opus-5-5` | Claude model. |
| `effort` | `high` | `low`, `medium`, `high`, `xhigh` or `max`. |
| `max-turns` | `30` | Investigation turns before Claude must give its verdict. |
| `post-comment` | `true` | Post or update the PR comment. |
| `add-labels` | `true` | Apply a verdict label. |
| `label-prefix` | `dependabot-review:` | Prefix for verdict labels. |
| `allowed-authors` | `dependabot[bot]` | Comma-separated PR authors to gate. Add `renovate[bot]` for Renovate. |
| `pr-number` | (from the event) | Set this when running from `workflow_dispatch`. |

**Outputs:** `verdict` (`pass`, `needs-human` or `fail`), `risk` (`low`, `medium`, `high` or `unknown`), `confidence`, and `skipped`.

## Security

- Claude's repository tools are **read-only** and confined to the checkout; they can't read `.git`. Claude has no shell. Your `test-command` is the only thing that runs code, and it runs without the Anthropic key in its environment.
- The PR description, release notes, web pages and repository contents are treated as untrusted data. The prompt tells Claude to ignore instructions inside them and report any such attempt. Text Claude writes into the comment can't @-mention people.
- The workflow uses `pull_request`, not `pull_request_target`.
- The reviewer runs with Python's `-P` flag, so files in the reviewed repository can't replace the reviewer's own modules.

## Cost

Each review is one Claude conversation of about 5–30 turns. The conversation is prompt-cached, so each turn re-reads earlier context at the cache rate. Token usage is printed in the job log. Merge-queue runs don't call Claude. To spend less, lower `effort` or `max-turns`.

## Requirements

Linux or macOS runners with Python 3.11 or newer. GitHub-hosted `ubuntu-latest` and `macos-latest` qualify. The action uses the runner's own `python3` in a private virtualenv and doesn't change the Python version your job uses.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

| Path | Purpose |
|---|---|
| `action.yml` | Composite action definition |
| `src/dep_reviewer/__main__.py` | Orchestration: PR review mode and merge-queue mode |
| `src/dep_reviewer/gate.py` | Verdict rules and merge-result detection |
| `src/dep_reviewer/claude_review.py` | Claude agent loop (tool use, turn limit, refusal and pause handling) |
| `src/dep_reviewer/repo_tools.py` | Read-only repository tools exposed to Claude |
| `src/dep_reviewer/prompts.py` | System prompt and PR context message |
| `src/dep_reviewer/schema.py` | Structured-output schema for Claude's review |
| `src/dep_reviewer/report.py` | PR comment rendering |
| `src/dep_reviewer/github_api.py` | Minimal GitHub REST and GraphQL client |
