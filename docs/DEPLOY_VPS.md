# Hosting RegCompass on a rented server

This guide puts one RegCompass behind a username and password and HTTPS on a rented Linux
server, so a team or a reviewer can open it from a browser. The local path in the root
`README.md` (`docker compose up`, no login) is unchanged; everything here is added on top
of it by one override file, `docker-compose.vps.yml`.

What the override does:

- The app asks for a login: a browser that is not signed in is sent to the RegCompass
  sign-in page (`/login`), and a successful sign-in lasts 12 hours on that browser (a
  signed cookie; "Log out" in the header ends it sooner, and restarting the app signs
  everyone out). Default: user `admin`, password `admin123`. **Change the password**
  (step 6) before anyone else has the address.
- "Log out" ends the session in that browser only. Restarting the app signs every browser
  out, because a session is a signed token the browser holds, not a record the server
  keeps. Wrong passwords are answered one at a time after a short pause, so guessing is
  slow; that is no substitute for replacing the default `admin123`.
- A Caddy service answers on ports 80 and 443 and gets and renews an HTTPS certificate by
  itself.
- The app's port 8000 and Ollama's port 11434 are not published: every request goes
  through Caddy and the login. Only `/api/status` answers without a login (the health
  check uses it; it reports the server's state: whether a Run is going, whether the login is
  on, the Economy list and its database and output paths, none of the Corpus or the
  Mappings), beside the sign-in page itself and the `/api/login` call it makes.

## 1. The server

- Ubuntu 24.04 LTS
- 4 vCPU, 8 GB RAM, 40 GB disk (the image is about 2.6 GB, the embedder about 1.2 GB,
  the prepared database about 640 MB once loaded)
- SSH access as a user with `sudo`

## 2. Install Docker

Docker's official convenience script installs Docker Engine and the Compose plugin:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker "$USER"     # run docker without sudo
```

Log out and back in, then check: `docker compose version` must say 2.24 or newer (the
override uses `!reset`, which older Compose does not understand).

## 3. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable        # answer y
```

Ports Docker publishes are opened by Docker itself, ahead of ufw. That is why the override
publishes nothing but Caddy's 80 and 443. If the provider has its own firewall (a security
group in its web console), open 22, 80 and 443 there too.

## 4. A domain name (for HTTPS)

Create an **A record** for a name you control (for example `regcompass.example.org`) that
points at the server's public IPv4 address, and wait until `dig +short
regcompass.example.org` prints that address. Caddy needs this before its first start to
get the certificate.

No domain yet? Two fallbacks:

- `<server-ip>.sslip.io` (for example `203.0.113.7.sslip.io`) is a public name that already
  resolves to that address; use it as the domain and HTTPS works the same way.
- Leave the domain unset and the site is plain HTTP at `http://<server-ip>`. The login
  still works, but the password crosses the internet unencrypted: use this only to try the
  server out.

## 5. Get the code

```bash
git clone https://github.com/Ryannurtanio/regcompass-final.git regcompass
cd regcompass
```

## 6. Settings: the `.env` file

Create `.env` in the `regcompass` directory, beside `docker-compose.yml`. Compose reads it
by itself; nothing else needs to be exported.

```bash
cat > .env <<'CONF'
COMPOSE_FILE=docker-compose.yml:docker-compose.vps.yml
REGCOMPASS_DOMAIN=regcompass.example.org
REGCOMPASS_AUTH_USER=admin
REGCOMPASS_AUTH_PASSWORD=choose-a-long-password-here   # plain ASCII; the user name has no colon
# Optional: lets the hosted copy start new Runs on Engine A or Engine B.
# Without it the prepared Runs are all still reviewable.
# OPENROUTER_API_KEY=sk-or-...
CONF
chmod 600 .env
```

- `COMPOSE_FILE` makes every `docker compose` command below use both files, so no `-f` is
  needed. Without that line, write `docker compose -f docker-compose.yml -f
  docker-compose.vps.yml ...` every time.
- `REGCOMPASS_DOMAIN`: the name from step 4. Leave the line out for the plain HTTP fallback.
- **Change the password.** Without the two `REGCOMPASS_AUTH_` lines the override falls back
  to `admin` / `admin123`, which anyone can guess.
- A key in `.env` stays on this server; it is never written into the image.

## 7. Load the prepared database

Before the first start, fill the data volume with the pre-run Runs (download, SHA-256
check, unpack; details in [RELEASE_DATA.md](RELEASE_DATA.md)):

