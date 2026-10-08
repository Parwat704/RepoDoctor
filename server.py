import os
import re
import shutil
import subprocess
import tempfile

from flask import Flask, jsonify, request, send_from_directory

from analyzer import analyze
from gemma import GemmaError, analyze_with_gemma, chat_with_gemma
app = Flask(__name__)

GITHUB_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
CLONE_TIMEOUT = 90   # seconds
MAX_REPO_MB = 200


class AnalysisError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def error(message, status=400):
    return jsonify({"success": False, "error": message}), status


def clone_repo(url, dest):
    """Shallow clone. The URL is already validated and passed as a list item, never through a shell."""
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", "--", url, dest],
            capture_output=True, text=True, timeout=CLONE_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        raise AnalysisError("Git is not installed on the server. Install Git and restart RepoDoctor.", 500)
    except subprocess.TimeoutExpired:
        raise AnalysisError("Cloning took too long. The repository may be too large.", 504)
    if result.returncode != 0:
        app.logger.warning("git clone failed: %s", result.stderr.strip())
        err = result.stderr.lower()
        if "not found" in err or "could not read username" in err or "authentication failed" in err:
            raise AnalysisError("Repository not found. Make sure it exists and is public.", 404)
        raise AnalysisError("Could not clone the repository. Please try again.", 502)


def repo_size_mb(path):
    total = 0
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total / 1_000_000


def remove_tree(path):
    def force(func, p, _exc):  # Windows marks .git files read-only
        os.chmod(p, 0o700)
        func(p)
    try:
        shutil.rmtree(path, onexc=force)
    except TypeError:  # Python < 3.12
        shutil.rmtree(path, onerror=force)


# Only these three files are served, so server.py and .env can never be downloaded.
@app.get("/")
def index():
    return send_from_directory(".", "index.html")


@app.get("/style.css")
def css():
    return send_from_directory(".", "style.css")


@app.get("/app.js")
def js():
    return send_from_directory(".", "app.js")


@app.post("/api/analyze")
def analyze_route():
    body = request.get_json(silent=True) or {}
    match = GITHUB_URL.match(str(body.get("repo_url", "")).strip())
    if not match:
        return error("That doesn't look like a GitHub repository URL. Use the form https://github.com/owner/repo.")
    owner, repo = match.groups()
    url = f"https://github.com/{owner}/{repo}"

    tmp = tempfile.mkdtemp(prefix="repodoctor_")
    try:
        dest = os.path.join(tmp, "repo")
        clone_repo(url, dest)
        if repo_size_mb(dest) > MAX_REPO_MB:
            raise AnalysisError(f"This repository is larger than {MAX_REPO_MB} MB, which is over the MVP limit.")
        repository, checks = analyze(dest, f"{owner}/{repo}")
        if repository["file_count"] == 0:
            raise AnalysisError("This repository is empty. There is nothing to analyze.")
        ai_error = None
        try:
            ai_analysis = analyze_with_gemma(repository, checks)
        except GemmaError as e:
            app.logger.warning("Gemma failed: %s", e.message)
            ai_error = e.message
            ai_analysis = {"summary": f"AI analysis unavailable. {e.message} The deterministic evidence is still available below.",
                           "findings": []}
        return jsonify({"success": True, "repository": repository, "checks": checks,
                        "ai_analysis": ai_analysis, "ai_error": ai_error})
    except AnalysisError as e:
        return error(e.message, e.status)
    except Exception:
        app.logger.exception("analysis failed")
        return error("Something went wrong while analyzing this repository.", 500)
    finally:
        remove_tree(tmp)

@app.post("/api/chat")
def chat_route():
    body = request.get_json(silent=True) or {}

    question = str(body.get("question", "")).strip()
    analysis = body.get("analysis")
    history = body.get("history", [])

    if not question:
        return error("Please enter a question.")

    if not isinstance(analysis, dict):
        return error("No repository analysis was provided.")

    if not isinstance(history, list):
        history = []

    history = history[-10:]

    try:
        answer = chat_with_gemma(
            question,
            analysis,
            history
        )

        return jsonify({
            "success": True,
            "answer": answer
        })

    except GemmaError as e:
        app.logger.warning("Gemma chat failed: %s", e.message)
        return error(e.message, 502)

    except Exception:
        app.logger.exception("chat failed")
        return error("Something went wrong while asking RepoDoctor.", 500)


if __name__ == "__main__":
    app.run(debug=True, port=5000)



