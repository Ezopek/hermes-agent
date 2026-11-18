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
  is published. Observers get that answer, once the verdict and transforms settled it, as one
  text delta and an `on_stream_end` carrying it; CLI voice mode speaks it whole, and the gateway
  still sends its whole-file voice reply for it. The prose beside a `clarify` call is the
  exception (that message is a tool call, never a text stop): it reaches the user with the
  prompt, as an interim message, a clarify-context message, a delta, or (no delta callback, the
  classic CLI with streaming off) a `💬` status line. A `hold_stream` transform keeps holding
  it. Files: `agent/turn_stop_gates.py`, `agent/conversation_loop.py`,
  `agent/stream_delivery.py`, `hermes_cli/cli_chat_turn_mixin.py`, `gateway/run_turn.py`,
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
