# Fork notes

This repository is a fork of Hermes Agent. `ezop` is the fork trunk: changes land there
through pull requests that pass the `Fork gate` workflow
(`.github/workflows/fork-gate.yml`). `upstream` is a separate branch and is not
part of this guidance.

## Upstream baseline

Upstream stable releases enter the trunk as a merge commit on the fixed `ezop` branch. The
trunk and upstream history are never rebased, squashed or rewritten: the sync branch starts
with one merge commit whose parents are the `ezop` head and the release commit, conflicts
are resolved in that commit, and each carry that needs more than a conflict resolution is
adapted or retired in its own commit after it. Only stable releases are synced, not
upstream's development branch. A sync branch contains no upstream workflow files:
`.github/workflows/` holds only `fork-*` files, which the gate's first step enforces.

Current stable baseline: upstream release `v0.23.2`, commit
`ae2a81ccc00e9b7c06301523e5bb09608397149c` (tag object
`28f57ed8e3dba4eceb67dfd3e3abda1a7604452d`).

## Carried behaviours

Behaviours this fork carries on top of upstream. At a later upstream sync, retire an entry
once its retirement condition holds, and remove only that carry's changes and tests. The
listed files are the main ones touched; many are shared with upstream code, so do not delete
them wholesale. The disposition is what the `v0.23.2` sync decided: keep (upstream lacks it),
adapt (upstream has part of it, the rest stays) or retire (upstream covers it).

- **ElevenLabs voice options.** The ElevenLabs TTS provider accepts `language_code` and
  `voice_settings` from `config.yaml`. Files: `tools/tts_tool_providers.py`,
  `hermes_cli/config_defaults.py`. Retire when upstream ElevenLabs TTS supports both options
  natively. Disposition: keep; `v0.23.2` has neither option for TTS.
