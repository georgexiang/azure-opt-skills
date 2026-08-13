#!/usr/bin/env bash
set -euo pipefail

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
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock_file="$script_dir/azure-cli-2.89.1.lock"
backup_root=""
extension_staging=""
extension_backup=""
config_dir=""
new_cli_created=false
new_extension_promoted=false
az_link_created=false
install_succeeded=false

cleanup() {
	if [[ "$install_succeeded" != true ]]; then
		if [[ -n "$backup_root" && -d "$backup_root" ]]; then
			rm -rf "$install_root"
			mv "$backup_root" "$install_root"
		elif [[ "$new_cli_created" == true ]]; then
			rm -rf "$install_root"
		fi
		if [[ -n "$extension_backup" && -d "$extension_backup" ]]; then
			rm -rf "$extension_dir"
			mv "$extension_backup" "$extension_dir"
		elif [[ "$new_extension_promoted" == true ]]; then
			rm -rf "$extension_dir"
		fi
		if [[ "$az_link_created" == true && -L "$bin_dir/az" ]]; then
			rm -f "$bin_dir/az"
		fi
	fi
	if [[ -n "$extension_staging" && -d "$extension_staging" ]]; then
		rm -rf "$extension_staging"
	fi
	if [[ -n "$config_dir" && -d "$config_dir" ]]; then
		rm -rf "$config_dir"
	fi
}
trap cleanup EXIT

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

