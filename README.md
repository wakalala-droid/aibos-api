# AI-BOS — FastAPI Backend

## The two repos, in one picture

```
YOUR COMPUTER
│
├── aibos-api/          ← THIS FOLDER  →  deploy to Render
│   ├── main.py         ← the API
│   ├── engine*.py      ← the analysis engines
│   ├── requirements.txt
│   └── render.yaml     ← Render reads this
│
└── aibos/              ← Next.js frontend  →  deploy to Vercel
    └── lib/api-base.ts ← the one place the backend address is decided
```

## Run it on your own machine

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in your keys
uvicorn main:app --reload --port 8000
```

- <http://localhost:8000/health> → `{"status":"ok", …}`
- <http://localhost:8000/docs> → interactive API explorer

Use a fresh virtual environment. `requirements.txt` pins `starlette` on purpose:
without the pin, an environment that already has a newer one makes `import main`
fail deep inside `fastapi/routing.py` on `on_startup`, which reads exactly like a
bug in this code and is not.

## Deploy to Render

**This used to be Railway. Railway no longer has a free tier**, the app there was
removed, and its address now answers `Application not found`. Render still has a
genuinely free web service and does not ask for a card.

1. render.com → **New +** → **Blueprint**
2. Point it at this repository. It reads `render.yaml`.
3. Render asks for every variable marked `sync: false` in that file. Those are
   the secrets, deliberately not written down in a public repository.

### The four it will not work without

| Variable | Where to get it |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | same page → **`service_role`** key, *not* the anon key |
| `GROQ_API_KEY` | console.groq.com, free |
| `ALLOWED_ORIGINS` | your Vercel address, comma separated, **no trailing slash** |

`SUPABASE_SERVICE_KEY` once held the anon key by mistake and every write silently
failed the database's security rules — the API looked healthy and saved nothing.
Check which of the two you copied.

`ALLOWED_ORIGINS` has to match what the browser puts in the address bar exactly.
Get it wrong and the site loads normally while every button fails, because the
browser blocks the call before it is sent. It is nearly always a trailing slash,
or `www` against no `www`.

Worth setting at the same time: `SUPABASE_JWT_SECRET` (lets the API check a login
itself instead of asking Supabase every request), `PUBLIC_APP_URL`, and
`CRON_SECRET` (must match the one on Vercel, or the nightly brief is refused).
Everything else in `render.yaml` is per-feature: leave it unset and that feature
is simply off.

### What the free tier costs you

The service **sleeps after 15 minutes with no traffic**, and the next request
waits 40–60 seconds while pandas, numpy and reportlab load. Everything after that
is normal speed until it goes quiet again.

To avoid it, point any uptime checker at `/health` every 10 minutes. Render's free
allowance is 750 instance-hours a month against a 730-hour month, so **one**
service kept awake around the clock still fits inside free. A second would not.

## Connect the front end

Vercel → the `aibos` project → Settings → Environment Variables:

```
NEXT_PUBLIC_API_URL = https://your-service.onrender.com
```

No trailing slash. **Then redeploy** — Next.js bakes `NEXT_PUBLIC_*` values into
the build, so changing one without a redeploy changes nothing and looks like the
save did not take.

There is no longer a fallback address in the code. If that variable is missing the
app returns 503 and says so, rather than quietly calling a host that is not there,
which is what the old hardcoded Railway fallback did.

## Checking a deploy actually landed

```bash
curl https://your-service.onrender.com/health
```

```json
{
  "status": "ok",
  "supabase_configured": true,
  "host": "render",
  "build_sha": "a74fe56",
  "expects_migration": 26
}
```

- `supabase_configured: false` → the API cannot see the database. `SUPABASE_URL`
  or `SUPABASE_SERVICE_KEY` is wrong or missing.
- `build_sha` is the commit actually serving. Compare it against what you pushed:
  equal means the new code is live, different means the deploy failed and the old
  version is still up. That has happened before and looks identical to success
  from the outside, which is why it is reported here.
- `host` says which platform it thinks it is on — useful while two are briefly
  live during a move.

## Rebuilding the database

See `docs/RESTORE.md` in the `aibos` repository. `supabase/REBUILD_ALL.sql` there
puts the whole schema back in one paste.