- **Delegation context preamble.** `delegation.context_preamble` and
  `delegation.context_preamble_file` prepend operator-defined text to every delegated
  subagent task. Files: `tools/delegate_tool.py`, `tools/delegate_tool_progress.py`.
  Retire when upstream offers an equivalent delegation preamble setting. Disposition: adapt;
  the preamble is part of the child system prompt, which upstream now builds from
  task-independent text only (the goal and context ride in the child's first message), so
  it is passed to that builder as its own keyword.
- **Session orientation file.** A file configured in `config.yaml` is loaded once per session
  and added to the system prompt. Files: `agent/wiki_orientation.py`, `agent/system_prompt.py`,
  `hermes_cli/config_defaults.py`. Retire when upstream supports a configurable
  session-orientation file. Disposition: adapt; the profile home is bound through the scope
  API instead of a raw setter, and the fork's copy of the auxiliary defaults factory was
  dropped because upstream now ships the same one.
- **Vault login filling across open shadow roots.** Upstream now finds and fills login
  controls inside open shadow roots. What remains is keeping formless controls of different
  shadow roots in separate groups, so split one-time-code boxes of one component are never
  merged with another's. File: `agent/vault_login_classifier.py`. The browser test is an
  integration test and is skipped on the hosted gate. Retire when upstream groups formless
  shadow-root controls per root. Disposition: adapt.
- **Model-aware image credentials and explicit vision routing.** Upstream scopes a Codex
  pool cooldown to the requested model. The fork also resolves a context-variant model alias
  to its wire model before that lookup, does not fall back to the single-account token when
  the pool refused the model, honours an explicitly configured vision provider without a
  silent fallback, and gives the image provider the model it will call. The fork's
  `undici`/`zod` dependency raises are retired: upstream's workspace is at or above them.
  Files: `agent/auxiliary_client.py`, `tools/vision_tools.py`,
  `plugins/image_gen/openai-codex/`. Retire when upstream merges
  [NousResearch/hermes-agent#132164](https://github.com/NousResearch/hermes-agent/pull/132164)
  (or an equivalent). Disposition: adapt.
- **Matrix crypto ownership and one-time-key durability.** One owner serialises access to the
  Matrix crypto store, including standalone sends routed through the gateway; events whose keys
  arrive late are recovered; one-time keys are persisted before they are published, and a batch
  whose upload outcome is unknown is retired (private keys kept, IDs never published again) so
  a peer's claimed key is not handed to a second peer. Standalone
  sends do not use upstream's direct REST sender; they take the same shared-bot target rule as
  upstream's live-adapter path before they reach the owner. Files:
  `plugins/platforms/matrix/adapter_crypto*.py`, `plugins/platforms/matrix/adapter_otk.py`,
  `gateway/platform_send.py`, `gateway/outbound/senders.py`. Retire when upstream gives the
  Matrix crypto store a single owner with delayed-key recovery and durable one-time keys that
  never republish an uncertain batch's IDs (merging
  [NousResearch/hermes-agent#101220](https://github.com/NousResearch/hermes-agent/pull/101220)
  alone does not: it re-uploads such a batch; it needs the ownership and recovery pieces and
  that rule). Disposition: adapt; the sender moved with upstream's
  relocation of the outbound send code.
- **Generic `pre_stop` plugin hook.** Plugins can check every text stop before the final answer
  is accepted and either send the agent back to work or end the turn as `Unresolved`; a
  callback that raises or times out counts as `Unresolved`. While a `pre_stop` hook is registered
  the turn's text is held like a `hold_stream` transform (no stream deltas, TTS or interim
  messages, and no draft text for `on_stream_delta` / `on_stream_end` / `on_interim_message`
  observers), so a rejected draft never reaches the user: only the accepted answer or the notice
  is published. That includes budget exhaustion: with a hook registered, a turn that runs out of
  iterations before `pre_stop` accepted a draft (rejected, or sent back by an earlier gate
  before it saw it) ends with the `Unresolved` notice, never a draft or a summary. Observers get no end event of a draft; once the verdict and transforms settled
  the answer they get it as one text delta and the one `on_stream_end` carrying it. CLI voice
  mode and the TUI speak it whole, and the gateway still sends its whole-file voice reply for it.
  The prose beside a `clarify` call is the exception (that message is a tool call, never a text
  stop): it reaches the user with the prompt, as an interim message, a clarify-context message, a
  delta, or (no delta callback, the classic CLI with streaming off) a `💬` status line, and its
  row is stored visible. A `hold_stream` transform keeps holding it. Files:
  `agent/turn_stop_gates.py`, `agent/conversation_loop.py`, `agent/stream_delivery.py`,
  `hermes_cli/cli_chat_turn_mixin.py`, `gateway/run_turn.py`, `tui_gateway/prompt_turn.py`,
  `agent/turn_final_response.py`, `agent/turn_finalizer.py`, `hermes_cli/plugins_dispatch.py`.
  Retire when upstream ships an
  equivalent hook that runs on every text stop and does not fail open. Disposition: keep;
  upstream's `pre_verify` fires only after code edits.
- **Context preserved when the compression summary is skipped or fails.**
  `compression.abort_on_summary_failure` also covers a skipped summary, and compression that
  makes no progress warns. Files: `agent/context_compressor.py`,
  `agent/context_compressor_prellm.py`, `agent/conversation_compression.py`. Retire when
  upstream has the same behaviour. Disposition: keep.
- **Mattermost voice posts.** `custom_voice_message` posts are treated as voice input, and an
  audio-only `video/webm` attachment is reported as audio so speech-to-text accepts it. Their
  attachments are fetched only for a sender the gateway explicitly authorized, like any other
  attachment. File: `plugins/platforms/mattermost/adapter.py`. Retire when upstream's
  Mattermost adapter supports both. Disposition: adapt.
- **Slack proxy-password test independent of `aiohttp-socks`.** The upstream test
  `tests/gateway/outbound/test_send_message_slack.py::test_slack_dm_resolution_error_never_echoes_the_proxy_password`
  expects `InvalidURL`, which only holds without `aiohttp-socks`. The gate installs the
  `matrix` extra, which brings it in; its connector then rejects the malformed proxy URL with a
  plain `ValueError`. The product code is unaffected (the password is redacted either way), so
  the test hides `aiohttp_socks` and keeps both assertions. Retire when upstream's test no
  longer depends on that package being absent.
- **Compress-restart FIFO journey test does not assume which turn compacts.** The turn that
  trips the preflight compaction threshold depends on the system prompt's size, so on some
  hosted runners it is one turn earlier and the test's `PRESSURE_1` expectation failed. The
  test now checks that each follower request starts with the committed compacted
  transcript. File: `tests/gateway/test_journey_compress_restart_fifo.py`. Retire when
  upstream's version of the test no longer depends on the compaction turn. Disposition: keep;
  the `v0.23.2` test still assumes the turn.

## Running the gate locally

The gate (`.github/workflows/fork-gate.yml`) builds the test environment with the extras
listed under `extras:` in that file, runs the static checks, then runs the Python suites in
eight slices. Use an isolated `HERMES_HOME` so nothing touches an existing Hermes install.
The Matrix extra builds against `libolm-dev`; the file search and doctor store probe tests
need `ripgrep` (`rg`) on `PATH`, and the source-build tests need `node` and `pnpm` on `PATH`
(the runtime job installs the locked ones with `packages: pnpm` of `setup-pm`).

```bash
export HERMES_HOME="$(mktemp -d)"
python -m pm.build_env --source . --out .venv --group dev --group test \
  --extra all --extra anthropic --extra bedrock --extra mistral --extra fal \
  --extra modal --extra daytona --extra parallel-web --extra keychain \
  --extra matrix --extra messaging
export HERMES_PYTHON="$PWD/.venv/bin/python"

# the static checks, as the gate runs them (see "Constraint on the gate")
"$HERMES_PYTHON" scripts/check \
  --base "$(python3 .github/fork-gate-base.py origin/ezop origin/upstream)"

# One slice of the Python runtime job (N is 1..8); the gate runs all eight.
HERMES_TEST_SLICE=N/8 HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_TIMEOUT=900 \
  scripts/run_tests.sh tests/agent/ tests/gateway/ tests/tools/ \
    tests/hermes_cli/ tests/hermes_state/ tests/cron/ \
    tests/plugins/image_gen/test_codex_model_cooldowns.py \
    tests/plugins/image_gen/test_openai_codex_provider.py \
    tests/plugins/platforms/matrix/

# Or just the files you changed.
scripts/run_tests.sh tests/hermes_cli/test_doctor_pm_store_probe.py
```

Running all eight slices locally is slow; prefer one slice or the files you changed. If the
test list in the workflow changes, copy it from there.

## Coverage status

The gate is a staged bootstrap, not full qualification.

- Covered: the upstream static checks and the Python suites listed above, on Ubuntu 24.04.
- Not covered: frontend, desktop, TUI, installer, container and release lanes, other
  operating systems, and anything that needs a live provider or real service.
- Provisional: the suite is inherited from upstream and has not been reviewed for this
  fork. Any test excluded from the gate is named, with its reason, next to the exclusion
  in the workflow file.

## Release sync

How an upstream stable release is brought into `ezop`. This is a manual procedure for the
operator, or for a delegate the operator names. It is not an automated or agent-driven
delivery mode; agents only prepare its inputs (the candidate branch, carry dispositions and
tests). `ezop` keeps its history: nothing is rebased or force-pushed, and each release lands
as one merge commit.

### Branches

- `upstream` is the raw mirror of upstream and the review base. Only the operator moves it,
  and only to the exact commit of the release tag.
- `ezop` is the fork trunk, where the change lands.
- Nobody pushes upstream tags or any other upstream ref to the fork.
- `main` is an old, stale raw mirror of upstream that nothing uses. It is neither the
  trunk nor the release mirror: no pull request targets it and nobody moves it. An update
  tool that defaults to `main` therefore selects the wrong code (see
  [Updating the installed checkout](#updating-the-installed-checkout)).

Before moving `upstream`, read the workflows in the release tree and decide which of them
could run on the fork: their triggers, the secrets they use and the permissions they ask for.
Pushing the branch is what lets them run.

### Procedure

Set the release first. `source` below is a remote that points at the upstream repository, not
at the fork. Fetching the tag into a local ref does not publish it.

```bash
TAG=<release tag>
git fetch --no-tags source "refs/tags/$TAG:refs/tags/$TAG"
TAG_COMMIT=$(git rev-parse "$TAG^{commit}")
```

1. **Move the mirror** (operator only), after the workflow check above. This must be a
   fast-forward; if `origin` rejects it, stop and sort that out before going on.

   ```bash
   git push origin "$TAG_COMMIT:refs/heads/upstream"
   ```

2. **Make the candidate.** Branch from `ezop` and merge the tag commit with a merge commit.
   Resolve conflicts in that merge. Carry adaptations (retiring or adapting entries under
   [Carried behaviours](#carried-behaviours), and removing every upstream workflow file so
   only `fork-*` files remain) go in further commits on top. The candidate is never rebased
   or force-pushed; if `ezop` moves, merge `origin/ezop` into it.

   ```bash
   git fetch origin
   git switch -c "sync/$TAG" origin/ezop
   git merge --no-ff "$TAG_COMMIT" -m "merge: upstream $TAG"
   ```

3. **Check before every push of the candidate.** The chain stops at the first failing
   check and prints `checks passed` only if all three succeed; anything else means do not push.

   ```bash
   git fetch origin &&
   # the mirror is exactly the release tag commit
   test "$(git rev-parse origin/upstream)" = "$TAG_COMMIT" &&
   # the candidate contains the current ezop head
   git merge-base --is-ancestor origin/ezop HEAD &&
   # .github/workflows holds only fork-* files, and at least one
   files=$(git ls-tree -r --name-only HEAD .github/workflows) &&
   test -n "$files" &&
   ! printf '%s\n' "$files" | grep -qvE '^\.github/workflows/fork-[^/]+\.ya?ml$' &&
   echo "checks passed"
   ```

4. **Review.** Open the pull request against `upstream`, not `ezop`:

   ```bash
   git push origin "sync/$TAG"
   gh pr create --base upstream --head "sync/$TAG" --title "merge: upstream $TAG"
   ```

   Never arm auto-merge while the base is `upstream`: it would merge into the mirror. Wait
   for a green `gate` and a green `xreview` on the head. The review covers only
   `git diff origin/upstream...<head>`, the fork's delta; it does not re-review upstream.

5. **Land.** Re-run the checks in step 3 (fetch first). Then retarget and merge with a merge
   commit, not rebase or squash:

   ```bash
   gh pr edit <PR> --base ezop
   gh pr merge <PR> --merge
   ```

   The merge relies on the `gate` and `xreview` already green on the reviewed head; a fresh
   gate run is not needed as evidence of that tree. Retargeting does start a new `gate` run
   (the `edited` trigger), and its two diff-based guards now judge the change against `ezop`,
   skipping files identical to `upstream` (see
   [Constraint on the gate](#constraint-on-the-gate)). If that run is red, do not merge: the
   finding is in a file the fork owns. The gate is never loosened to get a landing through;
   fix it with a commit on the candidate.

   For any new review round after retargeting, whatever the reason (a red `edited` run,
   `ezop` having moved, a review finding), switch the base back first
   (`gh pr edit <PR> --base upstream`) and only then push. A pull request that has already
   merged cannot be retargeted: if the gate on `ezop` turns out red after the merge, merge
   `origin/ezop` into the candidate, fix it there, and open a new pull request against
   `upstream` (step 4).

6. **After the merge.** Compare the landed tree with the reviewed one and check the trunk
   gate:

   ```bash
   git fetch origin &&
   MERGE=$(gh pr view <PR> --json mergeCommit --jq .mergeCommit.oid) &&
   if [ "$(git rev-parse "$MERGE^{tree}")" = "$(git rev-parse "<reviewed head>^{tree}")" ]
   then echo "tree: match"; else echo "tree: MISMATCH"; false; fi &&
   gh api "repos/<owner>/<repo>/commits/$MERGE/check-runs" \
     --jq '.check_runs[] | select(.name=="gate") | .conclusion'
   ```

   `tree: MISMATCH` stops the sequence. It means `ezop` moved and the landed tree is not the
   one that was reviewed: look at what changed before anything else. Otherwise the gate on
   `ezop` must print `success`. Record the
   tag, the tag commit, the reviewed head, the merge commit, the tree comparison and the
   gate result, on the pull request or wherever the operator keeps sync records.

### Constraint on the gate

Step 5 relies on the gate result for the reviewed head, so no job or step in
`fork-gate.yml` may behave differently by base branch or event type. The result then depends
on the tree, with one deliberate exception: the two diff-based guards in the static checks
(the code-health ratchet and the blob-size check) take the pull request base, or the
previous head on a push, as the base they compare against. This keeps the fork's own
baseline enforced on ordinary pull requests. The `alarm` job is not an exception: it runs
only on push, after `gate` has finished, and cannot change `gate`'s result.

Those guards judge only what the fork owns. A file whose mode and content at the head equal
the same path in `origin/upstream` is upstream's code, so its findings never count. The step
treats every run the same way: `.github/fork-gate-base.py` builds a local commit that is the
real base except that each such file already holds its head content, so the file shows no
diff, and `scripts/check` gets that commit as `--base`. Every other file is still judged
against the real base, so growing a file the fork modified fails as before. The script reads
`origin/upstream` and stops the step if that ref is missing. Because those files come from
the head, the `ENFORCEMENT` switch and the known-environment-variable set are read from the
head's copy wherever the fork's file equals upstream's.

For a release sync that means:

- On the sync pull request (base `upstream`) they judge the fork's delta against `upstream`.
- After retargeting to `ezop` (the `edited` run) and after the merge (the push run) they
  judge the fork's delta against `ezop`. Files identical to `upstream` are skipped, so only
  fork-owned files can turn the run red.
- A red gate at either point blocks the landing or flags it, like any other red gate. Fix it
  with a commit on the candidate and review again with the base `upstream` (step 4); never
  loosen the gate.

### Not covered

- How GitHub counts an earlier check run on a head after its pull request base changes.
- A race where `ezop` moves after the last check in step 5. The tree comparison in step 6
  detects it; nothing prevents it.

## Upstream contributions

How an open upstream pull request whose head is a branch of this fork is refreshed (a new
base, review fixes). This is a manual procedure for the operator, or for a delegate the
operator names. It is not an agent delivery mode; agents only prepare its inputs (the
candidate branch, a backup branch, tests and texts).

The ordinary delivery cannot carry such a change: it needs a pull request into `ezop` with a
green `gate`, and `gate` cannot run on an upstream tree.

### Scope and refs

- Only branches that back an open upstream pull request authored by the operator, and only
  those the operator has named for this procedure. Naming a branch is what authorizes the
  rebase and force-push of that branch; the first one is `feat/elevenlabs-tts-options`
  (upstream pull request 39382). This is not a way to push to any other fork branch, and it
  never rewrites `ezop`, `upstream` or `main`.
- No pull request inside the fork carries an upstream tree: it has no `gate` (the tree holds
  upstream workflows, not `fork-*` ones), its diff would span the upstream history, and it
  would run upstream workflows in the fork.
- The candidate branch `<ISSUE-ID>-…` is an ordinary fork branch, pushed without a pull
  request. Before the first push, check that no workflow in its tree has a push trigger that
  matches the branch name.
- The upstream base commit (`BASE`) is fetched into a local ref and never pushed. That keeps
  the rule that nobody pushes upstream tags or other upstream refs to the fork: this
  procedure creates no ref that names an upstream commit; the candidate only has it as an
  ancestor.

### Procedure

`<N>` is the upstream pull request number, `<branch>` its head branch in the fork and
`<upstream URL>` the upstream repository. `BASE` and `SHA` (the candidate) are the full
SHAs recorded on the issue.

1. **Prepare the inputs** (agent). First read the head of `<branch>` and push it as the
   backup `<ISSUE-ID>-pr<N>-prev`; the candidate is built from that head, so the backup is
   what the rebase starts from, whatever happens to `<branch>` later. Fetch upstream `main`
   into a local ref and record its full SHA as `BASE`. Rebase the contribution onto `BASE`;
   fixes go in as separate commits. Run the tests for the changed area and `scripts/check`
   with a temporary `HERMES_HOME` and no credentials. Push the candidate `<ISSUE-ID>-…`. On
   the issue, record the candidate SHA, `BASE`, the backup SHA, the commands with their exit
   codes, and what the tests do not cover. The pull request description and the reply to
   reviewers are issue documents.

2. **Review** (delegate). In a clean clone, check out the exact candidate SHA detached and
   review it against `BASE` held in a local ref:

   ```bash
   git fetch origin <candidate branch>
   git fetch <upstream URL> "$BASE:refs/review/base" &&
   test "$(git rev-parse refs/review/base)" = "$BASE" &&
   git merge-base --is-ancestor refs/review/base "$SHA" &&
   git switch --detach "$SHA" &&
   laura-review --base refs/review/base "<what changed and the feedback it answers>"
   ```

   Record the verdict, the SHA and the findings on the issue. Fixes come back as new commits
   on the candidate and a new round with the new SHA, until the final SHA is approved.

3. **Publish** (delegate). Confirm that the candidate still points at the reviewed SHA and
   that the head of `<branch>` is the one the backup points at. A different head (for example
   a maintainer's commit) stops the sequence: the inputs are prepared again on the new head.

   ```bash
   git fetch origin &&
   test "$(git rev-parse origin/<candidate branch>)" = "$SHA" &&
   OLD=$(git ls-remote origin "refs/heads/<branch>" | cut -f1) &&
   test "$OLD" = "$(git rev-parse origin/<ISSUE-ID>-pr<N>-prev)" &&
   git push --force-with-lease="refs/heads/<branch>:$OLD" origin "$SHA:refs/heads/<branch>"
   ```

   The push names the SHA, not a branch, and the lease rejects it if the head moved since it
   was read.

4. **Verify** (delegate). All of these must hold, and the checks and statuses are reported as
   found: no checks, or checks waiting for a maintainer, are not a pass.

   ```bash
   git ls-remote origin "refs/heads/<branch>"       # equals $SHA
   gh pr view <N> -R <upstream repo> --json headRefOid,mergeable,statusCheckRollup
   git diff --stat "$BASE...$SHA"                    # only the contribution's files
   ```

   `headRefOid` must equal `$SHA` and `mergeable` must not be `CONFLICTING`.

5. **Texts.** The description and any reply are checked against the public boundary before
   they are published: no internal names, hosts, paths, secrets or private evidence. Who
   publishes them, and from which account, is recorded with the operator's delegation, not
   here.

### Rollback

Push the backup back to the pull request branch, with the lease on the published SHA:

```bash
git fetch origin &&
git push --force-with-lease="refs/heads/<branch>:$SHA" \
  origin "$(git rev-parse origin/<ISSUE-ID>-pr<N>-prev):refs/heads/<branch>"
```

The candidate and backup branches are deleted once the upstream pull request is closed.

## Updating the installed checkout

How a Hermes installation that runs this fork's code from a git checkout is moved to a new
`ezop` head. It is a manual procedure for the operator, or for a delegate the operator names,
run as the user that owns the installation. Agents only prepare its inputs. It is separate
from [Release sync](#release-sync): that procedure delivers a release to `ezop` in this
repository; this one changes what a host runs, and merging a release does not update any
host. Where upstream's updating guide differs (default update target, `--backup` as the
rollback source, `/snapshot restore`), this section governs for this fork.

Terms used below:

- `CHECKOUT` is the installed checkout, the directory the `hermes` launcher runs from.
- `H` is the installation's home directory for the file paths below:
  `H="${HERMES_HOME:-$HOME/.hermes}"`. Leave the `HERMES_HOME` environment variable as it is
  (normally unset) for every `hermes` command, or set it identically in every step.
- `UNITS` are the service units that run this installation and were running when the window
  started (typically a gateway and a dashboard). Use `systemctl --user` if they are user units.
- `TAG` is the release tag (`vX.Y.Z`) being installed, and `M` is the commit on the head of
  `origin/ezop` that must end up installed. After a Release sync, `M` is the merge commit it
  records.

### No routine update until qualified

Routine updates of an installed checkout are prohibited until two conditions are met: this
section is merged to `ezop`, and the practice update [P2](#p2-practice-update-without-new-code)
has run green. The prohibition ends when the operator records that both are met; merging
this section alone does not end it, and nobody else may declare it ended. Removing the
stale `main` branch ([T1](#t1-retire-the-stale-main-branch)) is not a condition: it is
optional hardening, and keeping `main` does not extend the prohibition.

Do not run a bare `hermes update`, a bare `hermes update --check` (the one exception is the
optional check at the end of T1), or `/update` in any chat platform on such an installation.
The reasons:

- A fork checkout follows the `main` update channel unless told otherwise, and the supported
  way to change the target is `--branch`. The `stable` channel reads GitHub releases of `origin`, which for this
  fork means no releases, so it fails. A bare update therefore targets `origin/main`, the stale
  mirror described under [Branches](#branches): old upstream code without the baseline release
  and without any carried behaviour. `/update` runs the same command without a branch.
- The updater's downgrade guard may refuse that move today, but only conditionally: it refuses
  when the target lacks the runtime ledger and a profile's store already holds ledger rows. It
  allows the update when it cannot decide (an unreadable store, an incomplete target, a run with
  a different `HERMES_HOME`). It is neither a guaranteed refusal nor a supported route, and
  this procedure does not rely on it.
- A bare `--check` compares `HEAD` with `origin/main` only and can print "Already up to date"
  for a checkout that a real run would move.

This is a documented prohibition, not a technical control. The only runs of this procedure
allowed before it ends are [P1](#p1-eligibility-check) and P2.

The supported update always passes `--branch ezop` explicitly. That forces the `main` channel
onto `origin/ezop`; no upstream sync runs for a branch other than `main`, and a checkout left on
a detached HEAD is switched onto `ezop` (a fast-forward when the commit is the same).

### Before you start

- Run in an ordinary shell as the installation's user, in one shell session so that the
  variables persist, with your own CLI/TUI sessions closed.
- `git -C "$CHECKOUT" status --short` must be empty, and `origin` must be this fork.
- Stopping and starting system units needs `sudo`. The update itself runs with
  `--no-gateway-restart`, so the updater stops and restarts nothing and needs no privilege.
- Check free space first (`df -h "$H"`). The backup covers most of `HERMES_HOME`
  (including large data directories and logs), its size is not predicted, and a full disk
  makes the backup fail, which the gate below catches.

### Procedure

Run each step in order and record its output. Hashes, process ids and dependency-generation
names in any earlier report are one moment's state, not constants: record what the host shows.

0. **Record the starting state.**

   ```bash
   H="${HERMES_HOME:-$HOME/.hermes}"
   PREV=$(git -C "$CHECKOUT" rev-parse HEAD); echo "$PREV"
   systemctl list-units 'hermes-*' --state=active --no-legend   # decide UNITS from this
   INSTALL_DIR=$H/installs/<id>   # the entry whose facts.json belongs to CHECKOUT
   jq -r .packages.venv.environment "$INSTALL_DIR/facts.json"   # generation selected on disk
   ls "$INSTALL_DIR/environments/"                              # generations present
   jq '{pid,code_sha}' "$H/gateway_state.json"
   jq '{commit,dirty,displayVersion,branch}' "$CHECKOUT/install-stamp.json"
   GPID=$(jq -r .pid "$H/gateway_state.json")
   grep -o "$INSTALL_DIR/environments/[^/]*" /proc/$GPID/maps | sort -u   # generation mapped
   ```

   `PREV` is the rollback point. The two generations recorded here (selected on disk, and
   mapped by the running gateway) can differ, because a running process keeps what it imported
   at its start; they are the "generations before the update" for step 6. Record them as
   found; nothing is compared with hashes quoted in earlier reports.

1. **Fetch the release tag locally** (new release only; skip it for P2). The fork carries no
   release tags, and without the tag the displayed version stays at the old base and plugins
   that require a newer Hermes may not load. The tag is local and never pushed.

   ```bash
   git -C "$CHECKOUT" fetch --no-tags https://github.com/NousResearch/hermes-agent.git \
     "refs/tags/$TAG:refs/tags/$TAG"
   ```

2. **Preview.**

   ```bash
   hermes update --check --branch ezop
   test "$(git -C "$CHECKOUT" rev-parse origin/ezop)" = "$M" && echo "origin/ezop = M"
   ```

   For a new release it must report an update and `origin/ezop` must be `M`. For P2 it must
   match the class P1 recorded, and `origin/ezop` must be `M` (see P2). Anything else: stop.

3. **Stop the services**, then note the time.

   ```bash
   sudo systemctl stop $UNITS
   START=$(date '+%F %T')
   ```

   3b. **Take the backup (K1 gate).** A separate native backup, not `hermes update --backup`.
   The update's own `--backup` is best effort: a failed backup never stops the update, it is
   not verified after writing, and the update's receipt can report success without a usable
   archive. Here the backup's own exit status decides, then the archive is tested.

   ```bash
   Z="$H/backups/hermes-backup-$(date +%F-%H%M%S).zip"
   hermes backup -o "$Z"; B=$?; echo "backup exit=$B"
   unzip -tq "$Z";        U=$?; echo "unzip exit=$U"
   [ "$B" -eq 0 ] && [ "$U" -eq 0 ] && echo "K1 green" || echo "K1 RED: do not update"
   ```

   `hermes backup` exits 1 when the archive is incomplete and 2 when another backup holds the
   lock. `unzip -tq` checks that the file is a readable zip and that every member present
   matches its stored checksum. It does not prove the archive holds everything it should
   (that is the backup's exit status), that the SQLite databases inside are consistent, or
   that a restore will work; `hermes import` re-checks the checksums when it restores and is
   the real test. Do not substitute `python3 -m zipfile -t`: it exits 0 even for a damaged
   member.

   If either exit status is not 0, **do not update**. Start the services again
   (`sudo systemctl start $UNITS`), record the result, and keep `$Z` (or the
   `*.incomplete.zip`) for diagnosis.

   Backups made by hand are not pruned automatically; the operator removes old ones.
   With `updates.pre_update_backup` off, the update takes no quick state snapshots either.
   They are a subset of this full backup, so restoring is by `hermes import` of `$Z`, not by
   `/snapshot restore`.

4. **Update.**

   ```bash
   hermes update --branch ezop --no-gateway-restart; echo "exit=$?"
   ```

   Exit status other than 0: do not start the services; go to [Rollback](#rollback).

5. **Start the services.**

   ```bash
   sudo systemctl start $UNITS
   ```

6. **Verify.** All of these must hold:

   - `git -C "$CHECKOUT" rev-parse HEAD` is `M`, and `git -C "$CHECKOUT" symbolic-ref --short HEAD`
     is `ezop`.
   - `jq '{commit,dirty,displayVersion,branch}' "$CHECKOUT/install-stamp.json"`: `commit` is
     `M`, `dirty` is `false`, `branch` is `ezop`, and `displayVersion` starts with the version
     of `TAG`.
   - `systemctl is-active $UNITS` prints `active` for every unit.
   - `code_sha` in `$H/gateway_state.json` is `M` and its `pid` differs from step 0.
   - `journalctl -u <each unit> --since "$START" -p err` shows no entries.

   Also record, without pass or fail, the same generation facts as step 0: the generation
   selected on disk (`jq -r .packages.venv.environment "$INSTALL_DIR/facts.json"`),
   `ls "$INSTALL_DIR/environments/"`, and the generation the new gateway process maps (the
   `grep` on `/proc/<new pid>/maps`), and which generations from step 0 are gone. That is the
   only evidence of whether the generation from before the update survived.

   Every point green is a green run. Any red point: [Rollback](#rollback).

### Rollback

State of knowledge: this rollback is derived from the source and has not been exercised on a
real installation.

- **Needs:** `PREV` from step 0 and the green K1 archive `$Z` from the same window. Without
  both, do not start an update.
- **Data-loss boundary:** the services were stopped from step 3, so restoring `$Z` returns
  the home to its state at step 3b. What is lost is what was written after the services
  were started in step 5, such as messages and sessions from that period, and anything else
  written into `HERMES_HOME` since.
- **Always restore `$Z` when rolling back in the same window.** The update's completion step
  and the first start may have migrated data that the old code cannot read.
- **Dependencies:** the previous dependency generation is retained only if the updater's
  collector left it. It removes generations that are not selected, are older than a day and
  hold no lease, and nobody has shown that the pre-update generation survives a real update.
  If it is gone, the completion step in item 3 below rebuilds it and needs network access.
- **Fallback:** while the operator has not retired it, the fallback checkout kept beside the
  installed one since activation, with its launchers, restores the old code without any
  dependency rebuild. It restores code only, not data.

Order matters, because `hermes import` refuses to run while a gateway runs and can itself
start an existing gateway service when it finishes, whether or not the restore succeeded
(whether it can start a system unit without privilege is unchecked):

1. `sudo systemctl stop $UNITS`
2. `git -C "$CHECKOUT" checkout --detach "$PREV"`
3. Re-run the install completion at that commit with the managed interpreter the launcher uses
   (`PY`; its path changes when the managed tools are upgraded):
   `"$PY" -I "$CHECKOUT/hermes_cli/source_completion.py" --source "$CHECKOUT"`
4. `hermes import --force "$Z"`. Without `--force` it asks "Continue? [y/N]", and answering
   anything else (or just pressing Enter) prints "Aborted." and still exits 0, so the exit
   status alone does not show a restore. Success is exit status 0 **and** the final line
   `Done. Your Hermes configuration has been restored.`. It restores data and configuration
   from the archive; it does not restore the code (steps 2 and 3 set that), a checkout
   directory named `hermes-agent`, or the dependency environments, which the archive
   excludes. A checkout stored under another name inside `H` is included without its `.git`
   and is restored to its state at step 3b, which must again match `PREV` (step 5 checks
   this). If it suggests running `hermes gateway run`, do not.
5. `sudo systemctl start $UNITS` (a no-op if import already started it), then check, with
   `PREV` and the step 0 records in place of `M` and the new version:
   - `systemctl is-active $UNITS` prints `active` for each unit;
   - `git -C "$CHECKOUT" status --short` is empty and `rev-parse HEAD` is `PREV`. The
     checkout is on a detached HEAD, so the `ezop` branch checks of step 6 do not apply; the
     next update with `--branch ezop` switches it back to `ezop`;
   - `jq '{commit,dirty,displayVersion}' "$CHECKOUT/install-stamp.json"` shows `commit` equal
     to `PREV`, `dirty` false, and `displayVersion` as recorded in step 0 (the `branch` field
     is recorded, not judged);
   - `code_sha` in `$H/gateway_state.json` is `PREV` and its `pid` differs from the one
     recorded in step 0;
   - `journalctl -u <each unit> --since "<time of rollback step 1>" -p err` shows no entries.

If `hermes import` fails, prints "Aborted.", or does not print the final line, stop the
services again (`sudo systemctl stop $UNITS`): import tries to start an existing gateway
service even when the restore is incomplete, so one may be running on a half-restored home.
Leave them stopped, keep `$Z`, and escalate to the operator.

### P1: eligibility check

A check that interrupts nothing and can be run any time. It confirms that the installed
checkout can reach the fork and classifies how far it is from `origin/ezop`. It writes only
remote-tracking refs and tags in `CHECKOUT/.git` and logs under `H`.

Inputs: `CHECKOUT`, and `hermes` from `PATH` resolving to that checkout.

```bash
git -C "$CHECKOUT" remote get-url origin       # this fork
git -C "$CHECKOUT" status --short              # empty
hermes update --check --branch ezop; echo "exit=$?"
git -C "$CHECKOUT" rev-parse HEAD origin/ezop
git -C "$CHECKOUT" merge-base --is-ancestor HEAD origin/ezop && echo "HEAD is behind or equal"
git -C "$CHECKOUT" diff --name-only HEAD origin/ezop        # what an update would change
```

Pass: the remote is this fork, the tree is clean, the check exits 0, and `HEAD` is an
ancestor of `origin/ezop`. Any error, a diverged `HEAD` or other output is a fail: stop and
record it. A pass classifies the checkout as one of:

- **Current:** "Already up to date." and `HEAD` equals `origin/ezop`. P2 is a no-new-code run.
- **Behind by documentation only:** an update is available and every changed path is a
  documentation file (`*.md`). P2 is still a no-new-code run, but it will fetch those commits.
- **Behind by anything else:** code, dependencies or workflows changed. That is a real update
  (follow the procedure above with `M` and, for a release, `TAG`), not a P2, and it is the
  operator's call.

P1 only reads metadata. It does not exercise the backup, the completion step, migrations, a
restart or the downgrade guard.

### P2: practice update without new code

One real update with no new code, in a window that the operator names separately (it is not
implied by P1 or by merging this section). Services are down for the full backup, the
completion step and the restart; their length on this installation is unknown, so agree a
generous window.

Inputs: a P1 pass, repeated right before the window, that classified the checkout as
Current or Behind by documentation only; `M` is the commit `origin/ezop` points to after that
check; `TAG` is the release tag of the baseline named under
[Upstream baseline](#upstream-baseline). If `origin/ezop` has moved to anything other than
documentation since, stop and refer it to the operator: it is no longer a no-new-code run.
Merging this section itself puts the installed checkout in the second class, so this is the
expected case.

Run steps 0, 2, 3, 3b, 4, 5 and 6 above. Skip step 1 (the tag is already present: check
`git -C "$CHECKOUT" tag -l "$TAG"`). Step 2 must print "Already up to date." when the
checkout is Current, and "Update available" when it is behind by documentation only.

Green: K1 green (both exits 0), step 4 exit 0, every point of step 6 green with `M` as above,
and the stamp's `branch` is `ezop`. Red: any other result; roll back and record it. Record
the outputs of steps 0, 3b, 4 and 6, the archive size and the backup time, wherever the
operator keeps sync records. A green P2 together with this section merged is what lets the
operator end the prohibition.

What P2 exercises: the K1 gate; the switch from a detached HEAD onto `ezop` (at the same
commit when Current, or after fetching documentation-only commits); the whole installation
completion step with no "already up to date" shortcut (dependency sync, the generation
collector, launchers, frontend dependencies, data migrations and maintenance, the stamp
written with the branch). What it does not: updater-driven service pause
(`--no-gateway-restart` skips it), a code update, building a new dependency generation,
whether the pre-update generation is retained once a new one replaces it, migrations of new
code, rollback, or the downgrade guard. The selected dependency generation can also change
without any update (a plugin may install an extra on first use), so the restart in step 5 may
be the first start on a newer generation that was already committed. If step 6 turns red, look
in the journal for import errors before blaming the update, and compare against the
generations recorded in steps 0 and 6.

### T1: retire the stale `main` branch

Optional hardening for the repository administrator (the operator, or an administrator the
operator names), never an agent: it changes branch protection. With `main` gone, a bare
`hermes update` or `/update` fails while resolving the channel or fetching `origin/main`,
before the updater stops anything, whatever the state of the local stores. That follows from the source and has not
been exercised. It is not a condition for ending the prohibition above.

Nothing is lost: all of `main` is contained in `ezop` and in `upstream`, which step 0
re-checks. The branch can be recreated at the archived commit (see
[Restoring `main`](#restoring-main)).

Mechanics (re-read at execution, not assumed): `main` is protected by repository rulesets
whose `conditions.ref_name.include` lists `refs/heads/main` literally next to
`refs/heads/upstream`: one carries the `deletion` and `non_fast_forward` rules, another the
`update` rule. The `ezop` trunk has its own separate ruleset (pull request, `gate` and
`xreview`, deletion and non-fast-forward protection). The adjustment is to remove the single
string `refs/heads/main` from the include list of the rulesets that block the deletion, and
nothing else. Never delete, disable or loosen a ruleset, change a rule, an enforcement mode or
a bypass list, or touch `upstream` or `ezop`. Run it with administrator credentials; the
commands below are examples with `R=<owner>/<repo>`.

0. **Guards.** Every one must hold, or stop.

   ```bash
   git fetch origin
   gh api repos/$R --jq .default_branch                    # ezop
   MAIN_OID=$(gh api repos/$R/git/ref/heads/main --jq .object.sha); echo "$MAIN_OID"
   git merge-base --is-ancestor "$MAIN_OID" origin/ezop     && echo "in ezop"
   git merge-base --is-ancestor "$MAIN_OID" origin/upstream && echo "in upstream"
   gh pr list -R $R --base main --state open                # no output
   ```

1. **Archive the preimages** in a directory outside the repository.

   ```bash
   gh api repos/$R/git/ref/heads/main > main-ref.json
   for b in main ezop upstream; do gh api repos/$R/rules/branches/$b > rules-on-$b.json; done
   for id in $(gh api repos/$R/rulesets --jq '.[].id'); do
     gh api repos/$R/rulesets/$id > ruleset-$id.json; done
   gh api repos/$R/branches/main/protection > classic-protection.json   # 404 = none
   ```

   Which rulesets matter is decided by `rules-on-main.json` (its `ruleset_id` values), not by
   name. Stop and escalate instead of editing if a ruleset covers `main` by a pattern,
   `~ALL`, `~DEFAULT_BRANCH` or an exclude list rather than the literal `refs/heads/main`, if
   removing the string would leave an include list empty, or if classic branch protection
   exists on `main`: each of those is a wider change than this procedure covers. The preimage
   JSON, including any bypass list, is the source for every payload below.

2. **Remove `main` from the ruleset that carries `deletion`.** Build the payload from the
   archived preimage:

   ```bash
   ID=<ruleset id>
   jq '{name,target,enforcement,bypass_actors,conditions,rules}
       | with_entries(select(.value != null))
       | .conditions.ref_name.include -= ["refs/heads/main"]' ruleset-$ID.json > edit-$ID.json
   diff <(jq -S .conditions ruleset-$ID.json) <(jq -S .conditions edit-$ID.json)  # only that string
   gh api -X PUT repos/$R/rulesets/$ID --input edit-$ID.json
   ```

3. **Delete the branch.**

   ```bash
   gh api -X DELETE repos/$R/git/refs/heads/main
   ```

   If GitHub refuses because of the `update` rule, repeat step 2 for the ruleset that carries
   it, then delete again. Do not edit any ruleset that does not appear in `rules-on-main.json`.

4. **Verify.**

   ```bash
   gh api repos/$R/branches/main            # 404
   gh api repos/$R --jq .default_branch     # ezop
   git ls-remote origin refs/heads/main     # no output
   ```

   `ezop` and `upstream` must be unchanged apart from legitimate pull request merges into
   `ezop`; `rules-on-ezop.json` and `rules-on-upstream.json` re-read now must match the
   archived ones.

5. **Put the protections back.** Re-apply the archived preimage of every ruleset edited in
   steps 2-3 (the same `jq` filter without the `-=` line, `PUT` to the same id) and compare:
   `jq -S 'del(.updated_at)'` of a fresh read must equal the archive. If step 3 failed for any
   other reason, do this at once and stop; nothing else has changed.

Optional check on the installed checkout, no restart: a bare `hermes update --check` (the exception to
the prohibition above; it changes nothing) should now fail on the missing `main` instead of
printing "Already up to date." (source reading; not exercised). If it does neither, record it.

#### Restoring `main`

Recreate the branch at the archived commit and put the protections back:

```bash
gh api -X POST repos/$R/git/refs -f ref=refs/heads/main -f sha="$MAIN_OID"
```

If a ruleset refuses the creation, repeat step 2 for it first. Then re-apply the archived
preimage as in step 5 and verify `git ls-remote origin refs/heads/main` prints `$MAIN_OID`.
