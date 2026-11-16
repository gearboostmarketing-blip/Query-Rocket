# Query Rocket — The Complete Guide

Everything you need to deploy Query Rocket, connect your first client, and run it week to week. Read Parts 1 and 2 before deploy day; Part 3 is the step-by-step you follow on the day.

---

## Part 1 — What you have

### The files

| File | What it is |
|---|---|
| `query-rocket.zip` | The whole hub: dashboard, crawler, AI fix engine, Google sync, autopilot, tests, Docker setup. Also contains the plugin, the Claude skill, and this guide. |
| `query-rocket-connector.zip` | The WordPress plugin you upload to each client site. |
| `gearboost-seo-writing.zip` | The Claude skill for writing titles and descriptions by hand in Claude. Optional. |
| `seo_writing_guide.md` | How Query Rocket writes. Read it once; it's your agency's SEO voice. |

### How the pieces fit

- **The hub** runs on its own DigitalOcean droplet at an address like `hub.gearboostmarketing.com`. It crawls sites, pulls Google data, writes and reviews fixes with AI, and shows you everything in the dashboard.
- **The plugin** runs on each client's WordPress site (on Cloudways, as they are now). It only acts when the hub sends a signed, approved change, and it logs every change so it can be undone.
- **Google** (Search Console and GA4) is read-only. Query Rocket never changes anything in Google.
- **Anthropic's API** does the AI work. You pay per use (roughly $1 to $3 per site per month once running).

### What happens automatically once it's running

| When | What |
|---|---|
| Nightly, 2:15 a.m. | Recrawls any site not crawled in the last 7 days, then runs autopilot where it's on |
| Every hour | Sends approved fixes that were waiting, retries anything stuck |
| Daily, 5 a.m. | Pulls Search Console and GA4 data |
| Daily, 6 a.m. | Checks results on fixes applied 31+ days ago; rolls back autopilot titles/descriptions that hurt clicks |
| Monday, 7 a.m. | Emails the weekly report |

---

## Part 2 — Before deploy week (about an hour, spread out)

Doing these ahead of time means deploy day is just following steps.

### 1. Decide the hub's address

Pick a subdomain you control, e.g. `hub.gearboostmarketing.com`. You'll point it at the server on deploy day.

### 2. Get an Anthropic API key and set a spending limit

1. Go to **console.anthropic.com** and sign in or create an account.
2. Add a payment method under **Billing**, and buy a small amount of credit (e.g. $20) to start.
3. Set a **monthly spend limit** (e.g. $50) so a mistake can never surprise you.
4. Under **API Keys**, create a key named "Query Rocket" and save it somewhere safe (a password manager). You'll paste it into the server's settings.

### 3. Put the code in a private GitHub repository

The server downloads the code from GitHub, and updates later come the same way.

1. Create a free account at **github.com** if you don't have one.
2. Click **New repository**. Name it `query-rocket`. Choose **Private**. Don't add a README. Create it.
3. Unzip `query-rocket.zip` on your computer. You get a folder called `query-rocket`.
4. Upload the code. Easiest way: install **GitHub Desktop**, choose **File → Add local repository**, pick the `query-rocket` folder, let it create the repository, then **Publish** to the private repo you made.
   Command-line alternative, run inside the `query-rocket` folder:
   ```bash
   git init
   git add .
   git commit -m "Query Rocket v1"
   git branch -M main
   git remote add origin https://github.com/YOUR_GITHUB_USERNAME/query-rocket.git
   git push -u origin main
   ```
5. Check on github.com that you see folders like `core`, `queryrocket`, `wordpress-plugin`, and that there is **no** `.env` file (there shouldn't be; it's excluded on purpose).

### 4. Choose your first test client

Pick one client site you know well, on WordPress with Rank Math, ideally not your most sensitive client. You'll run Query Rocket there for two weeks before adding others. If it's built with Oxygen, even better: that's the part most worth testing.

### 5. Optional: email for the Monday report

If you want the weekly email from day one, have SMTP details ready (Google Workspace, Mailgun, or Postmark all work). You can skip this and add it later.

