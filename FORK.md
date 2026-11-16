# Fork notes

This repository is a fork of Hermes Agent. `ezop` is the fork trunk: changes land there
through pull requests that pass the `Fork gate` workflow
(`.github/workflows/fork-gate.yml`). `upstream` is a separate branch and is not
part of this guidance.

## Carried behaviours

Behaviours this fork carries on top of upstream. At a later upstream sync, retire an entry
once its retirement condition holds and drop the carried files and tests with it.

- **ElevenLabs voice options.** The ElevenLabs TTS provider accepts `language_code` and
  `voice_settings` from `config.yaml`. Files: `tools/tts_tool_providers.py`,
  `hermes_cli/config_defaults.py`. Retire when upstream ElevenLabs TTS supports both options
  natively.
- **Delegation context preamble.** `delegation.context_preamble` and
  `delegation.context_preamble_file` prepend operator-defined text to every delegated
  subagent task. Files: `tools/delegate_tool.py`, `tools/delegate_tool_progress.py`.
  Retire when upstream offers an equivalent delegation preamble setting.
- **Session orientation file.** A file configured in `config.yaml` is loaded once per session
  and added to the system prompt. Files: `agent/wiki_orientation.py`, `agent/system_prompt.py`,
  `hermes_cli/config_defaults_aux.py`. Retire when upstream supports a configurable
  session-orientation file.
- **Vault login filling across open shadow roots.** Login-field detection and filling also
  reaches controls inside open shadow roots. File: `agent/vault_login_classifier.py`. The
  browser test is an integration test and is skipped on the hosted gate. Retire when upstream
  handles open shadow roots in vault login inspection and filling.
- **Model-aware image credentials and explicit vision routing.** Codex credentials are looked
  up per model, and an explicitly configured vision provider is honoured without a silent
  fallback. The root and TUI `package.json` also raise `undici` and `zod` above versions with
  published advisories. Files: `agent/auxiliary_client.py`, `tools/vision_tools.py`,
  `plugins/image_gen/openai-codex/`, `package.json`, `ui-tui/package.json`. Retire when
  upstream merges
  [NousResearch/hermes-agent#132164](https://github.com/NousResearch/hermes-agent/pull/132164)
  (or an equivalent) and raises `undici` and `zod` itself.
- **Matrix crypto ownership and one-time-key durability.** One owner serialises access to the
  Matrix crypto store, including standalone sends routed through the gateway; events whose keys
  arrive late are recovered; one-time keys are persisted before they are published. Files:
  `plugins/platforms/matrix/adapter_crypto*.py`, `plugins/platforms/matrix/adapter_otk.py`,
  `gateway/platform_send.py`, `tools/send_message_senders.py`. Retire when upstream gives the
  Matrix crypto store a single owner with delayed-key recovery and durable one-time keys (for
  example by merging
  [NousResearch/hermes-agent#101220](https://github.com/NousResearch/hermes-agent/pull/101220)
  plus the ownership and recovery pieces).
- **Generic `pre_stop` plugin hook.** Plugins can check every text stop before the final answer
  is accepted and either send the agent back to work or end the turn as `Unresolved`; a
  callback that raises or times out counts as `Unresolved`. Files: `agent/turn_stop_gates.py`, `agent/turn_final_response.py`,
  `agent/turn_finalizer.py`, `hermes_cli/plugins_dispatch.py`. Retire when upstream ships an
  equivalent hook that runs on every text stop and does not fail open.
- **Context preserved when the compression summary is skipped or fails.**
  `compression.abort_on_summary_failure` also covers a skipped summary, and compression that
  makes no progress warns. Files: `agent/context_compressor.py`,
  `agent/context_compressor_prellm.py`, `agent/conversation_compression.py`. Retire when
  upstream has the same behaviour.
- **Mattermost voice posts.** `custom_voice_message` posts are treated as voice input, and an
  audio-only `video/webm` attachment is reported as audio so speech-to-text accepts it. File:
  `plugins/platforms/mattermost/adapter.py`. Retire when upstream's Mattermost adapter
  supports both.

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
