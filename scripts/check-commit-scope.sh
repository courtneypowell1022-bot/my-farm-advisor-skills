#!/usr/bin/env bash
#
# Audit a commit's file scope before it leaves the machine.
#
# Runtime datasets (boundaries, weather, CDL, rasters, generated maps) belong
# under DATA_PIPELINE_DATA_ROOT, never in Git. This guard inspects the staged
# index by default so pre-existing tracked assets from upstream cannot cause a
# false failure. Use --range to audit an arbitrary revision range instead.
#
# Usage:
#   scripts/check-commit-scope.sh                 # staged files
#   scripts/check-commit-scope.sh --staged        # staged files (explicit)
#   scripts/check-commit-scope.sh --range A..B    # files changed in a range
#   scripts/check-commit-scope.sh --quiet         # only print failures
#
# Exit status: 0 when the scope is clean, 1 when any rule is violated.
#
# Limits can be overridden for local experiments:
#   COMMIT_SCOPE_MAX_FILE_BYTES   (default 102400 = 100 KB)
#   COMMIT_SCOPE_MAX_TOTAL_BYTES  (default 1048576 = 1 MB)
# A value of 0 disables that limit.

set -u -o pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_ROOT=$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)

MODE=staged
RANGE=''
QUIET=0

MAX_FILE_BYTES=${COMMIT_SCOPE_MAX_FILE_BYTES:-102400}
MAX_TOTAL_BYTES=${COMMIT_SCOPE_MAX_TOTAL_BYTES:-1048576}

usage() {
  cat <<'EOF'
Usage: check-commit-scope.sh [--staged | --range <rev-range>] [--quiet]

  --staged          Audit files staged for commit (default).
  --range RANGE     Audit files added or modified in a git revision range,
                    for example origin/main..HEAD.
  --quiet, -q       Suppress per-rule PASS lines; still prints FAIL lines.
  --help, -h        Show this message.
EOF
}

