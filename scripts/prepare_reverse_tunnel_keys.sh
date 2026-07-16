#!/usr/bin/env bash
set -euo pipefail

key_dir="${REVERSE_TUNNEL_KEY_DIR:-$HOME/.config/gamb2le/reverse-tunnels}"
edge_tunnel_key="$key_dir/edge-to-data-ocean"
source_sync_key="$key_dir/data-ocean-source-sync"

create_key() {
  local path="$1"
  local comment="$2"

  if [[ -e "$path" || -e "$path.pub" ]]; then
    if [[ ! -f "$path" || ! -f "$path.pub" ]]; then
      printf 'Refusing to continue: expected both %s and %s.pub to exist.\n' "$path" "$path" >&2
      exit 1
    fi
    return
  fi

  ssh-keygen -t ed25519 -a 64 -N '' -f "$path" -C "$comment" >/dev/null
}

quote_yaml() {
  local value="$1"
  printf '"%s"' "${value//\"/\\\"}"
}

umask 077
install -d -m 0700 "$key_dir"

create_key "$edge_tunnel_key" edge-reverse-tunnel
create_key "$source_sync_key" data-ocean-source-sync

edge_tunnel_public="$(<"$edge_tunnel_key.pub")"
source_sync_public="$(<"$source_sync_key.pub")"

cat <<EOF
# Key files are in:
#   $key_dir
#
# Keep private keys outside git. The snippets below reference private keys by
# controller-local source path so Ansible can copy them without printing them.

## Cloud repo vars: GAMB2LE/aurora-cloud-infra
edge_tunnel_server_enabled: true
edge_tunnel_server_authorized_keys:
  - name: edge-reverse-tunnel
    key: $(quote_yaml "$edge_tunnel_public")

edge_source_sync_use_reverse_tunnels: false
edge_source_sync_ssh_private_key_source: $(quote_yaml "$source_sync_key")

## Edge repo vars: GAMB2LE/aurora-edge-infra
edge_managed_write_mode: true
edge_reverse_tunnels_enabled: true
edge_reverse_tunnel_private_key_source: $(quote_yaml "$edge_tunnel_key")
edge_source_sync_authorized_keys:
  - name: data-ocean-source-sync
    key: $(quote_yaml "$source_sync_public")

## Vault-content alternatives
# ansible-vault encrypt_string \\
#   --stdin-name edge_source_sync_ssh_private_key_content \\
#   < "$source_sync_key"
#
# ansible-vault encrypt_string \\
#   --stdin-name edge_reverse_tunnel_private_key_content \\
#   < "$edge_tunnel_key"
EOF
