# Claude Dependabot Reviewer

A GitHub Action that reviews Dependabot pull requests with Claude and tells you how likely each update is to break your code, with evidence.

For each Dependabot PR it:

1. Reads the PR: which packages changed, from and to which versions, the release notes Dependabot included, and the manifest and lockfile diffs.
2. Optionally builds and tests the PR with a command you provide, such as `npm ci && npm test`.
3. Lets Claude investigate:
   - It looks up what changed upstream (changelogs, release pages, migration guides, advisories) using web search and fetch.
   - It finds where your repository uses the dependency (imports, call sites, config, CI workflows, Dockerfiles) using read-only code search.
   - It checks the upstream changes against those usages and against your declared runtime versions, and weighs the test result.
4. Posts a comment on the PR with a risk level, the changes that matter, links to the affected lines, the test assessment and recommendations. It updates the same comment on each new push.
5. Labels the PR `dependabot-risk:low`, `dependabot-risk:medium` or `dependabot-risk:high`. It uses `dependabot-risk:unknown` if the review couldn't finish.

It works with any ecosystem Dependabot supports (npm, yarn, pnpm, NuGet, pip, Poetry, uv, Go, Maven, Gradle, Cargo, Docker, GitHub Actions and more), because Claude reads the diff and searches your code rather than relying on per-ecosystem parsers.

## Setup

1. **Add the API key as a Dependabot secret.** Workflows that Dependabot triggers can't read normal Actions secrets. Add `ANTHROPIC_API_KEY` under **Settings > Secrets and variables > Dependabot**. To use it across many repositories, add it once as an organization Dependabot secret.
2. **Add the workflow.** Copy [examples/dependabot-review.yml](examples/dependabot-review.yml) to `.github/workflows/` and set the toolchain step and `test-command` for your project.

```yaml
- uses: actions/checkout@v4
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- uses: actions/setup-node@v4
  with: { node-version: 22, cache: npm }
- uses: GH-ABN/depbotreviewer@v1
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    test-command: npm ci && npm test
```

The job needs `pull-requests: write` and `issues: write` permissions to comment and label.

## Inputs

| Input | Default | Description |
|---|---|---|
| `anthropic-api-key` | (required) | Anthropic API key. |
| `github-token` | `github.token` | Token used to read the PR and write the comment and labels. |
| `model` | `claude-opus-5-5` | Claude model. |
| `effort` | `high` | `low`, `medium`, `high`, `xhigh` or `max`. Higher effort investigates more thoroughly and costs more. |
| `max-turns` | `30` | Investigation turns before Claude must give its verdict. |
| `test-command` | (empty) | Shell command that builds and tests the PR. Claude reads the result and the full log. |
| `test-timeout-minutes` | `20` | Timeout for `test-command`. |
| `post-comment` | `true` | Post or update the PR comment. |
| `add-labels` | `true` | Apply a risk label. |
| `label-prefix` | `dependabot-risk:` | Prefix for risk labels. |
| `fail-on-risk` | `none` | `medium` or `high` makes the step fail at or above that risk, so you can make the review a required check. |
| `allowed-authors` | `dependabot[bot]` | Comma-separated PR authors to review. Add `renovate[bot]` for Renovate. |
| `pr-number` | (from the event) | Set this when running from `workflow_dispatch`. |

**Outputs:** `risk` (`low`, `medium`, `high` or `unknown`), `confidence`, and `skipped`.

You can chain on the output. For example, to approve low-risk PRs automatically:

```yaml
- uses: GH-ABN/depbotreviewer@v1
  id: review
  with: { anthropic-api-key: "${{ secrets.ANTHROPIC_API_KEY }}", test-command: npm ci && npm test }
- if: steps.review.outputs.risk == 'low'
  run: gh pr review --approve "${{ github.event.pull_request.number }}"
  env: { GH_TOKEN: "${{ github.token }}" }
```

## How risk is decided

- **Low:** nothing that changed upstream touches code this repository uses, runtime requirements are still met, and tests pass.
- **Medium:** there are breaking or behavior changes that Claude could not confirm as harmless, or the affected code has no tests, or no tests were run and the usage is non-trivial.
- **High:** Claude found code that breaks, the build or tests fail because of the update, a runtime or peer requirement is no longer met, or the release looks suspicious.

Every verdict comes with a confidence level and links to the evidence. The comment is advice, so a person should still read it before merging.

## Security

- Claude's repository tools are **read-only** and confined to the checkout; they can't read `.git`. Claude has no shell. Your `test-command` is the only thing that runs code, and it runs without the Anthropic key in its environment.
- The PR description, release notes, web pages and repository contents are treated as untrusted data. The prompt tells Claude to ignore instructions inside them and report any such attempt. Text Claude writes into the comment can't @-mention people.
- The workflow uses `pull_request`, not `pull_request_target`, so it never runs PR code with elevated access to a fork's PR.
- The reviewer runs with Python's `-P` flag, so files in the reviewed repository can't replace the reviewer's own modules.

## Cost

Each review is one Claude conversation of about 5–30 turns. The conversation is prompt-cached, so each turn re-reads earlier context at the cache rate. Token usage is printed in the job log. To spend less, lower `effort` or `max-turns`. Grouped updates and major versions take the most turns.

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
| `src/dep_reviewer/__main__.py` | Orchestration: PR context, tests, review, comment, labels |
| `src/dep_reviewer/claude_review.py` | Claude agent loop (tool use, turn limit, refusal and pause handling) |
| `src/dep_reviewer/repo_tools.py` | Read-only repository tools exposed to Claude |
| `src/dep_reviewer/prompts.py` | System prompt and PR context message |
| `src/dep_reviewer/schema.py` | Structured-output schema for the verdict |
| `src/dep_reviewer/report.py` | PR comment rendering |
| `src/dep_reviewer/github_api.py` | Minimal GitHub REST client |
