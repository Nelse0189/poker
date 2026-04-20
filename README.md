# Poker optimal play (NLHE)

Vite + React frontend and FastAPI backend using [pokerkit](https://github.com/uoftcprg/pokerkit) for postflop Monte Carlo equity. Preflop uses chart-based recommendations; postflop combines equity with pot odds, SPR, fold equity, and implied odds.

## Run locally

**Backend** (Python 3.11+):

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**Frontend**:

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). The dev server proxies `/api` to the backend.

## License

MIT (match pokerkit and your preferences). Add a `LICENSE` file if you need explicit terms.
