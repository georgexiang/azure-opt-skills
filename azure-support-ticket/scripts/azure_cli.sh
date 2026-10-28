#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/azure_cli_env.sh"
validate_toolchain_paths
export AZURE_EXTENSION_DIR="$extension_dir"
export AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no
export AZURE_CORE_COLLECT_TELEMETRY=0

not_ready() {
	printf '%s\n' 'SUPPORT_TOOLCHAIN_NOT_READY: run this entry point with --ensure before loading credentials; no Azure request was sent.' >&2
	exit 78
}

check_toolchain() {
	[[ -f "$marker" && -x "$install_root/bin/az" && -d "$extension_dir/support" ]] || return 1
	local check_config result=0 version support
	check_config="$(mktemp -d)"
	version="$(env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		AZURE_CONFIG_DIR="$check_config" AZURE_EXTENSION_DIR="$extension_dir" \
		AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no AZURE_CORE_COLLECT_TELEMETRY=0 \
		"$install_root/bin/az" version --query '"azure-cli"' -o tsv 2>/dev/null)" || result=1
	support="$(env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		AZURE_CONFIG_DIR="$check_config" AZURE_EXTENSION_DIR="$extension_dir" \
		AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no AZURE_CORE_COLLECT_TELEMETRY=0 \
		"$install_root/bin/az" extension show --name support --query version -o tsv 2>/dev/null)" || result=1
	[[ "$version" == "$azure_cli_version" && "$support" == "$support_extension_version" ]] || result=1
	if [[ "$result" == 0 ]]; then
		env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
			AZURE_CONFIG_DIR="$check_config" AZURE_EXTENSION_DIR="$extension_dir" \
			AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no AZURE_CORE_COLLECT_TELEMETRY=0 \
			"$install_root/bin/az" account show --help >/dev/null 2>&1 || result=1
	fi
	if [[ "$result" == 0 ]]; then
		env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
			AZURE_CONFIG_DIR="$check_config" AZURE_EXTENSION_DIR="$extension_dir" \
			AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no AZURE_CORE_COLLECT_TELEMETRY=0 \
			"$install_root/bin/az" support in-subscription tickets create --help >/dev/null 2>&1 || result=1
	fi
	rm -rf "$check_config"
	return "$result"
}

case "${1:-}" in
	--check|--ensure)
		[[ $# == 1 ]] || fail 'Toolchain mode takes no Azure arguments'
		if [[ "$1" == --ensure ]]; then
			# Installer diagnostics go to stderr, never into an Azure JSON result.
			bash "$script_dir/install_azure_cli.sh" >&2
		fi
		check_toolchain || not_ready
		exit 0
		;;
	'') fail 'Expected Azure CLI arguments or --check/--ensure' ;;
esac

# Hold a shared installation lock through exec, so repairs cannot remove active code.
mkdir -p "$state_dir"
assert_owned_real_dir "$state_dir"
exec 9<"$state_dir"
flock --shared 9
validate_toolchain_paths
# Ordinary invocations never install, authenticate, retry or reinterpret argv.
[[ -f "$marker" && -x "$install_root/bin/az" && -d "$extension_dir/support" ]] || not_ready
exec "$install_root/bin/az" "$@"
