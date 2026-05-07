# Deploying rental_finder

Three things matter for this app:

1. **Outbound IP reputation** — sites like MagicBricks / 99acres / OLX
   block known cloud IP ranges (AWS, GCP, DigitalOcean) more aggressively
   than residential ones. The hosting choice affects how often you get
   blocked.
2. **Persistent disk** — the SQLite cache at `~/.rental_finder/cache.sqlite`
   needs to survive container restarts.
3. **Secrets** — `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX`, and (optionally) any
   FB cookie state must NEVER end up in a docker image, git repo, or log.

## TL;DR — pick one

| You are | Pick |
|---|---|
| Just want it running for yourself | **Cloudflare Tunnel from your laptop / home server** (zero cost, residential IP, no scraping issues) |
| Want a real always-on URL, light public usage | **Fly.io** — Mumbai region, Docker, persistent volume, free tier covers this |
| Want simplest "git push, deploy" | **Railway.app** — $5/mo, US/EU only, won't have IN-region latency |
| Want full control / self-host | **Hetzner CX22** (€4/mo) or **DigitalOcean Bangalore droplet** ($4/mo) |

## Quickstart (this machine, for testing the production image locally)

```powershell
cd "c:\work\personal projects\rental_finder"

# 1. Run the test suite. Should be 46 passed, 0 failed.
cd ..
python -m pytest rental_finder\tests -q
cd rental_finder

# 2. Build and run the production container.
docker compose up --build -d

# 3. Confirm health and home page.
Invoke-WebRequest http://localhost:8000/healthz -UseBasicParsing
Start-Process http://localhost:8000

# 4. Tail logs.
docker compose logs -f web

# 5. Tear down (preserves the cache volume).
docker compose down
```

Want more concurrency? `$env:WORKERS=2; docker compose up --build -d`.

## Secrets hygiene (do this first, regardless of host)

1. **Verify nothing is committed**

   ```powershell
   cd "c:\work\personal projects"
   git status
   # If `rental_finder/.env` shows up as untracked, the .gitignore is working.
   # If it shows up as modified or staged, STOP and fix the .gitignore before committing.
   ```

2. **Restrict the Google API key in Google Cloud Console**

   Open <https://console.cloud.google.com/apis/credentials>, click your API key.

   - **Application restrictions**: pick **HTTP referrers** (recommended) and add:
     - `http://localhost:8000/*`
     - `http://127.0.0.1:8000/*`
     - your production hostname, e.g. `https://rental.example.com/*`

     (If you'll only call it from server-side code, "IP addresses" works too — set it to your host's egress IP.)

   - **API restrictions**: tick **Restrict key**, allow only **Custom Search API**.

   Without these restrictions, anyone who steals your key can burn through your free quota or, worse, run up billing if you've enabled it. Restrictions = if a key leaks, it's useless.

3. **Rotate if you suspect a leak.** New API key in Cloud Console → update `.env` (or platform secret) → delete old key.

4. **For the bot's `.env` file**:
   - Keep it out of git (`.gitignore` already covers it).
   - Don't email it. Don't paste it into chats. Don't put it in screenshots.
   - On the host, set permissions to 600: `chmod 600 rental_finder/.env`.

## Option 1 — Cloudflare Tunnel from your machine (recommended for personal use)