while (($#)); do
  case "$1" in
    --staged)
      MODE=staged
      shift
      ;;
    --range)
      if (($# < 2)) || [[ -z "${2:-}" ]]; then
        printf 'FAIL: --range requires a revision range such as origin/main..HEAD\n' >&2
        exit 2
      fi
      MODE=range
      RANGE=$2
      shift 2
      ;;
    --quiet|-q)
      QUIET=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'FAIL: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! git -C "${DEFAULT_ROOT}" rev-parse --show-toplevel >/dev/null 2>&1; then
  printf 'FAIL: not inside a git worktree (%s)\n' "${DEFAULT_ROOT}" >&2
  exit 1
fi

REPO_ROOT=$(git -C "${DEFAULT_ROOT}" rev-parse --show-toplevel)
cd "${REPO_ROOT}" || {
  printf 'FAIL: could not enter repo root %s\n' "${REPO_ROOT}" >&2
  exit 1
}

if [[ "${MODE}" == range ]]; then
  # Split the range so each endpoint is validated on its own.
  if [[ "${RANGE}" == *...* ]]; then
    RANGE_START=${RANGE%%...*}
    RANGE_END=${RANGE#*...}
  elif [[ "${RANGE}" == *..* ]]; then
    RANGE_START=${RANGE%%..*}
    RANGE_END=${RANGE#*..}
  else
    RANGE_START=''
    RANGE_END=${RANGE}
  fi
  RANGE_END=${RANGE_END:-HEAD}
  if ! git rev-parse --verify --quiet "${RANGE_END}^{commit}" >/dev/null 2>&1; then
    printf 'FAIL: cannot resolve revision range end: %s\n' "${RANGE}" >&2
    exit 2
  fi
  if [[ -n "${RANGE_START}" ]] &&
     ! git rev-parse --verify --quiet "${RANGE_START}^{commit}" >/dev/null 2>&1; then
    printf 'FAIL: cannot resolve revision range start: %s\n' "${RANGE}" >&2
    exit 2
  fi
fi

PATHS=()
if [[ "${MODE}" == staged ]]; then
  while IFS= read -r -d '' path; do
    PATHS+=("${path}")
  done < <(git diff --cached --name-only --diff-filter=ACMR -z)
elif [[ -n "${RANGE_START}" ]]; then
  while IFS= read -r -d '' path; do
    PATHS+=("${path}")
  done < <(git diff --name-only --diff-filter=ACMR -z "${RANGE}")
else
  # Single revision: audit the files that commit itself introduced or changed.
  while IFS= read -r -d '' path; do
    PATHS+=("${path}")
  done < <(git diff-tree --root --no-commit-id --name-only --diff-filter=ACMR -r -z "${RANGE_END}")
fi

blob_size() {
  local path=$1
  if [[ "${MODE}" == staged ]]; then
    git cat-file -s ":${path}" 2>/dev/null
  else
    git cat-file -s "${RANGE_END}:${path}" 2>/dev/null
  fi
}

# Runtime directories: everything the pipeline generates under the data root.
RUNTIME_DIR_PATTERNS=(
  '(^|/)growers/'
  '(^|/)derived/'
  '(^|/)logs/'
  '(^|/)manifests/'
  '(^|/)web-maps/'
  '(^|/)\.venv/'
  '(^|/)node_modules/'
  '(^|/)\.cache/'
  '(^|/)shared/(cdl|weather|osm)/'
)

# Runtime files that live outside the directories above.
RUNTIME_FILE_PATTERNS=(
  '(^|/)boundary/field_boundaries\.geojson$'
  '(^|/)daily_weather\.csv$'
  '(^|/)field_boundary\.geojson$'
  '^data/'
)

# Generated table and report names, regardless of where they sit.
DATA_NAME_PATTERNS=(
  '(^|/)[^/]*_weather_[^/]*\.csv$'
  '(^|/)[^/]*_[0-9]{4}_cdl\.csv$'
  '(^|/)[^/]*_cdl\.csv$'
  '(^|/)[^/]*crop_rotation\.csv$'
  '(^|/)field-inventory\.csv$'
  '(^|/)grower_web_map\.json$'
  '(^|/)[^/]*_grower_map\.html$'
)

# Binary and archive formats that should never be committed as raw data.
BINARY_PATTERNS=(
  '\.(tif|tiff)$'
  '\.(parquet|feather|arrow)$'
  '\.(zip|gz|tgz|tar|bz2|xz|7z)$'
  '\.(npy|npz|raw|bin|pkl|h5|hdf5)$'
  '\.(so|dylib|dll)$'
  '\.(gpkg|shp|dbf|shx|prj|cpg)$'
)

FAILURES=0
COUNT=${#PATHS[@]}
TOTAL_BYTES=0

note_fail() {
  printf 'FAIL: %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}

matches_any() {
  local value=$1
  shift
  local pattern
  for pattern in "$@"; do
    if [[ "${value}" =~ ${pattern} ]]; then
      return 0
    fi
  done
  return 1
}

if (( COUNT == 0 )); then
  if (( QUIET == 0 )); then
    printf 'PASS: No %s files to audit\n' "${MODE}"
  fi
  exit 0
fi

for path in "${PATHS[@]}"; do
  if matches_any "${path}" "${RUNTIME_DIR_PATTERNS[@]}"; then
    note_fail "runtime directory in commit: ${path}"
    continue
  fi
  if matches_any "${path}" "${RUNTIME_FILE_PATTERNS[@]}"; then
    note_fail "runtime file in commit: ${path}"
    continue
  fi
  if matches_any "${path}" "${DATA_NAME_PATTERNS[@]}"; then
    note_fail "generated data file in commit: ${path}"
    continue
  fi
  if matches_any "${path}" "${BINARY_PATTERNS[@]}"; then
    note_fail "binary or archive file in commit: ${path}"
    continue
  fi
done

# Size checks use compressed blob sizes, matching what Git would store.
for path in "${PATHS[@]}"; do
  size=$(blob_size "${path}")
  if [[ -z "${size}" ]]; then
    continue
  fi
  TOTAL_BYTES=$((TOTAL_BYTES + size))
  if (( MAX_FILE_BYTES > 0 && size > MAX_FILE_BYTES )); then
    note_fail "file exceeds per-file limit (${size} > ${MAX_FILE_BYTES} bytes): ${path}"
  fi
done

if (( MAX_TOTAL_BYTES > 0 && TOTAL_BYTES > MAX_TOTAL_BYTES )); then
  note_fail "commit scope exceeds total limit (${TOTAL_BYTES} > ${MAX_TOTAL_BYTES} bytes)"
fi

if (( FAILURES > 0 )); then
  printf 'FAIL: %d commit-scope violation(s) across %d file(s)\n' "${FAILURES}" "${COUNT}"
  exit 1
fi

if (( QUIET == 0 )); then
  printf 'PASS: Commit scope clean (%d file(s), %d bytes)\n' "${COUNT}" "${TOTAL_BYTES}"
fi
exit 0
