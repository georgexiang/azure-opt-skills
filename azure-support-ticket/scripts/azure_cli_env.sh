#!/usr/bin/env bash
# Packaged identically in both Azure bundles; callers consume these settings.
# shellcheck disable=SC2034
azure_cli_version="2.89.1"
support_extension_version="2.0.1"
support_extension_url="https://azcliprod.blob.core.windows.net/cli-extensions/support-2.0.1-py2.py3-none-any.whl"
support_extension_sha256="3f070154f839464aa24b54990d87d2dba99d6da60ec3ef314a74ba22c268fec4"
install_base="$HOME/.local/share/azure-cli"
install_root="$install_base/$azure_cli_version"
bin_dir="$HOME/.local/bin"
extension_dir="$install_base/extensions"
state_dir="$HOME/.local/state"
marker="$install_root/.complete"

fail() {
	printf '%s\n' "$1" >&2
	exit 1
}

assert_owned_real_dir() {
	local path="$1"
	[[ -d "$path" && ! -L "$path" ]] || fail "Unsafe Azure CLI install directory: $path"
	[[ "$(stat -c %u "$path")" == "$(id -u)" ]] || fail "Azure CLI install directory has another owner: $path"
}

assert_safe_existing_path() {
	local path="$1"
	if [[ -e "$path" || -L "$path" ]]; then
		assert_owned_real_dir "$path"
	fi
}

validate_toolchain_paths() {
	[[ "$HOME" == /* ]] || fail "HOME must be an absolute path"
	assert_owned_real_dir "$HOME"
	local path
	for path in "$HOME/.local" "$HOME/.local/share" "$install_base" "$install_root" \
		"$install_root/bin" "$bin_dir" "$extension_dir" "$state_dir"; do
		assert_safe_existing_path "$path"
	done
	for path in "$install_root/bin/az" "$marker"; do
		[[ ! -L "$path" ]] || fail "Unsafe Azure CLI install file: $path"
	done
}
