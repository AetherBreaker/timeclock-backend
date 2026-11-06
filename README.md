# timeclock-backend

The server side of [timeclock-system](https://github.com/AetherBreaker/timeclock-system), deployed by Coolify on the VPS. It's included in timeclock-system as a submodule at `backend/`.

## What it runs

**Relay.** A TCP relay on port 5432 forwards the time clocks' PostgreSQL connections to the sidecar. It accepts only tunnel addresses (`10.8.0.0/24`) and closes every other connection before PostgreSQL sees it.

**PostgreSQL 18.** A sidecar, the compose service `timeclock-postgres` in `docker/compose.yaml`, hosts two databases, `timeclock` and `timeclock_test`. Only the backend reaches it, over a private network; the owner's SQL editor uses a loopback-only publish over SSH.

**Migrations.** At startup the backend applies the migrations from [timeclock-schema](https://github.com/AetherBreaker/timeclock-schema) to both databases.

**Automatic clock-out.** Each database records every scheduled run in `backend.auto_clock_out_runs`, projected 7 days ahead from `config.settings` in the backend's `TZ`. The backend runs each row at its time, catches up missed rows from the last 14 days oldest first, and marks each row with when it ran and how many shifts it closed.

The backend is a WireGuard spoke on the hub's tunnel, without tolerate: a lost tunnel makes the container fail and restart rather than run on without it.

Coolify reaches the clocks themselves through [coolify-tunnel](https://github.com/AetherBreaker/coolify-tunnel), a separate application.

## Shift corrections page

The compose service `timeclock-web` runs the shift corrections page from the same image (`timeclock-web` console script, NiceGUI on port 8080, behind Coolify's proxy). Managers sign in with Zoho, or with an email and password from `PAGE_TEST_ACCOUNTS` while testing; either way only an email listed in `backend.editors` on `timeclock` gets in, and every page load and save checks the sign-in again. The page's PostgreSQL login, `timeclock_web`, has no password until the owner sets one, and the sidecar refuses it from anywhere but the page's address on the private `timeclock-web-db` network (`10.240.17.0/28`).

The page opens on the week's shift list: every employee's shifts, sorted by last name, with a week total each, the Employee, Group and Role filters, and a bar counting what needs attention (automatic clock-outs, extra punches, strays). A row opens its shift in the editor: a timeline whose markers drag to the minute, the shift's times to type, breaks and lunches, the hours before and after, and History. A save writes one edit with its note; another editor's change shows up live, and one to a punch you also changed asks whose to keep. The draft lives in the browser, so a dropped connection loses nothing. Add shift opens the same editor empty for an employee and a day. Your own shifts open read-only.

| Variable | Meaning |
| --- | --- |
| `PAGE_PUBLIC_URL` | The page's address, e.g. `https://corrections.example.com`; Zoho redirects back to `<it>/auth/zoho` |
| `PAGE_SESSION_SECRET` | 64 random characters; signs the browser sessions |
| `TIMECLOCK_WEB_PASSWORD` | 48 random characters; the `timeclock_web` login's password |
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` | Zoho sign-in: the page's Server-based Application in Zoho's API console |
| `PAGE_TEST_ACCOUNTS` | Test sign-in: a JSON object of email to password (16 random characters or more), e.g. `{"name@example.com": "..."}`; empty turns it off. Mark it literal in Coolify |
| `PAGE_FAULT_CONTACT` | Optional: who the editor's "found a problem in itself" message says to tell; `Jacob` when unset |

The page refuses to start without the first three and at least one sign-in method (both Zoho variables, `PAGE_TEST_ACCOUNTS`, or both); the other services deploy regardless. Setup, once:

1. In Coolify, give `timeclock-web` its domain on port 8080 (`https://<domain>:8080` in the service's Domains field). For Zoho, register that domain in Zoho's API console with the redirect URI `https://<domain>/auth/zoho`.
2. Set the variables above, then deploy.
3. In the SQL editor, as `postgres` on `timeclock`: `ALTER ROLE timeclock_web PASSWORD '<TIMECLOCK_WEB_PASSWORD>'`.
4. Add each manager there too: `INSERT INTO backend.editors (email, employee_id, developer) VALUES ('name@example.com', 123, false)`. The email is lowercase (the page lowercases what it's given). `employee_id` is their own employee number, which blocks edits to their own punches, or `NULL` for none. `developer` true lets them switch the page to the test database. The row creates their database role; deleting it cuts off an open sign-in at once.

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
