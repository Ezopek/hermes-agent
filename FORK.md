# Fork notes

This repository is a fork of Hermes Agent. `ezop` is the fork trunk: changes land there
through pull requests that pass the `Fork gate` workflow
(`.github/workflows/fork-gate.yml`). `upstream` is a separate branch and is not
part of this guidance.

## Release sync

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
  arrive late are recovered; one-time keys are persisted before they are published. Standalone
  sends do not use upstream's direct REST sender; they take the same shared-bot target rule as
  upstream's live-adapter path before they reach the owner. Files:
  `plugins/platforms/matrix/adapter_crypto*.py`, `plugins/platforms/matrix/adapter_otk.py`,
  `gateway/platform_send.py`, `gateway/outbound/senders.py`. Retire when upstream gives the
  Matrix crypto store a single owner with delayed-key recovery and durable one-time keys (for
  example by merging
  [NousResearch/hermes-agent#101220](https://github.com/NousResearch/hermes-agent/pull/101220)
  plus the ownership and recovery pieces). Disposition: adapt; the sender moved with upstream's
  relocation of the outbound send code.
- **Generic `pre_stop` plugin hook.** Plugins can check every text stop before the final answer
  is accepted and either send the agent back to work or end the turn as `Unresolved`; a
  callback that raises or times out counts as `Unresolved`. Files: `agent/turn_stop_gates.py`, `agent/turn_final_response.py`,
  `agent/turn_finalizer.py`, `hermes_cli/plugins_dispatch.py`. Retire when upstream ships an
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

## Running the gate locally

The gate (`.github/workflows/fork-gate.yml`) builds the test environment with the extras
listed under `extras:` in that file, runs the static checks, then runs the Python suites in
eight slices. Use an isolated `HERMES_HOME` so nothing touches an existing Hermes install.
The Matrix extra builds against `libolm-dev`; the file search and doctor store probe tests
need `ripgrep` (`rg`) on `PATH`.

```bash
export HERMES_HOME="$(mktemp -d)"
python -m pm.build_env --source . --out .venv --group dev --group test \
  --extra all --extra anthropic --extra bedrock --extra mistral --extra fal \
  --extra modal --extra daytona --extra parallel-web --extra keychain \
  --extra matrix --extra messaging
export HERMES_PYTHON="$PWD/.venv/bin/python"

"$HERMES_PYTHON" scripts/check --base origin/ezop        # the static checks

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
- Release syncs: the gate's static job compares against the pull request's base, the
  previous trunk, so a sync that brings in many upstream commits reports findings that
  already exist in the release. The sync itself must pass
  `scripts/check --base <release commit>`; the findings against the old trunk are listed
  in the sync pull request, each shown to sit in a file that is identical to the release.
