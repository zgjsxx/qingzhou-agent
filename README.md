# xu-agent — Personal AI Assistant

## Project Structure

```
xu-agent/
├── frontend/              # Frontend (Next.js + agent-chat-ui)
│   ├── src/               # UI components & pages
│   ├── .env.example       # Frontend env template
│   └── package.json
├── backend/              # Backend (LangGraph server)
│   ├── src/
│   │   ├── agent.py      # Agent graph definition (entry point)
│   │   └── tools.py      # Tool definitions
│   ├── langgraph.json    # LangGraph server config
│   ├── requirements.txt  # Python dependencies
│   ├── .env.example      # Backend env template
│   └── .gitignore
└── README.md
```

## Quick Start

### 1. Start the Backend

```bash
cd backend

# Create .env from template and fill in your API key
cp .env.example .env

# Install LangGraph CLI (requires Python 3.11+)
pip install langgraph-cli

# Install dependencies
pip install -r requirements.txt

# Start the LangGraph dev server on port 2024
langgraph dev
```

### 2. Start the Frontend

```bash
cd frontend

# Create .env from template
cp .env.example .env

# Install dependencies
pnpm install

# Start the dev server
pnpm dev
```

Then open `http://localhost:3000` in your browser.

## Architecture

- **Backend**: LangGraph Server exposes the agent via a standard REST API on `localhost:2024`. The agent uses `create_agent` with function-calling tools.
- **Frontend**: `agent-chat-ui` connects to the LangGraph Server API, handles streaming responses, tool calls, and chat history.

## Customization

- Add new tools in `backend/src/tools.py` and import them in `agent.py`
- Change the LLM model in `backend/src/agent.py` (supports any OpenAI-compatible API)
- Modify the system prompt in `agent.py` to change the agent's personality
