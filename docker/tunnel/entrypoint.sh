#!/bin/sh
# Install the host key from TUNNEL_SSH_HOST_KEY_B64 (a Coolify secret, so the clocks' pinned
# fingerprint survives redeploys), print its fingerprint, and run sshd in the foreground.
set -eu
[ -n "${TUNNEL_SSH_HOST_KEY_B64:-}" ] || { echo "TUNNEL_SSH_HOST_KEY_B64 is not set" >&2; exit 1; }
KEY=/etc/ssh/keys/ssh_host_ed25519_key
install -d -m 700 /etc/ssh/keys
echo "$TUNNEL_SSH_HOST_KEY_B64" | base64 -d > "$KEY"
chmod 600 "$KEY"
ssh-keygen -y -f "$KEY" > "$KEY.pub"
echo "timeclock-tunnel host key: $(ssh-keygen -lf "$KEY.pub")"
exec /usr/sbin/sshd -D -e
