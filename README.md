# timeclock-backend

The server side of [timeclock-system](https://github.com/AetherBreaker/timeclock-system), deployed by Coolify on the VPS. It's included in timeclock-system as a submodule at `backend/`.

Today it runs two services (`docker/compose.yaml`):

| Service | What it does |
| --- | --- |
| `timeclock-backend` | The devkit-managed Python app. For now it only writes the heartbeat; the database relay, PostgreSQL and the scheduler come with the design spec. |
| `timeclock-tunnel` | A forwarding-only `sshd` (its own Dockerfile in `docker/tunnel/`, not listed in `[tool.docker].services`). Each clock dials it on port **2223** and opens one reverse forward to its own SSH server. Coolify then reaches that clock's host at `timeclock-tunnel:<port>` over the `coolify` network. |

## Coolify environment

| Variable | Meaning |
| --- | --- |
| `ALERTS_EMAIL_PWD` | aeth_ext's alert mailbox password |
| `TUNNEL_SSH_HOST_KEY_B64` | The tunnel's ed25519 host key, base64-encoded. Keep it stable: every clock pins its fingerprint. |
| `PINGKEY` | Optional: the healthchecks.io ping key |

Make the host key once, on any machine, and paste the base64 output into Coolify as a secret:

```powershell
ssh-keygen -t ed25519 -N '""' -C timeclock-tunnel -f timeclock_tunnel_host_key
[Convert]::ToBase64String([IO.File]::ReadAllBytes("timeclock_tunnel_host_key"))
```

The tunnel's log prints the key's fingerprint at every start. The clocks check the VPS against that fingerprint when they set up the tunnel. The VPS's firewall must allow TCP **2223** inbound.

## Adding a clock

1. On the clock, run `sudo timeclock-tunnel-setup`. It prints one line: `restrict,port-forwarding,permitlisten="0.0.0.0:<port>" ssh-ed25519 ...`.
2. Add that line to `docker/tunnel/authorized_keys`, commit, push and redeploy. Ports are `22001` for the first clock, then `22002`, and so on.
3. In Coolify, add a server at `timeclock-tunnel`, port `<port>`, user `root`.

A clock may open only its own port: no local forwards, no shell, no other port.