You run the bot on your laptop or home server. Cloudflare Tunnel exposes it at a public HTTPS URL with no port-forwarding, no firewall changes, and your residential IP scrapes the rental sites (which is gold — those sites don't block residential IPs).

Cost: **$0**. Latency: best in class for Indian users.

### Steps

1. Install `cloudflared`:

   ```powershell
   winget install --id Cloudflare.cloudflared
   ```

2. (Optional, only for a custom domain) buy/own a domain and add it to your Cloudflare account.

3. Start the bot in production mode (keep it running in PowerShell):

   ```powershell
   cd "c:\work\personal projects"
   python -m uvicorn rental_finder.web.app:app --host 127.0.0.1 --port 8000
   ```

4. In another terminal, start a quick tunnel (no domain required):

   ```powershell
   cloudflared tunnel --url http://localhost:8000
   ```

   Cloudflare prints a URL like `https://random-words.trycloudflare.com`. Anyone with that URL can reach your bot. The URL changes each time you restart `cloudflared`.

5. (Optional) Named tunnel + your own domain — see <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>.

### Pros / cons

- Plus: residential IP (best scraping success), zero hosting cost, your own machine = your own data, HTTPS for free, Cloudflare DDoS protection.
- Minus: machine must stay on. Quick tunnels rotate URL on restart (use named tunnels for stable URL).

## Option 2 — Fly.io (recommended for "always-on, real URL")

Free tier covers this app: 3 shared-cpu-1x machines, 256-2048 MB RAM, 3 GB persistent volume. **Mumbai region (`bom`)** is available for low Indian-user latency.

### Steps

1. Install Fly CLI: <https://fly.io/docs/hands-on/install-flyctl/>.

   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   ```

2. Sign up (`fly auth signup`), add a credit card for verification (no charges if you stay within free tier limits).

3. From the project root:

   ```powershell
   cd "c:\work\personal projects\rental_finder"
   fly launch --no-deploy --name rental-finder --region bom
   ```

   This generates a `fly.toml`. Fly detects the Dockerfile and uses it.

4. Edit `fly.toml` (Fly creates it next to the Dockerfile) — add a persistent
   volume mount and the right port:

   ```toml
   app = "rental-finder"
   primary_region = "bom"

   [build]
     dockerfile = "Dockerfile"

   [http_service]
     internal_port = 8000
     force_https = true
     auto_stop_machines = "stop"
     auto_start_machines = true
     min_machines_running = 0

   [[mounts]]
     source = "rf_data"
     destination = "/home/app/.rental_finder"

   [[vm]]
     memory = "512mb"
     cpu_kind = "shared"
     cpus = 1
   ```

5. Create the volume and set secrets (these are **never** stored in your repo):

   ```powershell
   fly volumes create rf_data --region bom --size 1
   fly secrets set `
     GOOGLE_CSE_KEY="<your_key>" `
     GOOGLE_CSE_CX="<your_cx>"
   ```

6. Deploy:

   ```powershell
   fly deploy
   ```

7. Open it:

   ```powershell
   fly open
   ```

   Your app is at `https://rental-finder.fly.dev`.

### Future updates

```powershell
fly deploy
```

That's it. Fly rebuilds the image, deploys, runs healthcheck, swaps traffic.

### IP-reputation note

Fly's BOM region uses Equinix IPs which are mostly OK with Indian rental sites today. If you start seeing source counts drop (lots of 0s in the source pills), you can switch on `HTTP_PROXY` env var to route through a residential proxy. For personal traffic you're unlikely to hit this.

## Option 3 — Railway.app (simplest "click a button")

Cost: $5/month base, includes 500 hours of usage and a small persistent volume.

### Steps

1. Sign up at <https://railway.app>, connect your GitHub.
2. Push this project to a private GitHub repo (the `.gitignore` keeps `.env` safe).
3. In Railway: **New Project → Deploy from GitHub repo**, pick this repo.
4. Railway auto-detects the Dockerfile.
5. **Variables** tab → add `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX`, and any other env from `.env.example`.
6. **Settings → Volumes** → add a volume mounted at `/home/app/.rental_finder`.
7. Deploy. URL is `https://yourproject-production.up.railway.app`.

Cons: no Indian region (US/EU only). Slightly higher latency. IPs are well-known cloud ranges; expect occasional source blocks.

## Option 4 — Generic VPS (DigitalOcean / Hetzner / Linode)

Most flexibility, cheapest at scale, more setup work.

```bash
# On a fresh Ubuntu 24.04 VPS
sudo apt update && sudo apt install -y docker.io docker-compose-v2 ufw
sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
sudo usermod -aG docker $USER && newgrp docker

git clone <your-private-repo-url> rental-finder && cd rental-finder/rental_finder
cp .env.example .env && nano .env   # paste your real keys
chmod 600 .env

docker compose --env-file .env up -d --build
```

Then put **Caddy** in front for free Let's Encrypt HTTPS:

```bash
sudo apt install -y caddy
echo "rental.yourdomain.com {
    reverse_proxy localhost:8000
}" | sudo tee /etc/caddy/Caddyfile

sudo systemctl restart caddy
```

DNS: point `rental.yourdomain.com` A-record to your VPS IP. Caddy auto-issues HTTPS.

### Recommended VPS specs

- **2 GB RAM** if you keep Playwright (`INCLUDE_PLAYWRIGHT=1`).
- **1 GB RAM** if you don't need Facebook source (default).
- **20 GB disk** is plenty.
- **DigitalOcean Bangalore** for Indian-user latency.

## Option 5 — Self-host with `tailscale serve`

If you only need it accessible to your devices (phone, laptop), Tailscale's `serve` command exposes localhost over your private mesh:

```powershell
tailscale serve --bg https / http://localhost:8000
```

Reachable at `https://<your-machine>.<tailnet>.ts.net` from any device signed into Tailscale. Zero cost, zero public exposure.

## Production-mode launch (any host)

The default uvicorn command serves a single worker (good for our use case — the bottleneck is upstream HTTP, not CPU). The Dockerfile + docker-compose honour a `WORKERS` env var:

```bash
# 2 workers, behind a reverse proxy that sets X-Forwarded-* headers
WORKERS=2 docker compose up -d --build
```

The container also passes `--proxy-headers --forwarded-allow-ips '*'` to uvicorn so it correctly reads the client IP from `X-Forwarded-For`.

For >2 workers, add a Redis-backed cache instead of the SQLite one — workers don't share file locks. With aiosqlite + WAL mode the current cache is OK at ~2 workers per container; beyond that you start contending on writes.

### Stability checklist (already in this build)
- Per-source `asyncio.wait_for` timeout of 30 s; one slow source can never block the response.
- Source exceptions are caught per-source — a single broken site never aborts the request.
- Hard filters drop out-of-band listings before render so the UI shows what the user actually asked for.
- Container has a real `HEALTHCHECK` (curl `/healthz`).
- Memory limit of 1 GB and JSON-file log rotation (10 MB × 5) in `docker-compose.yml`.
- `restart: unless-stopped` so the container comes back after a crash.
- Non-root user (uid 1000) inside the container.

## Hardening checklist before going public

- [ ] `.env` is in `.gitignore` and not committed (`git ls-files | grep -i env` returns only `.env.example`).
- [ ] Google API key has HTTP referrer + API restrictions in Cloud Console.
- [ ] If using Fly/Railway, secrets are set via the platform's secret store, not baked into the image.
- [ ] HTTPS is enforced (Cloudflare Tunnel / Fly / Caddy give this for free).
- [ ] If exposing publicly, add a basic-auth layer or rate-limit (Caddy and Cloudflare both do this in one config line) so people don't burn through your Google CSE quota.
- [ ] A `chmod 600 .env` on any host where the file is on disk.

## What to NOT do

- Do NOT commit `.env`. Ever.
- Do NOT put the API key in the JavaScript / template — it's already server-side and stays there.
- Do NOT enable Facebook scraping (`--enable-facebook`) on a cloud VPS with your real account. Use a throwaway account or skip it. Cloud IPs + Meta = lockout within hours.
- Do NOT enable billing on the Google Cloud project unless you really need >100 CSE queries/day. Without billing, hitting the quota just returns 429s — annoying but free.
