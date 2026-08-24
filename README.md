# senza-agent

Out-of-the-box general-purpose AI agent built on the [Senza SDK](https://github.com/oh-my-harness/Senza).

## Quick Start

```bash
# 1. Build & install (creates venv, builds Senza wheel, installs senza-agent)
./scripts/dev_setup.sh
source .venv/bin/activate

# 2. Set your LLM credentials
export OPENAI_API_KEY=sk-...
export OPENAI_API_BASE=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o

# 3. Run
senza-agent --nostop          # interactive CLI
senza-agent --web             # web dashboard (http://localhost:8090)
cd desktop && npm start       # desktop app (Electron)
```

## Architecture

```
┌─ senza-agent (this repo) ──────────────────────────┐
│                                                     │
│  Python product layer:                              │
│    ├── cli.py          — CLI entry point            │
│    ├── agent.py        — agent assembly (tools)     │
│    ├── behavior/       — advisor, acceptance gate   │
│    ├── tools/          — fs, web, code_exec, graph  │
│    ├── webserver/      — aiohttp dashboard backend  │
│    └── desktop/        — Electron shell             │
│                                                     │
│  Senza SDK (runtime):                               │
│    agent loop · compression · LLM client · JSON     │
│                                                     │
└─────────────────────────────────────────────────────┘
```

## Modes

| Mode | Command | Description |
|------|---------|-------------|
| CLI | `senza-agent "do something"` | Single task |
| Interactive | `senza-agent --nostop` | REPL, one task at a time |
| Web | `senza-agent --web` | Browser dashboard at :8090 |
| Desktop | `cd desktop && npm start` | Electron app |
