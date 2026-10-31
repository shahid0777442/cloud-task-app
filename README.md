# Cloud Task Manager (Flask + PostgreSQL + S3)

## Run locally
```bash
docker compose up -d                 # PostgreSQL
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && set -a && source .env && set +a
python app.py                        # http://127.0.0.1:8000
```
Endpoints: `/` app, `/health` liveness, `/health/db` DB readiness.

## Deploy on AWS (maps to the guide)
| Guide step | Use this file |
|---|---|
| Step 6 IAM role | `deploy/iam-policy.json` (+ CloudWatchAgentServerPolicy) |
| Step 8 install | `git clone`, `pip install -r requirements.txt` |
| Step 9 env vars | `deploy/myapp.env.example` -> `/etc/myapp.env` |
| Step 10 service | `deploy/myapp.service` -> `/etc/systemd/system/` |
| Step 11 Nginx | `deploy/nginx-myapp.conf` -> `/etc/nginx/sites-available/myapp` |

Secret format in Secrets Manager (`prod/myapp/db`):
`{"username":"appuser","password":"..."}`

The app creates the `tasks` table automatically on first start.
Set the Load Balancer / uptime check path to `/health`.
