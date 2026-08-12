#!/bin/bash

set -euo pipefail

# Keep interrupted-cleanup diagnostics visible when the active container check
# has redirected its normal output to a temporary result file.
exec 3>&2

readonly CONTAINER_NAME="pyfinder-docker"
readonly IMAGE_NAME="pyfinder:dev"
readonly EXPECTED_BASE_IMAGE="ghcr.io/sceylan/finder-base:gmt5"
readonly OWNERSHIP_LABEL_KEY="io.pyfinder.verification"
readonly OWNERSHIP_LABEL_VALUE="installed-image"
readonly CONTAINER_RUNTIME="/home/sysop/runtime"
readonly CONTAINER_USER="1000:1000"
readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly INSTALLED_HELPER="${PROJECT_ROOT}/tests/container/verify_installed_image.py"

TEMPORARY_DIRECTORY=""
RUNTIME_ROOT=""
CONTAINER_CID_FILE=""
OBSERVED_IMAGE_ID=""
OBSERVED_PYTHON_VERSION=""
VERIFIER_OWNS_ACTIVE_CONTAINER=false

fail() {
    printf 'verify-pyfinder-image: %s\n' "$1" >&2
    exit 1
}

query_canonical_container() {
    local names

    # Every run checks the exact accepted name first. A collision belongs to
    # somebody else unless this process is already inside its owned run.
    if ! names=$(
        docker container ls \
            --all \
            --filter "name=^${CONTAINER_NAME}$" \
            --format '{{.Names}}'
    ); then
        fail "could not query the exact ${CONTAINER_NAME} container name"
    fi
    if [[ -n "$names" ]]; then
        fail "${CONTAINER_NAME} already exists; leaving it untouched"
    fi
}

cleanup_owned_container() {
    local owned_container_id
    local inspected_container
    local inspected_container_id
    local ownership_label

    if [[ ! -s "$CONTAINER_CID_FILE" ]]; then
        return 0
    fi
    owned_container_id="$(<"$CONTAINER_CID_FILE")"
    if [[ ! "$owned_container_id" =~ ^[0-9a-f]{64}$ ]]; then
        printf 'verify-pyfinder-image: interrupted cleanup found an invalid private container ID; leaving %s untouched\n' \
            "$CONTAINER_NAME" >&3
        return 1
    fi

    # The fixed name and label can also belong to another concurrent verifier.
    # Remove only when Docker reports the exact ID written by this run.
    if ! inspected_container=$(
        docker container inspect \
            --format '{{.Id}}|{{index .Config.Labels "io.pyfinder.verification"}}' \
            "$CONTAINER_NAME" 2>/dev/null
    ); then
        printf 'verify-pyfinder-image: could not inspect %s during interrupted cleanup; leaving it untouched\n' \
            "$CONTAINER_NAME" >&3
        return 1
    fi
    IFS='|' read -r inspected_container_id ownership_label \
        <<< "$inspected_container"
    if [[ "$inspected_container_id" != "$owned_container_id" ]]; then
        printf 'verify-pyfinder-image: interrupted cleanup ID does not match the private container ID; leaving %s untouched\n' \
            "$CONTAINER_NAME" >&3
        return 1
    fi
    if [[ "$ownership_label" != "$OWNERSHIP_LABEL_VALUE" ]]; then
        printf 'verify-pyfinder-image: interrupted cleanup label does not match; leaving %s untouched\n' \
            "$CONTAINER_NAME" >&3
        return 1
    fi
    if ! docker container rm --force "$owned_container_id" >/dev/null; then
        printf 'verify-pyfinder-image: could not remove owned interrupted container %s\n' \
            "$owned_container_id" >&3
        return 1
    fi
}

