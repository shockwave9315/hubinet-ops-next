#!/usr/bin/env bash
set -euo pipefail

readonly CORE_REPOSITORY="https://github.com/home-assistant/core.git"
readonly CORE_COMMIT="fc034572d0216a04ed40a07154394908a594dfed"
readonly MINIMUM_PYTHON="3.14.2"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
core_dir="${repo_root}/.dev/home-assistant-core"
venv_dir="${repo_root}/.venv"
python_bin="${PYTHON_BIN:-python3.14}"

for command in git uv "${python_bin}"; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Required command not found: ${command}" >&2
    exit 1
  fi
done

"${python_bin}" - "${MINIMUM_PYTHON}" <<'PY'
import sys

minimum = tuple(map(int, sys.argv[1].split(".")))
if sys.version_info[:3] < minimum:
    raise SystemExit(
        f"Python {sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro} is too old; need >= {sys.argv[1]}"
    )
PY

mkdir -p "${repo_root}/.dev"
if [[ ! -d "${core_dir}/.git" ]]; then
  if [[ -e "${core_dir}" ]]; then
    echo "Refusing to replace non-Git path: ${core_dir}" >&2
    exit 1
  fi
  git init "${core_dir}"
  git -C "${core_dir}" remote add origin "${CORE_REPOSITORY}"
fi

if [[ "$(git -C "${core_dir}" remote get-url origin)" != "${CORE_REPOSITORY}" ]]; then
  echo "Unexpected Home Assistant origin in ${core_dir}" >&2
  exit 1
fi
if ! git -C "${core_dir}" diff --quiet || \
  ! git -C "${core_dir}" diff --cached --quiet; then
  echo "Refusing to alter a modified Home Assistant checkout" >&2
  exit 1
fi
if ! git -C "${core_dir}" cat-file -e "${CORE_COMMIT}^{commit}" 2>/dev/null; then
  git -C "${core_dir}" fetch --depth=1 origin "${CORE_COMMIT}"
fi
git -C "${core_dir}" checkout --detach --quiet "${CORE_COMMIT}"

if [[ "$(git -C "${core_dir}" rev-parse HEAD)" != "${CORE_COMMIT}" ]]; then
  echo "Home Assistant checkout did not resolve to ${CORE_COMMIT}" >&2
  exit 1
fi

test_link="${core_dir}/tests/components/hubinet_ops"
if [[ -e "${test_link}" && ! -L "${test_link}" ]]; then
  echo "Refusing to replace existing test path: ${test_link}" >&2
  exit 1
fi
ln -sfn "${repo_root}/tests/components/hubinet_ops" "${test_link}"

uv_version="$(sed -n 's/^uv==//p' "${core_dir}/requirements.txt")"
if [[ -z "${uv_version}" ]]; then
  echo "Could not read the pinned uv version from HA requirements.txt" >&2
  exit 1
fi
uv_command=(uv tool run --from "uv==${uv_version}" uv)

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  "${uv_command[@]}" venv "${venv_dir}" --python "${python_bin}"
fi

if [[ "$("${venv_dir}/bin/python" -c 'import sys; print(sys.version_info >= (3, 14, 2))')" != "True" ]]; then
  echo "Existing .venv uses unsupported Python; remove it and rerun bootstrap" >&2
  exit 1
fi

(
  cd "${core_dir}"
  "${uv_command[@]}" pip install --python "${venv_dir}/bin/python" \
    -r requirements.txt
  "${uv_command[@]}" pip install --python "${venv_dir}/bin/python" \
    -r requirements_test.txt
  "${uv_command[@]}" pip install --python "${venv_dir}/bin/python" \
    -e . --config-settings editable_mode=compat
  PATH="${venv_dir}/bin:${PATH}" "${venv_dir}/bin/python" \
    -m script.install_integration_requirements hassio mqtt proxmoxve
  "${uv_command[@]}" pip install --python "${venv_dir}/bin/python" \
    "asyncssh==2.21.0"
  "${venv_dir}/bin/python" -m script.translations develop --all
)

echo "Development environment ready. Run scripts/test.sh"