```bash
docker compose run --rm --no-deps regcompass-load
```

That reads the download address and SHA-256 from `config/prepared_data.yaml`. If they are
still blank (a copy taken before the release was published), copy the archive to the server
yourself and name it and its SHA-256 on the command line. The file must be mounted into the
container, because the loader runs there:

```bash
# on your own machine
scp regcompass-prepared-<date>.tar.gz <user>@<server-ip>:~/regcompass/
# on the server, in ~/regcompass
docker compose run --rm --no-deps \
  -v "$PWD/regcompass-prepared-<date>.tar.gz:/tmp/prepared.tar.gz:ro" \
  regcompass-load regcompass load-data --url /tmp/prepared.tar.gz --sha256 <sha256>
```

The SHA-256 is the first field of the `.sha256` file packed beside the archive.

## 8. Start

```bash
docker compose up -d --build
docker compose ps
```

The first start builds the image and pulls the embedder (`bge-m3`); allow several minutes.
The app is ready when `docker compose ps` shows `regcompass-app` as `healthy`. Then open
`https://regcompass.example.org` (or `http://<server-ip>`): the sign-in page opens, and after
signing in the browser goes on to the page it was asked for.

Check from the server itself:

```bash
curl -s https://regcompass.example.org/api/status             # open: JSON
curl -s -o /dev/null -w '%{http_code}\n' https://regcompass.example.org/api/runs   # 401
curl -s -u "$REGCOMPASS_AUTH_USER:$REGCOMPASS_AUTH_PASSWORD" https://regcompass.example.org/api/runs      # JSON
```

## Changing the password later

Edit `REGCOMPASS_AUTH_PASSWORD` in `.env`, then `docker compose up -d`. Compose recreates
the app container with the new value. The restart also signs every browser out, so the
next visit asks for the new password.

## Backups

The database and the stored documents live in the named volume `regcompass_regcompass-data`
(the prefix is the directory name; `docker volume ls` shows it).

The database alone, while the app runs (SQLite's own online backup):

```bash
mkdir -p backups
docker compose exec regcompass python -c "import sqlite3; sqlite3.connect('/data/regcompass.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp regcompass:/data/backup.db "backups/regcompass-$(date +%F).db"
docker compose exec regcompass rm /data/backup.db
```

The whole volume (database and documents), with the app stopped:

```bash
mkdir -p backups
docker compose stop regcompass
docker run --rm -v regcompass_regcompass-data:/data -v "$PWD/backups":/backup \
  alpine tar czf "/backup/regcompass-data-$(date +%F).tar.gz" -C /data .
docker compose start regcompass
```

Copy the `backups/` directory off the server.

## Updating

```bash
git pull
docker compose up -d --build
```

The data volume is kept. A newer prepared database is loaded with the app stopped:
`docker compose stop regcompass`, then `docker compose run --rm --no-deps regcompass-load
regcompass load-data --force` (it replaces what is there, your Review Decisions included),
then `docker compose start regcompass`. A copied archive takes the same `-v`, `--url` and
`--sha256` as in step 7, plus `--force`.

## Troubleshooting

- **No certificate, the browser warns or cannot connect.** `docker compose logs caddy`
  says why. Usually the A record does not point at this server yet, or port 80 or 443 is
  closed at the provider's firewall. Fix it and `docker compose restart caddy`.
- **502 Bad Gateway.** Caddy is up but the app is not ready: on the first start it waits
  for the embedder pull. `docker compose ps` and `docker compose logs regcompass`.
- **The app will not start and its log says `login is half set`.** One of the two
  `REGCOMPASS_AUTH_` variables is set and the other is empty. Set both.
- **The sign-in page says "Wrong user name or password".** Both are case-sensitive.
  `docker compose logs regcompass` shows `login on` at start when the login is active.
- **Signing in succeeds but the sign-in page comes straight back.** The browser refused
  the session cookie. On HTTPS the cookie is marked Secure (Caddy says the request was
  HTTPS), so open the site by its `https://` address; check that the browser allows
  cookies for it.
- **Scripts and curl.** They skip the page: send the same pair as an HTTP Basic header
  (`curl -u "$REGCOMPASS_AUTH_USER:$REGCOMPASS_AUTH_PASSWORD" ...`).
- **Out of disk.** `docker system df`; `docker image prune` removes old builds after an
  update.
- **Stop everything:** `docker compose down` (keeps the data). `docker compose down -v`
  also deletes the data volume and every Run in it.