[[ "$HOME" == /* ]] || fail "HOME must be an absolute path"
assert_owned_real_dir "$HOME"
assert_safe_existing_path "$HOME/.local"
assert_safe_existing_path "$HOME/.local/share"
assert_safe_existing_path "$install_base"
assert_safe_existing_path "$install_root"
assert_safe_existing_path "$bin_dir"
assert_safe_existing_path "$extension_dir"
assert_safe_existing_path "$state_dir"

if [[ -e "$bin_dir/az" || -L "$bin_dir/az" ]]; then
	[[ -L "$bin_dir/az" ]] || fail "Refusing to replace non-symlink Azure CLI command: $bin_dir/az"
	[[ "$(readlink "$bin_dir/az")" == "../share/azure-cli/$azure_cli_version/bin/az" ]] || \
		fail "Refusing to replace unowned Azure CLI symlink: $bin_dir/az"
fi

if [[ ! -f "$lock_file" ]]; then
	printf 'Missing Azure CLI dependency lock: %s\n' "$lock_file" >&2
	exit 1
fi

check_installation() {
	[[ -f "$marker" && -x "$install_root/bin/az" && -L "$bin_dir/az" ]] || return 1
	[[ "$(readlink "$bin_dir/az")" == "../share/azure-cli/$azure_cli_version/bin/az" ]] || return 1
	[[ "$(installed_cli_version)" == "$azure_cli_version" ]] || return 1
	local check_config support_version
	check_config="$(mktemp -d)"
	support_version="$(
		env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
			AZURE_CORE_COLLECT_TELEMETRY=0 \
			AZURE_CONFIG_DIR="$check_config" \
			AZURE_EXTENSION_DIR="$extension_dir" \
			"$install_root/bin/az" extension show --name support --query version -o tsv 2>/dev/null || true
	)"
	rm -rf "$check_config"
	[[ "$support_version" == "$support_extension_version" ]]
}

installed_cli_version() {
	local check_config version
	check_config="$(mktemp -d)"
	version="$(
		env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
			AZURE_CONFIG_DIR="$check_config" AZURE_CORE_COLLECT_TELEMETRY=0 \
			"$install_root/bin/az" version --query '"azure-cli"' -o tsv 2>/dev/null || true
	)"
	rm -rf "$check_config"
	printf '%s' "$version"
}

if [[ "${1:-}" == "--check" ]]; then
	check_installation
	exit $?
fi

for directory in "$HOME/.local" "$HOME/.local/share" "$install_base" "$bin_dir" "$state_dir"; do
	mkdir -p "$directory"
	assert_owned_real_dir "$directory"
done
exec 9<"$state_dir"
flock 9

if check_installation; then
	printf 'Azure CLI %s and support extension %s are already installed at %s\n' \
		"$azure_cli_version" "$support_extension_version" "$install_root"
	exit 0
fi

installed_cli=""
if [[ -f "$marker" && -x "$install_root/bin/az" ]]; then
	installed_cli="$(installed_cli_version)"
fi

if [[ "$installed_cli" != "$azure_cli_version" ]]; then
	if [[ -d "$install_root" ]]; then
		backup_root="$(mktemp -d "$install_base/.backup-$azure_cli_version.XXXXXX")"
		rmdir "$backup_root"
		mv "$install_root" "$backup_root"
	else
		new_cli_created=true
	fi
	python3_path="$(command -v python3)"
	python_version="$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$python3_path" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
	[[ "$python_version" == "3.11" ]] || fail "Azure CLI lock requires Python 3.11, found $python_version"
	env -i HOME="$HOME" PATH=/usr/bin:/bin "$python3_path" -m venv "$install_root"
	env -i \
		HOME="$HOME" \
		PATH=/usr/bin:/bin \
		LANG=C.UTF-8 \
		PIP_CONFIG_FILE=/dev/null \
		"$install_root/bin/python" -m pip install \
		--disable-pip-version-check \
		--no-cache-dir \
		--quiet \
		--require-hashes \
		--only-binary=:all: \
		--index-url https://pypi.org/simple \
		-r "$lock_file"
	printf '%s\n' "$azure_cli_version" >"$install_root/.complete"
fi

if [[ ! -L "$bin_dir/az" ]]; then
	ln -s "../share/azure-cli/$azure_cli_version/bin/az" "$bin_dir/az"
	az_link_created=true
fi

config_dir="$(mktemp -d)"
extension_staging="$(mktemp -d "$install_base/.extensions-$support_extension_version.XXXXXX")"

installed_support="$(
	env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		AZURE_CORE_COLLECT_TELEMETRY=0 \
		AZURE_CONFIG_DIR="$config_dir" \
		AZURE_EXTENSION_DIR="$extension_dir" \
		"$bin_dir/az" extension show --name support --query version -o tsv 2>/dev/null || true
)"

if [[ "$installed_support" != "$support_extension_version" ]]; then
	support_wheel="$config_dir/support-$support_extension_version-py2.py3-none-any.whl"
	curl --disable --fail --silent --show-error --location "$support_extension_url" --output "$support_wheel"
	printf '%s  %s\n' "$support_extension_sha256" "$support_wheel" | sha256sum --check --status
	env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		AZURE_CORE_COLLECT_TELEMETRY=0 \
		AZURE_CONFIG_DIR="$config_dir" \
		AZURE_EXTENSION_DIR="$extension_staging" \
		"$bin_dir/az" extension add \
			--source "$support_wheel" \
			--yes \
			--only-show-errors
	staged_support="$(
		env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
			AZURE_CORE_COLLECT_TELEMETRY=0 \
			AZURE_CONFIG_DIR="$config_dir" \
			AZURE_EXTENSION_DIR="$extension_staging" \
			"$bin_dir/az" extension show --name support --query version -o tsv
	)"
	[[ "$staged_support" == "$support_extension_version" ]] || fail "Support extension staging verification failed"
	if [[ -d "$extension_dir" ]]; then
		extension_backup="$(mktemp -d "$install_base/.extensions-backup-$support_extension_version.XXXXXX")"
		rmdir "$extension_backup"
		mv "$extension_dir" "$extension_backup"
	fi
	mv "$extension_staging" "$extension_dir"
	extension_staging=""
	new_extension_promoted=true
fi

installed_cli="$(
	env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		AZURE_CONFIG_DIR="$config_dir" AZURE_CORE_COLLECT_TELEMETRY=0 \
		"$bin_dir/az" version --query '"azure-cli"' -o tsv
)"
installed_support="$(
	env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C.UTF-8 \
		AZURE_CORE_COLLECT_TELEMETRY=0 \
		AZURE_CONFIG_DIR="$config_dir" \
		AZURE_EXTENSION_DIR="$extension_dir" \
		"$bin_dir/az" extension show --name support --query version -o tsv
)"

if [[ "$installed_cli" != "$azure_cli_version" || "$installed_support" != "$support_extension_version" ]]; then
	printf 'Azure CLI verification failed: cli=%s support=%s\n' "$installed_cli" "$installed_support" >&2
	exit 1
fi

rm -rf "$config_dir"
config_dir=""
install_succeeded=true
if [[ -n "$backup_root" && -d "$backup_root" ]]; then
	rm -rf "$backup_root" || true
	backup_root=""
fi
if [[ -n "$extension_backup" && -d "$extension_backup" ]]; then
	rm -rf "$extension_backup" || true
	extension_backup=""
fi

printf 'Azure CLI %s and support extension %s installed at %s\n' \
	"$installed_cli" "$installed_support" "$install_root"