### 6. Read the writing guide

Open `seo_writing_guide.md` and change anything that doesn't sound like GearBoost. Every AI fix follows it. If you change it, commit the change to GitHub before deploy day.

---

## Part 3 — Deploy day (about 2 to 3 hours the first time)

Follow these in order. Commands go in your computer's terminal (Terminal on Mac, Windows Terminal or PowerShell on Windows) unless it says you're on the server. Copy and paste them exactly, replacing anything in CAPITALS or `yourdomain` with your own values.

If anything errors, stop and send me the exact message or a screenshot.

### Step 1 — Create an SSH key on your computer (skip if you have one)

```bash
ssh-keygen -t ed25519 -C "jared-laptop"
```

Press Enter to accept the default location and set a passphrase. Then show the public key so you can copy it:

```bash
cat ~/.ssh/id_ed25519.pub
```

---

### Step 2 — Create the droplet

In DigitalOcean: **Create → Droplets**.

| Setting | Choose |
|---|---|
| Region | New York or whichever is closest to most clients |
| Image | Ubuntu 24.04 LTS |
| Size | Basic, Regular, **2 vCPU / 4 GB RAM** (around $24/month) |
| Authentication | SSH Key → add the public key from Step 1 |
| Backups | Turn on weekly backups (adds about 20% to the price, worth it) |
| Monitoring | Turn on (free) |
| Hostname | `query-rocket-hub` |

Why 4 GB: the crawler runs a real browser (Playwright), and together with the database and AI job workers, 2 GB gets tight. You can resize up later in a few clicks if needed.

Copy the droplet's **IP address** once it's created.

---

### Step 3 — Point your subdomain at the droplet

Wherever your domain's DNS is managed (Cloudflare, your registrar, or DigitalOcean), add:

| Type | Name | Value |
|---|---|---|
| A | `hub` | your droplet IP |

If you use Cloudflare, set it to **DNS only (grey cloud)** for now, so HTTPS certificates can be issued in Step 9. You can switch the proxy on later.

DNS can take a few minutes to update. Check it with:

```bash
ping hub.yourdomain.com
```

---

### Step 4 — First login and create your own user

```bash
ssh root@YOUR_DROPLET_IP
```

Update the system and create a non-root user:

```bash
apt update && apt upgrade -y
adduser jared
usermod -aG sudo jared
rsync --archive --chown=jared:jared ~/.ssh /home/jared
```

Open a **second terminal window** and confirm you can log in as the new user before going further:

```bash
ssh jared@YOUR_DROPLET_IP
```

From here on, always log in as `jared`, not root.

---

### Step 5 — Lock down the server

**Firewall** (only SSH, HTTP, and HTTPS allowed in):

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

**Disable root login and password login.** Open the SSH config:

```bash
sudo nano /etc/ssh/sshd_config
```

Find and set these lines (remove any `#` at the start):

```
PermitRootLogin no
PasswordAuthentication no
```

Save (Ctrl+O, Enter, Ctrl+X) and restart SSH:

```bash
sudo systemctl restart ssh
```

**Block repeated login attempts and turn on automatic security updates:**

