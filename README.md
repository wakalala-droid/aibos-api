# AI-BOS — FastAPI Backend

## The two repos, in one picture

```
YOUR COMPUTER
│
├── aibos-api/          ← THIS FOLDER  →  deploy to Railway
│   ├── engine.py       ← COPY from your Streamlit project
│   ├── main.py         ← already written (wraps engine.py)
│   ├── requirements.txt
│   └── railway.toml
│
└── aibos/              ← Next.js frontend  →  deploy to Vercel
    └── lib/api.ts      ← calls Railway URL
```

## Step 1 — Copy engine.py here

```
cp /path/to/your/streamlit-project/engine.py ./engine.py
```
That is literally the only file you move. Do not edit it.

## Step 2 — Test locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in your keys
uvicorn main:app --reload --port 8000
```

Visit http://localhost:8000/health → should return {"status":"ok"}
Visit http://localhost:8000/docs   → interactive API explorer (free!)

## Step 3 — Deploy to Railway

```bash
# Install Railway CLI once
npm install -g @railway/cli

railway login
railway init          # creates a new Railway project
railway up            # deploys — takes ~2 minutes
railway domain        # copy the URL e.g. https://aibos-api.up.railway.app
```

## Step 4 — Set environment variables on Railway

Go to Railway dashboard → your project → Variables tab. Add:

| Key                  | Value                                     |
|----------------------|-------------------------------------------|
| GROQ_API_KEY         | gsk_xxx (same key from your Streamlit app)|
| SUPABASE_URL         | https://your-project.supabase.co          |
| SUPABASE_SERVICE_KEY | eyJ... (service key, NOT anon key)        |
| NEXT_PUBLIC_APP_URL  | https://your-app.vercel.app               |

## Step 5 — Connect Next.js to the API

In your Vercel project → Settings → Environment Variables, add:

```
NEXT_PUBLIC_API_URL = https://aibos-api.up.railway.app
```

The Next.js app already has `lib/api.ts` which reads this variable.

## That's it. Done.

Railway auto-redeploys every time you `git push`.
