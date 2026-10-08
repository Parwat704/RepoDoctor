# RepoDoctor 🩺

**Diagnose. Explain. Fix.**

RepoDoctor is an AI-powered tool that analyzes public GitHub repositories, finds real project issues, and explains how to fix them.

Instead of sending an entire repository blindly to an AI, RepoDoctor first collects evidence using deterministic checks and then uses **Gemma 4** to reason over that evidence.

## How it works

```text
GitHub URL
    ↓
Clone Repository
    ↓
Deterministic Analysis
    ↓
Evidence
    ↓
Gemma 4
    ↓
Findings + Explanations
    ↓
AI Chatbot
```

### What it checks

* Missing dependencies and configuration
* README/documentation issues
* Missing tests
* Environment variables
* Package/config inconsistencies
* Potential security issues
* Large or suspicious files
* TODO/FIXME markers

## Tech Stack

* Python + Flask
* HTML / CSS / JavaScript
* Gemma 4
* GitHub

## Run locally

```bash
git clone https://github.com/Parwat704/RepoDoctor.git
cd RepoDoctor

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

Create a `.env` file:

```text
GEMMA_API_KEY=your_api_key_here
```

Then:

```bash
python server.py
```

Open `http://localhost:5000`.

## Why RepoDoctor?

**AI shouldn't guess what's wrong with your codebase.**

RepoDoctor collects real evidence first, then asks AI to explain what that evidence means.

---

Built for **Hacktoberfest Hack Day 2026**.
