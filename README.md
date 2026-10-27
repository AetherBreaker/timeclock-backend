# timeclock-backend

The server side of [timeclock-system](https://github.com/AetherBreaker/timeclock-system), deployed by Coolify on the VPS. It's included in timeclock-system as a submodule at `backend/`.

## What it runs

**Relay.** A TCP relay on port 5432 forwards the time clocks' PostgreSQL connections to the sidecar. It accepts only tunnel addresses (`10.8.0.0/24`) and closes every other connection before PostgreSQL sees it.

**PostgreSQL 18.** A sidecar, the compose service `timeclock-postgres` in `docker/compose.yaml`, hosts two databases, `timeclock` and `timeclock_test`. Only the backend reaches it, over a private network; the owner's SQL editor uses a loopback-only publish over SSH.

**Migrations.** At startup the backend applies the migrations from [timeclock-schema](https://github.com/AetherBreaker/timeclock-schema) to both databases.

**Automatic clock-out.** Each database records every scheduled run in `backend.auto_clock_out_runs`, projected 7 days ahead from `config.settings` in the backend's `TZ`. The backend runs each row at its time, catches up missed rows from the last 14 days oldest first, and marks each row with when it ran and how many shifts it closed.

The backend is a WireGuard spoke on the hub's tunnel, without tolerate: a lost tunnel makes the container fail and restart rather than run on without it.

Coolify reaches the clocks themselves through [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel), a separate application.

## Coolify environment

| Variable | Meaning |
| --- | --- |
| `ALERTS_EMAIL_PWD` | aeth_ext's alert mailbox password |
| `PINGKEY` | Optional: the healthchecks.io ping key |
| `WG_PRIVATE_KEY` | This spoke's WireGuard private key |
| `WG_HUB_URL` | Where the spoke fetches the hub's configuration |
| `WG_HUB_REPO` | The repository holding the hub's configuration |
| `WG_HUB_TOKEN` | Optional: the token for reading that repository |
| `POSTGRES_PASSWORD` | The `postgres` superuser's password, for the sidecar and the backend |

The sidecar applies `POSTGRES_PASSWORD` only when it first initializes its data folder. To rotate it, run `ALTER ROLE postgres PASSWORD '…'` in the SQL editor first, then change it in Coolify and redeploy.
