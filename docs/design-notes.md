# Design notes

Why the factory is built the way it is, and what the first runs taught. The README has the overview and the graph.

## VM vs host

The work happens in a microVM, the orchestration on the host.

| Where | What runs there | What it can reach |
| --- | --- | --- |
| VM (`claude_factory` harness of sandboxed-agents) | The Claude Agent SDK running the stage command and its subagents, the repo's `make check test` | DeepSeek's API, nothing else. The API key is swapped in by the host's proxy, so the VM never sees it |
| Host (this repo) | The LangGraph thread, the approval, SQLite, Langfuse, `gh`, the commit and the push | GitHub through the logged-in `gh`, the session API |

The split follows from who must hold what. The agent reads untrusted issue text and runs code, so it gets the smallest reach that still lets it work: a model endpoint and a working tree. Every action with an effect outside the VM (a comment, a label, a branch, a PR) is taken by host code that does not interpret the agent's output beyond one JSON line and a few file paths.

One sandbox session per issue keeps one VM and one working tree from research to validate. Each stage is one message with `fresh_context: true`, so the stage agent starts without the previous stage's conversation, as the commands expect, but on the tree the previous stage left: the research document is there for the plan stage, the plan for implement, the code for validate. The VM stays alive during the approval wait (about 126 MiB idle); if it is lost, the sandbox lab rebuilds it from the repo plus the saved patch.

## Headless contract vs forking the commands

The four commands are written for an interactive session: `create_plan` asks questions, `implement_plan` pauses for manual verification after each phase, all of them call scripts that only exist on my machine. Two ways to run them headless:

| Option | For | Against |
| --- | --- | --- |
| Fork the commands into factory variants | Each one says exactly what the factory needs | Two copies drift; an improvement to a command never reaches the factory |
| Prepend a contract to the unchanged command (chosen) | The factory always runs the current command; the contract is one place, in `src/factory/stages.py` | The contract has to override instructions it does not see, so it must be phrased generally |

The contract says that no human is available, that where the command says to ask, wait or pause the agent decides and writes the assumption into the document, which scripts to skip, that subagents must be spawned with the Agent tool where the command says so, and how to end: one `FACTORY_RESULT: {...}` line with status, artifact path and summary. Each stage adds a note (where to write the document; for implement, run every phase with no pause; for validate, run `make check test` and change nothing). `commands_sha`, the hash of contract plus command file, is stored with every stage run and sent to Langfuse, so a change in either shows up next to the numbers it changed.

The contract had to be explicit about delegation: without the sentence about the Agent tool, DeepSeek did the subagents' research itself.

## `setting_sources=[]`, and why `CLAUDE.md` never enters the VM

The Claude Agent SDK loads every settings source when `setting_sources` is not passed, including the project's `.claude/settings.json` with its hooks. The factory works on repositories whose content it does not control, so `claude_factory` passes `setting_sources=[]`: no settings, hooks, `CLAUDE.md`, commands or agents are loaded from the repo or from a home directory.

What does cross into the VM is chosen on the host:

- the command file's body, as the text of the message;
- the agent files from `LAB_FACTORY_AGENTS_DIR`, as programmatic subagent definitions (description, tools, model, prompt), hashed into `agents_sha`.

A personal `CLAUDE.md` stays out on purpose. It holds personal context, and everything in the VM's prompt goes to the model provider.

## The approval trust boundary

There is exactly one human gate, between the plan and any code. A decision counts only if all of these hold (`github.find_decision`):

- the comment's author login equals `FACTORY_APPROVER`;
- it was created after the plan comment, by `created_at`, so editing an older comment cannot approve;
- its stripped body starts with `/approve` or `/reject`; a sentence that mentions `/approve` later on is not a decision.

The first comment that qualifies wins. Anyone can comment on a public issue; only the approver's comments are read as decisions. The approver approves a plan they have read on the issue, not a summary of it.

The second gate is the PR itself: the factory never merges, and a human reviews every change like any other.

## The patch as the only channel out

The VM has no GitHub access and no shared folder with the host. After every run the sandbox lab saves the working tree as `workspace_saved`: a binary patch against the session's starting commit, untracked files included. That is the only way anything leaves the VM, and it is enough:

