# Fork notes

This repository is a fork of Hermes Agent. `ezop` is the fork trunk: changes land there
through pull requests that pass the `Fork gate` workflow
(`.github/workflows/fork-gate.yml`). `upstream` is a separate branch and is not
part of this guidance.

## Running the bootstrap checks locally

Use an isolated `HERMES_HOME` so nothing touches an existing Hermes install.

```bash
export HERMES_HOME="$(mktemp -d)"
python -m pm.build_env --source . --out .venv --group dev --group test
export HERMES_PYTHON="$PWD/.venv/bin/python"

"$HERMES_PYTHON" scripts/check --base origin/ezop        # the static checks
scripts/run_tests.sh tests/hermes_cli/test_doctor_pm_store_probe.py   # one file; widen as needed
```

`scripts/run_tests.sh` needs `ripgrep` (`rg`) on `PATH`: file search and the doctor store
probe tests depend on it, and the hosted runner installs it explicitly. The hosted gate
runs the Python suites under `tests/agent/`, `tests/gateway/`, `tests/tools/`,
`tests/hermes_cli/`, `tests/hermes_state/` and `tests/cron/` in eight slices. Running all
of them locally is slow; prefer the files you changed.

## Coverage status

The gate is a staged bootstrap, not full qualification.

- Covered: the upstream static checks and the Python suites listed above, on Ubuntu 24.04.
- Not covered: frontend, desktop, TUI, installer, container and release lanes, other
  operating systems, and anything that needs a live provider or real service.
- Provisional: the suite is inherited from upstream and has not been reviewed for this
  fork. Any test excluded from the gate is named, with its reason, next to the exclusion
  in the workflow file.
