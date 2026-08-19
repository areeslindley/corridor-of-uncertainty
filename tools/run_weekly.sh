#!/usr/bin/env bash
#
# Corridor of Uncertainty — cron/systemd wrapper for the weekly site update.
#
# cron runs with a near-empty environment: PATH is usually just /usr/bin:/bin,
# there is no SSH agent, no locale, and no shell profile is sourced. Every one
# of those has to be re-established explicitly here, which is why the job is not
# invoked directly from the crontab line.
#
# Usage:
#   tools/run_weekly.sh                # normal weekly run
#   tools/run_weekly.sh --backfill     # extra args pass straight through
#
set -Eeuo pipefail

# --- locate the repository ------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- environment ----------------------------------------------------------
# Secrets and configuration live outside the repository. Keep this file at
# mode 600: it holds an Anthropic key, a YouTube key and a Gmail app password.
ENV_FILE="${COU_ENV_FILE:-${HOME}/.config/corridor-of-uncertainty/cou.env}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
else
  echo "FATAL: environment file not found: ${ENV_FILE}" >&2
  echo "Copy tools/cou.env.example there and fill it in." >&2
  exit 2
fi

# cron's PATH will not include /usr/local/bin (quarto, gh) or ~/.local/bin.
export PATH="${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# Quarto and Python both misbehave under the POSIX locale when episode titles
# contain the em-dashes and arrows used in the page template.
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1

# Non-interactive git: never prompt for credentials, and use the deploy key
# explicitly since there is no ssh-agent under cron.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i ${HOME}/.ssh/cou_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"

# --- python environment ---------------------------------------------------
VENV="${COU_VENV:-${REPO_ROOT}/.venv}"
if [[ -x "${VENV}/bin/python" ]]; then
  PYTHON="${VENV}/bin/python"
else
  PYTHON="$(command -v python3)"
  echo "WARNING: no virtualenv at ${VENV}; falling back to ${PYTHON}" >&2
fi

# --- run ------------------------------------------------------------------
exec "${PYTHON}" "${REPO_ROOT}/tools/weekly_update.py" "$@"
