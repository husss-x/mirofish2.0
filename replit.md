# MiroFish

MiroFish is a next-generation AI prediction and simulation engine powered by multi-agent technology. Users upload seed information (news, reports, stories), and the system automatically constructs a digital world where thousands of intelligent agents (with independent personalities and long-term memory) interact on simulated social platforms (Twitter, Reddit) to forecast future trajectories or explore "what if" scenarios.

## Architecture

- **Frontend**: Vue 3 + Vite, runs on port 5000
- **Backend**: Flask (Python 3.11+), runs on port 5001
- **Package manager (backend)**: `uv`
- **Package manager (frontend/root)**: `npm`

## Project Layout

```
/
├── frontend/          # Vue 3 + Vite frontend
│   ├── src/
│   │   ├── api/       # Axios-based API client (proxies to /api -> backend)
│   │   ├── components/# UI components for the 5-step workflow
│   │   └── views/     # Main page layouts
│   └── vite.config.js # Dev server on port 5000, proxies /api to port 5001
├── backend/           # Flask Python backend
│   ├── app/
│   │   ├── api/       # Flask Blueprints (graphrag, simulation, report)
│   │   ├── services/  # Core business logic
│   │   ├── models/    # Data persistence
│   │   └── config.py  # Configuration (reads from .env)
│   ├── scripts/       # Standalone OASIS simulation scripts
│   └── run.py         # Flask app entry point (port 5001)
└── static/            # Project assets and screenshots
```

## Configuration

Environment variables (set via Replit Secrets or `.env` at project root):

| Variable | Description | Required |
|---|---|---|
| `LLM_API_KEY` | API key for LLM provider (OpenAI format) | Yes |
| `LLM_BASE_URL` | LLM API base URL (default: OpenAI) | No |
| `LLM_MODEL_NAME` | Model name (default: gpt-4o-mini) | No |
| `ZEP_API_KEY` | Zep Cloud API key for agent memory | Yes |

## Development

### Frontend only (this workflow):
```
cd frontend && npm run dev
```

### Backend (separate terminal):
```
cd backend && uv run python run.py
```

## Workflow

The "Start application" workflow runs the frontend on port 5000 (webview). The backend must be started separately and requires `LLM_API_KEY` and `ZEP_API_KEY` to be set.

## Key Features

1. **Graph Building**: Extracts entities from uploaded documents and builds a GraphRAG in Zep
2. **Environment Setup**: Generates detailed agent personas based on extracted entities
3. **Simulation**: Runs parallel social media simulations (OASIS) where agents interact
4. **Report Generation**: AI "Report Agent" analyzes outcomes to produce prediction reports
5. **Interaction**: Users can chat with any agent in the simulated world
