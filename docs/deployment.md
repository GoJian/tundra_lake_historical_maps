# Deployment

Target: a **self-hosted Linux server with an NVIDIA GPU** (for SAM segmentation)
and the NVIDIA Container Toolkit installed. The stack is two services — `api`
(FastAPI + titiler, GPU) and `web` (nginx serving the built React app and proxying
the API).

## 1. Transfer the code

Push to GitHub, then clone on the server:

```bash
# on your workstation (first time)
cd tundra-portal
git init && git add . && git commit -m "Initial commit"
git branch -M main
git remote add origin git@github.com:<you>/tundra-portal.git
git push -u origin main

# on the server
git clone git@github.com:<you>/tundra-portal.git && cd tundra-portal
```

## 2. Transfer the data (separately)

Data is gitignored (see [data-layout.md](data-layout.md)). Copy it to the server's
data volume and point the app at it, e.g.:

```bash
rsync -aP /local/tundra-data/  server:/srv/tundra-data/
rsync -aP /local/tundra-maps/  server:/srv/tundra-maps/
# on the server, from the repo root:
ln -s /srv/tundra-data data
ln -s /srv/tundra-maps map
```

(The SAM weights auto-download on first use, so they need not be transferred.)

## 3a. Run with Docker Compose (recommended)

```bash
cp .env.example .env          # set TUNDRA_API_KEY etc.
# point compose at the host data volume:
export TUNDRA_DATA_HOST=/srv/tundra-data
TUNDRA_API_KEY=<secret> docker compose up --build -d
```

- Web portal: `http://<server>:8080`
- API/docs:   `http://<server>:8000/docs`

`docker-compose.yml` mounts `${TUNDRA_DATA_HOST}` → `/data` and `./map` → `/app/map`,
and reserves one GPU for the `api` service. The `api` image is built from
`services/api/Dockerfile` (PyTorch+CUDA base + GDAL); `web` from `web/Dockerfile`.

## 3b. Run without Docker (systemd)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd web && npm ci && npm run build && cd ..     # static build in web/dist
```

Serve `web/dist` behind nginx (see `web/nginx.conf` for the proxy rules) and run
the API under a process manager:

```ini
# /etc/systemd/system/tundra-api.service
[Service]
WorkingDirectory=/opt/tundra-portal
EnvironmentFile=/opt/tundra-portal/.env
ExecStart=/opt/tundra-portal/.venv/bin/uvicorn services.api.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
```

> Keep `--workers 1`: segmentation jobs are held in-process and serialized on the
> GPU (see `services/api/jobs.py`). For horizontal scaling, move the job store to
> Redis + a task queue.

## 4. Production notes

- **Auth**: set `TUNDRA_API_KEY`; the frontend sends it via `VITE_API_KEY` (build arg).
- **HTTPS**: terminate TLS at a reverse proxy (nginx/Caddy/Traefik) in front of `web`.
- **Persistence**: the composite cache and SAM weights live under the data volume,
  so they survive container restarts.
- **CORS**: currently open (`*`) for dev — restrict `allow_origins` in
  `services/api/main.py` for production.