```bash
sudo apt install -y fail2ban unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Choose **Yes** when asked.

**Add swap memory** (a safety net if the crawler spikes memory):

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

### Step 6 — Install Docker

```bash
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker jared
```

Log out and back in so the Docker permission takes effect, then check:

```bash
docker --version
docker compose version
```

---

### Step 7 — Get the code onto the server

Create a key that lets the server read (but not change) your private repository:

```bash
ssh-keygen -t ed25519 -C "query-rocket-droplet" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub
```

In GitHub: open the Query Rocket repo → **Settings → Deploy keys → Add deploy key**. Paste the key, name it "DigitalOcean hub", and leave **Allow write access** unchecked.

Tell the server to use that key for GitHub:

```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
```

Clone the repo (replace with your GitHub username):

```bash
cd ~
git clone git@github.com:YOUR_GITHUB_USERNAME/query-rocket.git
cd query-rocket
```

---

### Step 8 — Set up Google access (Search Console + GA4)

This is done once in the Google Cloud Console, using the Google account that has access to your clients' properties.

1. Go to **console.cloud.google.com** and create a project called **Query Rocket**.
2. Under **APIs & Services → Library**, enable:
   - Google Search Console API
   - Google Analytics Data API
   - Google Analytics Admin API (lets the hub list your GA4 properties)
3. Under **OAuth consent screen**:
   - User type: **Internal** if you use Google Workspace, otherwise **External**.
   - App name: Query Rocket. Add your email as support and developer contact.
   - If External, add your own Google account as a test user, then **publish the app to production**. Apps left in "Testing" mode lose their Google connection every 7 days. For your own use, you'll see an "unverified app" warning when you sign in; click **Advanced → Continue**.
4. Under **Credentials → Create credentials → OAuth client ID**:
   - Type: Web application.
   - Authorized redirect URI: `https://hub.yourdomain.com/google/callback/` (the exact path will be confirmed in the build).
5. Copy the **Client ID** and **Client secret** for the next step.

---

### Step 9 — Configure and launch

Create the settings file from the template in the repo:

```bash
cp .env.example .env
nano .env
```

Generate strong random values for the secret key and database password:

```bash
openssl rand -base64 48
```

Run it twice and use one for each. Fill in `.env`:

```
DOMAIN=hub.yourdomain.com
DJANGO_SECRET_KEY=paste-first-random-value
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=hub.yourdomain.com

POSTGRES_DB=queryrocket
POSTGRES_USER=queryrocket
POSTGRES_PASSWORD=paste-second-random-value

REDIS_URL=redis://redis:6379/0

ANTHROPIC_API_KEY=your-anthropic-key
GOOGLE_CLIENT_ID=from-step-8
GOOGLE_CLIENT_SECRET=from-step-8

AUTOPILOT_PAUSED=False
REPORT_EMAIL_TO=you@gearboostmarketing.com
DEFAULT_FROM_EMAIL=Query Rocket <reports@yourdomain.com>
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True
```

The email lines are for the Monday report. Any SMTP service works (Google Workspace, Mailgun, Postmark). Leave `EMAIL_HOST` blank to skip email for now.

Lock the file so only you can read it:

```bash
chmod 600 .env
```

Build and start everything:

```bash
docker compose up -d --build
```