cleanup() {
    local status=$?
    local cleanup_status=0

    trap - EXIT HUP INT TERM
    if [[ "$VERIFIER_OWNS_ACTIVE_CONTAINER" == "true" ]]; then
        cleanup_owned_container || cleanup_status=1
    fi
    if [[ -n "$TEMPORARY_DIRECTORY" && -d "$TEMPORARY_DIRECTORY" ]]; then
        chmod -R u+rwx "$TEMPORARY_DIRECTORY" 2>/dev/null || cleanup_status=1
        rm -rf -- "$TEMPORARY_DIRECTORY" || cleanup_status=1
    fi
    if [[ "$status" -eq 0 && "$cleanup_status" -ne 0 ]]; then
        status=$cleanup_status
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

run_image() {
    local mount_source="$1"
    local entrypoint_override="$2"
    local status
    local -a command=(
        docker run
        --rm
        --interactive
        --name "$CONTAINER_NAME"
        --cidfile "$CONTAINER_CID_FILE"
        --label "${OWNERSHIP_LABEL_KEY}=${OWNERSHIP_LABEL_VALUE}"
        --network none
        --platform linux/amd64
        --pull=never
        --user "$CONTAINER_USER"
        --env "PYFINDER_IMAGE_PYTHON_VERSION=${OBSERVED_PYTHON_VERSION}"
    )
    shift 2

    [[ -n "$OBSERVED_IMAGE_ID" ]] \
        || fail "the immutable local image ID has not been inspected"
    rm -f -- "$CONTAINER_CID_FILE"
    query_canonical_container
    if [[ -n "$mount_source" ]]; then
        command+=(
            --mount "type=bind,source=${mount_source},target=${CONTAINER_RUNTIME}"
        )
    fi
    if [[ -n "$entrypoint_override" ]]; then
        command+=(--entrypoint "$entrypoint_override")
    fi
    command+=("$OBSERVED_IMAGE_ID" "$@")

    # Network isolation is part of every run, including simple help checks.
    # A verifier defect therefore cannot turn into a provider request.
    VERIFIER_OWNS_ACTIVE_CONTAINER=true
    if "${command[@]}"; then
        status=0
    else
        status=$?
    fi
    VERIFIER_OWNS_ACTIVE_CONTAINER=false
    return "$status"
}

inspect_required_image() {
    local image_metadata
    local image_id
    local image_os
    local image_architecture
    local image_user
    local image_entrypoint
    local image_command
    local base_image
    local python_version

    if ! image_metadata=$(
        docker image inspect "$IMAGE_NAME" \
            --format '{{.Id}}|{{.Os}}|{{.Architecture}}|{{.Config.User}}|{{json .Config.Entrypoint}}|{{json .Config.Cmd}}|{{index .Config.Labels "org.opencontainers.image.base.name"}}|{{index .Config.Labels "io.pyfinder.python.version"}}'
    ); then
        fail "could not inspect the required local image ${IMAGE_NAME}"
    fi
    IFS='|' read -r \
        image_id \
        image_os \
        image_architecture \
        image_user \
        image_entrypoint \
        image_command \
        base_image \
        python_version <<< "$image_metadata"

    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || fail "local ${IMAGE_NAME} returned an invalid immutable image ID: ${image_id}"
    [[ "${image_os}/${image_architecture}" == "linux/amd64" ]] \
        || fail "local ${IMAGE_NAME} platform is ${image_os}/${image_architecture}; expected linux/amd64"
    [[ "$image_user" == "$CONTAINER_USER" ]] \
        || fail "local ${IMAGE_NAME} user is ${image_user}; expected ${CONTAINER_USER}"
    [[ "$image_entrypoint" == '["/usr/local/bin/pyfinder-entrypoint"]' ]] \
        || fail "local ${IMAGE_NAME} entrypoint differs from the accepted entrypoint"
    [[ "$image_command" == '["continuous"]' ]] \
        || fail "local ${IMAGE_NAME} default command differs from the accepted command"
    [[ "$base_image" == "$EXPECTED_BASE_IMAGE" ]] \
        || fail "local ${IMAGE_NAME} base label differs from ${EXPECTED_BASE_IMAGE}"
    [[ "$python_version" =~ ^3\.12\.[0-9]+$ ]] \
        || fail "local ${IMAGE_NAME} Python label is not a Python 3.12 patch release: ${python_version}"

    OBSERVED_IMAGE_ID="$image_id"
    OBSERVED_PYTHON_VERSION="$python_version"
    printf 'Observed image metadata: %s linux/amd64 user=%s Python=%s\n' \
        "$OBSERVED_IMAGE_ID" "$image_user" "$python_version"
}

host_stat() {
    local path="$1"

    stat -f '%u:%g:%Lp' "$path" 2>/dev/null \
        || stat --format='%u:%g:%a' "$path"
}

assert_no_fallback_output() {
    local output_file="$1"

    if grep -Fqi 'fallback' "$output_file"; then
        fail "verification output reported a runtime fallback"
    fi
    if grep -Fq '/home/sysop/pyfinder' "$output_file"; then
        fail "verification output selected a container-home runtime"
    fi
}

verify_missing_mount_failure() {
    local output_file="${TEMPORARY_DIRECTORY}/missing-mount.out"
    local status

    if run_image "" "" --help >"$output_file" 2>&1; then
        fail "entrypoint accepted an absent ${CONTAINER_RUNTIME} mount"
    else
        status=$?
    fi
    [[ "$status" -ne 0 ]] || fail "missing-mount check returned success"
    grep -F "required runtime mount is absent: ${CONTAINER_RUNTIME}" "$output_file" >/dev/null \
        || fail "missing-mount failure did not identify ${CONTAINER_RUNTIME}"
    grep -F 'observed ownership:' "$output_file" >/dev/null \
        || fail "missing-mount failure omitted observed ownership"
    grep -F 'required runtime identity: 1000:1000' "$output_file" >/dev/null \
        || fail "missing-mount failure omitted the required identity"
    grep -F 'host correction: correct the host path' "$output_file" >/dev/null \
        || fail "missing-mount failure omitted the host correction"
    assert_no_fallback_output "$output_file"
    printf 'Entrypoint negative passed: missing %s mount\n' "$CONTAINER_RUNTIME"
}

verify_unwritable_failure() {
    local host_path="$1"
    local container_path="$2"
    local output_name="$3"
    local output_file="${TEMPORARY_DIRECTORY}/${output_name}.out"
    local before_stat
    local after_stat
    local status

    before_stat="$(host_stat "$host_path")"
    chmod 0555 "$host_path"
    if run_image "$RUNTIME_ROOT" "" --help >"$output_file" 2>&1; then
        chmod 0777 "$host_path"
        fail "entrypoint accepted unwritable path ${container_path}"
    else
        status=$?
    fi
    [[ "$status" -ne 0 ]] || fail "unwritable-path check returned success"
    after_stat="$(host_stat "$host_path")"
    [[ "${after_stat%:*}" == "${before_stat%:*}" ]] \
        || fail "entrypoint changed ownership of ${container_path}"
    [[ "${after_stat##*:}" == "555" ]] \
        || fail "entrypoint repaired the mode of ${container_path}"
    grep -F "required runtime directory is not writable: ${container_path}" "$output_file" >/dev/null \
        || fail "unwritable failure did not identify ${container_path}"
    grep -F 'observed ownership:' "$output_file" >/dev/null \
        || fail "unwritable failure omitted observed ownership for ${container_path}"
    grep -F 'required runtime identity: 1000:1000' "$output_file" >/dev/null \
        || fail "unwritable failure omitted the required identity for ${container_path}"
    grep -F 'host correction: correct the host path' "$output_file" >/dev/null \
        || fail "unwritable failure omitted the host correction for ${container_path}"
    assert_no_fallback_output "$output_file"
    chmod 0777 "$host_path"
    printf 'Entrypoint negative passed: unwritable %s\n' "$container_path"
}

command -v docker >/dev/null 2>&1 || fail "docker is not available"
[[ -f "$INSTALLED_HELPER" ]] || fail "installed-image helper is absent: ${INSTALLED_HELPER}"

query_canonical_container
inspect_required_image

TEMPORARY_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/pyfinder-image-check.XXXXXX")"
RUNTIME_ROOT="${TEMPORARY_DIRECTORY}/runtime"
CONTAINER_CID_FILE="${TEMPORARY_DIRECTORY}/container.cid"
mkdir -p \
    "${RUNTIME_ROOT}/pyfinder/state" \
    "${RUNTIME_ROOT}/pyfinder/logs" \
    "${RUNTIME_ROOT}/pyfinder/runs" \
    "${RUNTIME_ROOT}/pyfinder/playbacks"
chmod 0777 \
    "$RUNTIME_ROOT" \
    "${RUNTIME_ROOT}/pyfinder" \
    "${RUNTIME_ROOT}/pyfinder/state" \
    "${RUNTIME_ROOT}/pyfinder/logs" \
    "${RUNTIME_ROOT}/pyfinder/runs" \
    "${RUNTIME_ROOT}/pyfinder/playbacks"

# This temporary bind proves image behavior only. It is intentionally separate
# from operator data and cannot establish deployment readiness.
positive_output="${TEMPORARY_DIRECTORY}/entrypoint-positive.out"
if ! run_image "$RUNTIME_ROOT" "" --help >"$positive_output" 2>&1; then
    sed -n '1,240p' "$positive_output" >&2
    fail "mounted entrypoint positive check failed"
fi
grep -F 'Run a PyFinder workflow process.' "$positive_output" >/dev/null \
    || fail "entrypoint did not reach the installed pyfinder command"
assert_no_fallback_output "$positive_output"
printf 'Entrypoint positive passed: mounted runtime accepted and installed CLI reached\n'

verify_missing_mount_failure
verify_unwritable_failure \
    "${RUNTIME_ROOT}/pyfinder" \
    "/home/sysop/runtime/pyfinder" \
    "unwritable-service-root"
verify_unwritable_failure \
    "${RUNTIME_ROOT}/pyfinder/state" \
    "/home/sysop/runtime/pyfinder/state" \
    "unwritable-state"
verify_unwritable_failure \
    "${RUNTIME_ROOT}/pyfinder/logs" \
    "/home/sysop/runtime/pyfinder/logs" \
    "unwritable-logs"
verify_unwritable_failure \
    "${RUNTIME_ROOT}/pyfinder/runs" \
    "/home/sysop/runtime/pyfinder/runs" \
    "unwritable-runs"
verify_unwritable_failure \
    "${RUNTIME_ROOT}/pyfinder/playbacks" \
    "/home/sysop/runtime/pyfinder/playbacks" \
    "unwritable-playbacks"

identity_output="${TEMPORARY_DIRECTORY}/installed-identity.out"
if ! run_image \
    "$RUNTIME_ROOT" \
    "/opt/python-3.12/bin/python3.12" \
    - <"$INSTALLED_HELPER" >"$identity_output" 2>&1; then
    sed -n '1,240p' "$identity_output" >&2
    fail "installed image helper failed"
fi
grep -F 'PYFINDER_IMAGE_RESULT=' "$identity_output" >/dev/null \
    || fail "installed image helper did not produce its verified result"
grep -F '"platform": "linux/amd64"' "$identity_output" >/dev/null \
    || fail "installed image helper did not confirm linux/amd64"
grep -F '"runtime_identity": "1000:1000"' "$identity_output" >/dev/null \
    || fail "installed image helper did not confirm UID/GID 1000:1000"

[[ -d "${RUNTIME_ROOT}/pyfinder/state" ]] || fail "temporary state directory disappeared"
[[ -d "${RUNTIME_ROOT}/pyfinder/logs" ]] || fail "temporary logs directory disappeared"
[[ -d "${RUNTIME_ROOT}/pyfinder/runs" ]] || fail "temporary runs directory disappeared"
[[ -d "${RUNTIME_ROOT}/pyfinder/playbacks" ]] || fail "temporary playbacks directory disappeared"

printf '%s\n' \
    'Installed identity, ParamWS logging, and controlled input materialization passed:' \
    "$(grep -F 'PYFINDER_IMAGE_RESULT=' "$identity_output")"

query_canonical_container
printf 'Installed image verification passed for %s; %s is absent after cleanup.\n' \
    "$OBSERVED_IMAGE_ID" "$CONTAINER_NAME"
