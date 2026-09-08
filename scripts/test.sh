#!/usr/bin/env bash
set -euo pipefail

readonly CORE_COMMIT="fc034572d0216a04ed40a07154394908a594dfed"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
core_dir="${repo_root}/.dev/home-assistant-core"
python_bin="${repo_root}/.venv/bin/python"
ruff_bin="${repo_root}/.venv/bin/ruff"
test_dir="${core_dir}/tests/components/hubinet_ops"

if [[ ! -x "${python_bin}" || ! -x "${ruff_bin}" ]]; then
  echo "Development environment is missing; run scripts/bootstrap-dev.sh" >&2
  exit 1
fi
if [[ "$(git -C "${core_dir}" rev-parse HEAD 2>/dev/null)" != "${CORE_COMMIT}" ]]; then
  echo "Home Assistant test harness is not pinned to ${CORE_COMMIT}" >&2
  exit 1
fi
if [[ ! -L "${test_dir}" ]]; then
  echo "Hubinet-Ops test harness link is missing; rerun bootstrap" >&2
  exit 1
fi

"${ruff_bin}" check --config "${core_dir}/pyproject.toml" \
  "${repo_root}/custom_components/hubinet_ops" \
  "${repo_root}/deploy/hubinet-package-scan-helper.py"

cd "${core_dir}"
PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}" \
  "${python_bin}" -m pytest -c pyproject.toml \
  tests/components/hubinet_ops "$@"