The first build takes several minutes because it installs the browser for the crawler. Then set up the database and your login:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
docker compose exec web python manage.py createsuperuser
```

Then run the built-in tests. All 31 should pass in a few seconds; if any fail, send me the output before going further:

```bash
docker compose exec web python manage.py test core
```

Visit **https://hub.yourdomain.com**. Caddy (the web server in the stack) gets the HTTPS certificate automatically the first time the site is reached. Log in with the account you just created.

---

### Step 10 — Connect Google

In Query Rocket, open **Google data → Connect a Google account** and sign in with the account that can see your clients' Search Console and GA4. Then, on each site, click **Connect Google data** and pick its properties. The first sync pulls about four months of history.

---

### Step 11 — Install the plugin on each client site

For each client site:

1. In WordPress: **Plugins → Add New → Upload Plugin**, choose `query-rocket-connector.zip`, install, and activate.
2. Open **Settings → Query Rocket** and copy the connection key.
3. In Query Rocket, open the site, click **Connect WordPress**, paste the key, and connect.

Cloudways note: the plugin clears Breeze and Varnish after each change automatically. If a site sits behind Cloudflare with a strict firewall rule, allow `/wp-json/query-rocket/` for the hub's IP address.

Start with one site. Approve fixes by hand for a couple of weeks before turning on autopilot anywhere.

---

### Step 12 — Nightly database backups

Droplet backups are weekly. Add a nightly database backup so you never lose more than a day of fix history:

```bash
mkdir -p ~/backups
nano ~/backup.sh
```

Paste:

```bash
#!/bin/bash
cd ~/query-rocket
docker compose exec -T db pg_dump -U queryrocket queryrocket | gzip > ~/backups/qr-$(date +%F).sql.gz
find ~/backups -name "qr-*.sql.gz" -mtime +14 -delete
```

Make it run every night at 3 a.m. server time:

```bash
chmod +x ~/backup.sh
crontab -e
```

Add this line at the bottom:

```
0 3 * * * /home/jared/backup.sh
```

Optional but recommended later: copy these backups off the server to DigitalOcean Spaces, so a lost droplet doesn't take the backups with it.

---

### Emergency stop

To halt autopilot on every site at once, set `AUTOPILOT_PAUSED=True` in `.env`, then:

```bash
docker compose up -d
```

Every applied change can still be rolled back from the fix queue.

---

### Updating after new code is pushed

Every time we finish a phase or fix something:

```bash
cd ~/query-rocket
git pull
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
```

---

### Everyday commands

| Task | Command |
|---|---|
| See what's running | `docker compose ps` |
| Watch the live logs | `docker compose logs -f` |
| Logs for just the AI/crawl workers | `docker compose logs -f worker` |
| Restart everything | `docker compose restart` |
| Stop everything | `docker compose down` |
| Check disk space | `df -h` |
| Check memory | `free -h` |

---

### Troubleshooting

**Site won't load / no HTTPS.** Check DNS points to the droplet (`ping hub.yourdomain.com`), ports 80 and 443 are open (`sudo ufw status`), and Cloudflare proxy is off during first setup. Then check `docker compose logs caddy`.

**"Bad Request (400)".** The domain in `DJANGO_ALLOWED_HOSTS` doesn't exactly match the address you visited.

**Crawls fail or the server freezes.** Usually memory. Check `free -h`. Lower the number of crawler workers in `.env` or resize the droplet to 8 GB in DigitalOcean (Power off → Resize → Power on).

**Google connection stops working after a week.** The OAuth app is still in Testing mode. Publish it to production (Step 8, point 3) and reconnect.

**Locked out of SSH.** Use the **Recovery Console** in the DigitalOcean droplet page, which works even without SSH.

---

### Security notes

- Never publish the database or Redis ports in `docker-compose.yml`. Docker can open ports that bypass the firewall, so only Caddy should expose 80 and 443.
- Never commit `.env` to GitHub. The repo's `.gitignore` will exclude it.
- The WordPress plugin connects with its own per-site token, so the hub never stores client WordPress passwords.
- If a token or key is ever exposed, rotate it: the Anthropic key in the Anthropic console, Google credentials in Cloud Console, and plugin tokens from the hub dashboard.

---

### Reference: how the pieces fit

The build will include these files in the repo. They're shown here so you know what's running.

**docker-compose.yml (simplified)**

```yaml
services:
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    depends_on: [web]
    restart: unless-stopped

  web:
    build: .
    command: gunicorn queryrocket.wsgi:application --bind 0.0.0.0:8000 --workers 3
    env_file: .env
    depends_on: [db, redis]
    restart: unless-stopped

  worker:
    build: .
    command: celery -A queryrocket worker --concurrency 2 --loglevel info
    env_file: .env
    depends_on: [db, redis]
    restart: unless-stopped

  beat:
    build: .
    command: celery -A queryrocket beat --loglevel info
    env_file: .env
    depends_on: [db, redis]
    restart: unless-stopped

  db:
    image: postgres:16
    env_file: .env
    volumes: [pg_data:/var/lib/postgresql/data]
    restart: unless-stopped

  redis:
    image: redis:7
    restart: unless-stopped

volumes:
  pg_data:
  caddy_data:
```

**Caddyfile**

```
hub.yourdomain.com {
    reverse_proxy web:8000
}
```

| Service | Job |
|---|---|
| caddy | Web server, handles HTTPS certificates automatically |
| web | The Django dashboard and the API the WordPress plugin talks to |
| worker | Runs crawls, AI fix proposals, reviews, and Google data pulls in the background |
| beat | The scheduler: nightly crawls, 5 a.m. Google sync, 6 a.m. result checks, Monday 7 a.m. report |
| db | PostgreSQL, stores sites, pages, fixes, approvals, rollback history |
| redis | The job queue between web, beat, and worker |


---

## Part 4 — Your first client, start to finish

### 1. Add the site

1. Log in at `https://hub.yourdomain.com`.
2. On **Client sites**, enter the client name, the homepage URL exactly as it appears in the browser (https, with or without www, whichever the site actually uses), and a page limit (500 is fine for most). Click **Add and crawl**.
3. The site page shows a progress bar. Most small sites finish in 2 to 10 minutes.

### 2. Confirm the business profile

On the site page, the purple **What Query Rocket understood** card shows the business name, type, phone, address, hours, services, and areas the AI found.

1. Click **Review and confirm**.
2. Correct anything wrong. Check the phone and address carefully; they go into schema.
3. Uncheck **Serves customers locally** for online-only businesses.
4. Click **Save and confirm**. From now on, crawls never overwrite it.

### 3. Read the issues

**What needs attention** lists problems grouped by type, worst first. Red is critical, orange is a warning, purple is a notice. Click any group to see every page affected and the Google or Rank Math rule behind it. Click a page to see how it looks in Google, with pixel-width bars for the title and description.

### 4. Connect Google data

1. Click **Connect Google data** on the site page (the first time, it asks you to sign in with Google).
2. Pick the site's Search Console property and GA4 property. Click **Save and sync**.
3. Within a few minutes, **Search performance** shows clicks, impressions, position, organic sessions, and key events, plus **Closest wins**: searches the site ranks 5 to 20 for.

### 5. Connect WordPress

1. On the client's WordPress: **Plugins → Add New → Upload Plugin**, upload `query-rocket-connector.zip`, **Activate**.
2. Go to **Settings → Query Rocket** and copy the connection key.
3. In Query Rocket, click **Connect WordPress** on the site page, paste the key, click **Connect**.
4. It should say **Connected** and show Rank Math (or Yoast). It then reads each page's SEO fields in the background.

If it fails, see Troubleshooting in Part 7.

### 6. Generate your first fixes

1. Click **Fixes**, then **Find new fixes**. The AI writes up to 40 fixes, each checked by code and reviewed by a second AI. Give it a few minutes.
2. Each fix card shows **Now** and **Change to**, why, the rule it follows, how many checks it passed, and the reviewer's verdict.

### 7. Review fixes like an editor

For the first two weeks, review every fix yourself. This is also how you teach autopilot what to trust.

- **Approve** if you'd happily put it on the client's site today.
- **Edit** if it's close. Your edit is rechecked; if it fails a check (too wide, duplicate), you'll see why.
- **Reject** if it's wrong, bland, or not better than what's there.
- The **Rejected** tab shows fixes the checks or reviewer blocked, with the reason. You can approve one anyway if you disagree, as long as it passes the safety checks.

Approved fixes go to WordPress right away. Watch them move to **Approved and live**:
- **Live and verified** means Query Rocket loaded the real page and saw the change.
- **Applied, not verified** after a few hours means something on the page (theme, builder, cache) is overriding it. Check the page; roll back if needed.
- **Failed** shows the reason, e.g. someone edited that field by hand since the crawl, so Query Rocket refused to overwrite them.

### 8. Check the live site

After your first few fixes, open the client's pages in a private browser window, view the page source, and confirm the title and description changed. Check that nothing looks different to visitors (titles and descriptions don't change the visible page).

### 9. Roll back if you need to

Any live fix has a **Roll back** button. The previous value returns immediately. The client's WordPress also keeps its own log under **Settings → Query Rocket**.

---

## Part 5 — Your weekly routine (15 to 30 minutes)

1. **Monday:** read the weekly email. It shows fixes applied, rollbacks, results, what's waiting, and critical issues per site.
2. **Open the Fix queue** (left menu). The badge shows how many are waiting. Work through each site.
3. **Check "Applied, not verified"** fixes on the Approved tab. These usually point to a theme, builder, or cache issue on that site.
4. **Look at Closest wins** on your main clients. Those searches are where new content or page improvements pay off fastest.
5. **Glance at critical issues.** Broken links and pages returning errors often come from site edits by clients or other plugins.

