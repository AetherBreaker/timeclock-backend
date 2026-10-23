# timeclock-backend

The server side of [timeclock-system](https://github.com/AetherBreaker/timeclock-system), deployed by Coolify on the VPS. It's included in timeclock-system as a submodule at `backend/`.

For now it's the devkit-managed Python app with only a heartbeat. The database relay, PostgreSQL (a sidecar in `docker/compose.yaml`) and the scheduler come with the design spec.

Coolify reaches the clocks themselves through [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel), a separate application.

## Coolify environment

| Variable | Meaning |
| --- | --- |
| `ALERTS_EMAIL_PWD` | aeth_ext's alert mailbox password |
| `PINGKEY` | Optional: the healthchecks.io ping key |