- the stage documents under `thoughts/` arrive in it, and `workspace.materialize` (a fresh clone at the base commit plus `git apply --binary`) makes them readable on the host; `read_artifact` refuses paths outside the tree;
- the PR is the same patch minus `thoughts/`, committed by the host as the local git user on `factory/issue-<n>`. The host refuses an empty diff and a diff that touches `.github/`, since a workflow change could reach the repository's secrets.

## Measuring cost when subagents do half the work

The sandbox lab's first spike compared the reported cost with what the run actually cost. The SDK's result totals and its streamed usage cover the main agent only: on a `research_codebase` run the reported cost was 56% of the true cost, and the budget check missed the subagents too. Summing the subagents' own messages that do reach the SDK got to 75%, because they carry no output tokens and each subagent's final call never arrives as a message.

The fix is in the sandbox lab's runner: it reads the CLI's subagent transcripts inside the VM, keeps each model call once, and adds them to `usage`, `cost_usd` and the budget check. Afterwards a run's `cost_usd` matched the main plus subagent transcripts to the last digit, with the subagents at 52% of the cost. The live budget can still overshoot by about one subagent call, since a call is seen only once its transcript line is written. Every number in [runs.md](runs.md) includes the subagents.

## Threat model for untrusted issue text

Anyone who can open an issue on the target repo controls text that reaches the agents. An issue is only picked up once it carries the `factory` label, which needs write access, but the issue body and later edits are still outside the factory's control.

| What an injected instruction could try | What stops or bounds it |
| --- | --- |
| Make the agent act on GitHub (comment, push, approve itself) | The VM has no GitHub token and no network beyond the model provider; only host code calls `gh` |
| Reach a shell on the host | `gh` and `git` are called with argv lists, never shell strings; issue text goes through stdin |
| Run hooks or settings from the repository | `setting_sources=[]` |
| Approve its own plan | Only the approver's comments, created after the plan comment, count |
| Smuggle harmful code into a PR | The approver reads the plan before any code is written; a diff touching `.github/` is refused; no auto-merge, a human reviews the PR |
| Exfiltrate data | The VM sees only the public repo and the commands and agents; it can send only to the model provider, which receives all of that anyway |
| Burn money or time | Per-stage limits on tool calls, wall time and cost, enforced in the VM's runner and the session |

The agents are also told that the issue text between the `<issue>` markers is a user report, not instructions. That is a mitigation, not a boundary: the boundaries are the VM, the gate before code and the review of the PR.

## Lessons from the runs

Issue #3 reached its PR on the third attempt; the other three issues got there on the run in the report. What the runs showed:

- **The session API going down fails the stage.** On one attempt the API was stopped mid-run; validate failed with a connection error and the issue was labelled `factory:failed`. `watch` does not check that the API is up before it picks an issue, so with the API down a queued issue fails right away instead of waiting. A health check before picking would fix that.
- **Fields asked for in prose get dropped.** The validate contract asked for `tests_exit_code` in a sentence, and the agent validated correctly but left the field out of its `FACTORY_RESULT` line, which fails validation by design. The field now appears in the template of the line itself; the model copies the line's shape reliably.
- **A retry starts over.** There is no resume from a failed stage: removing the status label queues the issue again, which reruns research and plan and needs a new `/approve`. Cheap at $0.09 to $0.11 for the first two stages, but it costs the approver another round. The report shows each issue's current run; the abandoned ones stay in `var/factory.db`.
- **A retry after a pushed branch fails at the push.** The PR branch is always `factory/issue-<n>`, created fresh from the base commit. If a previous run already pushed it, the new push is rejected as non-fast-forward and the run fails at `open_pr` after every stage succeeded. Delete the remote branch before re-running such an issue.
- **The SDK under-reported cost** until usage was read from the transcripts (see above). Without that fix every cost in the report would be roughly half of the true one, and the per-stage budgets would have been half as strict as they read.
- **Parallel issues conflict.** Issues #1, #2 and #4 were all planned against the same `main`, and each touched `README.md` and `tests/test_cli.py`. After #6 and #8 were merged, PR #7 conflicted. The fix was a human merge of `main` into its branch, keeping both sets of tests (34 tests green after). The factory has no rebase or revision loop; issues queued together should touch disjoint files, or be merged one at a time with the next one started after.
- **Labels are created on startup.** `watch` calls `github.ensure_labels()` when it starts, which creates `factory` and any missing `factory:*` status label, so a fresh target repo needs no manual setup and `set_status` never fails on a missing label.