Monthly: check your Anthropic Console usage against the spending limit, and your DigitalOcean backups.

---

## Part 6 — Rolling out autopilot safely

Autopilot is off for every site until you turn it on, and each fix type unlocks only after you've shown it's trustworthy.

**Unlock rules (across all your sites):**
- Image alt text, focus keywords, schema, and filling in *missing* titles and descriptions unlock at **10 approved at 90% or better**. Edits count against the score.
- Rewriting *existing* titles and descriptions unlocks at **20 approved at 95%**, and is a separate checkbox.
- Homepages, pinned pages, and broken links **always** wait for you.

**Suggested rollout:**
1. **Weeks 1 to 2:** approve by hand on your test client only. Aim for at least 10 image alt fixes and a handful of titles and descriptions.
2. **Week 3:** add 2 or 3 more clients. Keep approving by hand.
3. **Week 4:** on your test client, open **Autopilot**, turn it on for image alt text only, keep "Roll back automatically" checked.
4. **After a month of results:** add more fix types and more sites as the **Hurt results** column stays at zero.
5. **Rewriting existing titles** last, and only on sites where the 28-day results have been positive.

**Pinning pages:** to protect a page forever (a client's favorite landing page, a legal page), go to `https://hub.yourdomain.com/admin/`, open **Pages**, filter by site, tick **Pinned**, and save.

### Oxygen page builder edits

On Oxygen sites, alt text and links often live inside Oxygen's own data. To let Query Rocket edit them:

1. Approve at least 10 image alt fixes at 90%+ (anywhere).
2. On the client's WordPress: **Settings → Query Rocket → Allow Query Rocket to edit image alt text and links inside Oxygen page builder data**, then Save.
3. In Query Rocket: the site's **Connect WordPress** screen, click **Test connection**, then tick **Allow Query Rocket to edit Oxygen page data** and Save.

Test this first on an OXpress-built page you don't mind breaking. Approve one alt fix, check the page in Oxygen and on the live site, then try **Roll back** and confirm the page returns exactly to how it was. If Query Rocket says it can't find builder data on an Oxygen page, tell me the meta key OXpress writes to and it's a one-line addition.

---

## Part 7 — Troubleshooting

### Deploying

| Problem | Fix |
|---|---|
| `Permission denied (publickey)` when logging in | You're using a different computer or key. Use the droplet's **Recovery Console** in DigitalOcean, or add your key to `/home/jared/.ssh/authorized_keys`. |
| `permission denied` running docker | Log out and back in after Step 6 so the docker group applies. |
| Build fails partway | Run `docker compose up -d --build` again; network hiccups during the browser download are common. If it fails the same way twice, send me the last 30 lines. |
| Site won't load / no padlock | DNS not pointing at the droplet yet, Cloudflare proxy on (turn it grey), or ports closed. Check `ping hub.yourdomain.com`, `sudo ufw status`, `docker compose logs caddy`. |
| "Bad Request (400)" | `DJANGO_ALLOWED_HOSTS` in `.env` doesn't exactly match your address. Fix it, then `docker compose up -d`. |
| "Server Error (500)" | Run `docker compose logs web --tail 50` and send me the output. |
| Locked out of the dashboard after wrong passwords | Wait 15 minutes. It's the login protection. |

### Crawling

| Problem | Fix |
|---|---|
| Crawl fails: "Couldn't load any pages" | Site down, or a firewall blocking the hub. Whitelist the droplet's IP in Cloudways/Cloudflare/Wordfence. Your old data is kept. |
| Very few pages found | Check the homepage URL is the final address (https, www or not). No sitemap and few links also limits discovery. |
| Business profile looks wrong | Correct and confirm it on the profile screen. It won't be overwritten again. |
| "AI is off" banner | `ANTHROPIC_API_KEY` missing in `.env`. Add it, then `docker compose up -d`. |

### Google

| Problem | Fix |
|---|---|
| Sign-in error "redirect_uri_mismatch" | The redirect URI in Google Cloud must be exactly `https://hub.yourdomain.com/google/callback/` (with the slash). |
| Connection stops after 7 days | Your Google OAuth app is still in Testing. Publish it to production (deploy Step 8) and reconnect. |
| Property not in the list | That Google account can't see it. Get added as a user in the client's Search Console / GA4, then reload. |
| No data yet | Search Console data lags 2 to 3 days; brand-new properties may have none. |

### WordPress connection and fixes

| Problem | Fix |
|---|---|
| "Plugin endpoint not found" | Plugin not activated, or a security plugin blocks the REST API. Allow `/wp-json/query-rocket/`. |
| "Bad signature" | The key was regenerated or mistyped. Copy it again from Settings → Query Rocket. |
| "Request expired" | The client server's clock is off. Ask Cloudways support to sync time (NTP). |
| "The site redirects to…" | Update the site's homepage URL in Query Rocket (via `/admin/`, Sites) to the address it names. |
| Fix failed: field changed on the site | Someone edited that field by hand. Recrawl, and Query Rocket will propose again based on the new value. |
| Applied but not verified | A theme, page builder, or other SEO plugin is overriding Rank Math's output, or a CDN cache hasn't cleared. Check the live page source. |
| Link fix: "isn't in this page's editable content" | The link lives in a menu, footer, widget, or Oxygen data. Fix it by hand, or enable Oxygen edits (Part 6). |

### Anything else

Send me: what you clicked, what you expected, the exact message, and if possible the output of `docker compose logs --tail 50`.

---

## Part 8 — Keeping it healthy

### Costs to expect

| Item | Monthly |
|---|---|
| DigitalOcean droplet (2 vCPU, 4 GB) + backups | about $29 |
| Anthropic API, 10 client sites | about $15 to $25 first month, then $5 to $15 |
| Google APIs | free |

Lower `FIX_MAX_PER_RUN` in `.env` if AI costs ever creep up.

### Safety switches

- **Stop all autopilot now:** set `AUTOPILOT_PAUSED=True` in `.env`, then `docker compose up -d`.
- **Protect a page:** pin it in `/admin/`.
- **Undo a change:** Roll back in the fix queue.
- **Disconnect a site:** deactivate the plugin on that site. Titles, descriptions, and focus keywords already written stay in Rank Math; alt text overrides and schema added by the plugin go away with it.

### Updating Query Rocket

When I send you an updated version: replace the files in your `query-rocket` folder, commit and push to GitHub, then on the server:

```bash
cd ~/query-rocket
git pull
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py test core
```

The last command runs the 31 built-in tests (about 5 seconds). If they all pass, the update is good. If the plugin changed, upload the new `query-rocket-connector.zip` on each client site (WordPress replaces the old version and keeps its settings).

### Things never to do

- Never commit `.env` to GitHub or share it. It holds your keys.
- Never change `DJANGO_SECRET_KEY` after launch. Stored Google sign-ins and plugin keys are encrypted with it and would need to be reconnected.
- Never publish the database or Redis ports in `docker-compose.yml`.

---

## Deploy week checklist

**Before deploy day**
- [ ] Hub address chosen
- [ ] Anthropic API key created, credit added, spend limit set
- [ ] Code in a private GitHub repo (no `.env` in it)
- [ ] First test client chosen
- [ ] Writing guide read and adjusted

**Deploy day**
- [ ] Droplet created, DNS pointing at it
- [ ] Server locked down, Docker installed
- [ ] Code cloned, `.env` filled in
- [ ] Google Cloud project, APIs enabled, OAuth client created and published
- [ ] Hub running at `https://hub.yourdomain.com`, you can log in
- [ ] `docker compose exec web python manage.py test core` passes
- [ ] Backups scheduled

**First client**
- [ ] Site added and crawled
- [ ] Business profile confirmed
- [ ] Google data connected and showing
- [ ] Plugin installed and connected
- [ ] First fixes reviewed; a few approved and verified on the live site
- [ ] One rollback tested

