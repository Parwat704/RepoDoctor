"""Gemma 4 integration via the Gemini API. The API key stays on the server; the browser never sees it."""
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

MODEL = "gemma-4-26b-a4b-it"   # change this one line to switch models
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT = 90  # seconds

SEVERITIES = ("high", "medium", "low")
CATEGORIES = ("build", "testing", "documentation", "security", "code_quality")


class GemmaError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _load_dotenv():
    """Tiny .env reader so we don't need an extra package. Real environment variables win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

SYSTEM_PROMPT = f"""You are RepoDoctor, a repository diagnostic engine. You receive EVIDENCE: JSON produced by deterministic checks on a cloned GitHub repository. Reason ONLY from that evidence. You never see the repository's files.

How to work:
1. Start from "signals": a ranked list of candidate issues computed by deterministic code. Each has kind, basis, severity_hint, summary and facts. The facts are ready-to-cite evidence strings. "checks" and "repository" hold the underlying data.
2. Turn the meaningful signals into findings. Merge closely related signals. Skip signals that are not worth reporting. You may add a finding that is not in "signals" only if "checks" directly supports it.
3. Priority order: (a) kind "risk" and "inconsistency" signals, where two parts of the repository disagree or something referenced cannot be found; (b) "quality" signals; (c) "absence" signals last. Do not fill the list with "missing X" findings when real inconsistencies exist. Do not pad: fewer well-supported findings beat many weak ones.

Severity:
- high: likely broken functionality, a security concern, or a serious repository inconsistency.
- medium: an important maintainability or developer-experience problem.
- low: an improvement or opportunity.
Start from each signal's severity_hint and change it only if the evidence clearly justifies it. A missing LICENSE is always low. Missing tests and a sparse README are low or medium unless the evidence shows clear context (for example test configuration with no tests, or a large project).

Confidence (0 to 1) states how strongly the evidence supports the finding. Never output 1.0. Do not give every finding the same value. Choose the value the evidence supports:
- basis "direct", exact mismatch or exact line cited: 0.90 to 0.98.
- absence of a file or feature (certain that it is missing, but its importance is a judgement): 0.80 to 0.92.
- basis "heuristic", or an inference beyond the literal facts: 0.55 to 0.80.

Other rules:
- Never invent files, commands, scripts, dependencies, line numbers or evidence. If the evidence is insufficient, make no claim.
- category must be one of: {", ".join(CATEGORIES)}.
- evidence: short strings copied or closely quoted from the signal facts or checks. Every finding needs at least one.
- At most 8 findings, sorted high, then medium, then low. If there are no meaningful issues, return an empty findings array.
- Security: say a "possible secret" was detected. Never claim it is a valid credential. Use masked values exactly as given.
- suggested_patch: a unified diff (---, +++ and correct @@ line counts) ONLY when the evidence contains the exact existing lines needed (for example a README line's text with its line number, or files.gitignore_lines). A new file may use "--- /dev/null". Otherwise null.
- Respond with JSON only, no markdown fences, in exactly this shape:
{{"summary": "short overall assessment", "findings": [{{"severity": "high", "category": "documentation", "title": "...", "explanation": "...", "evidence": ["..."], "recommendation": "...", "confidence": 0.93, "suggested_patch": null}}]}}"""

# Names our own checks look for; evidence may legitimately say they are missing.
ALWAYS_OK = "package.json readme.md .gitignore .env .env.example license"
FILE_TOKEN = re.compile(r"(?<![\w@.:/-])((?:\.{0,2}/)?(?:[\w@.-]+/)*[\w@-][\w@.-]*\.(?:js|jsx|ts|tsx|mjs|cjs|py|json|ya?ml|sh|toml|txt|html|css|md|lock)|\.(?:env|gitignore|npmrc|nvmrc)[\w.-]*)", re.I)
FRAMEWORK_NAMES = {"next.js", "node.js", "vue.js", "nuxt.js", "express.js", "react.js", "nest.js"}


def build_evidence(repository, checks):
    """Compact, bounded evidence. Only this goes to the model, never raw repository files."""
    checks = json.loads(json.dumps(checks))  # copy
    signals = checks.pop("signals", [])
    pkg = checks.get("package", {})
    pkg["scripts"] = {k: str(v)[:200] for k, v in list(pkg.get("scripts", {}).items())[:30]}
    return {"signals": signals, "repository": repository, "checks": checks}


def _post(body, key):
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _error_message(e):
    try:
        return json.loads(e.read().decode("utf-8"))["error"]["message"]
    except Exception:
        return ""


def _call(body, key, retried=False):
    try:
        return _post(body, key)
    except urllib.error.HTTPError as e:
        detail = _error_message(e)
        low = detail.lower()
        if e.code == 429:
            raise GemmaError("Gemma is rate-limited right now (Gemma has low free-tier limits). Wait a minute and try again.")
        if e.code in (401, 403) or (e.code == 400 and "api key" in low):
            raise GemmaError("The Gemini API rejected GEMMA_API_KEY. Check that the key is correct and enabled.")
        if e.code == 404:
            raise GemmaError(f"Model '{MODEL}' was not found by the Gemini API.")
        if e.code == 400 and not retried and any(w in low for w in ("json mode", "mime", "system instruction", "developer instruction")):
            # Fallback: put the instructions in the user message and drop JSON mode.
            sys_text = body["systemInstruction"]["parts"][0]["text"]
            user_text = body["contents"][0]["parts"][0]["text"]
            plain = {"contents": [{"role": "user", "parts": [{"text": sys_text + "\n\n" + user_text}]}],
                     "generationConfig": {k: v for k, v in body["generationConfig"].items() if k != "responseMimeType"}}
            return _call(plain, key, retried=True)
        if e.code >= 500 and not retried:
            time.sleep(2)
            return _call(body, key, retried=True)
        print(f"[gemma] HTTP {e.code}: {detail[:300]}")
        raise GemmaError("The Gemini API returned an error. Please try again.")
    except (socket.timeout, TimeoutError):
        raise GemmaError("Gemma took too long to respond. Please try again.")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise GemmaError("Gemma took too long to respond. Please try again.")
        raise GemmaError("Could not reach the Gemini API. Check your internet connection.")


def _response_text(data):
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise GemmaError(f"Gemma returned no answer{f' (blocked: {reason})' if reason else ''}.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))  # skip reasoning parts
    if not text.strip():
        raise GemmaError("Gemma returned an empty answer.")
    return text


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:]
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1]):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise GemmaError("Gemma returned malformed JSON. Please try again.")


def _ungrounded(text, corpus):
    """True if the text cites a file name that appears nowhere in the evidence we sent."""
    for tok in FILE_TOKEN.findall(text):
        t = tok.lower()
        t = t[2:] if t.startswith("./") else t
        if t not in FRAMEWORK_NAMES and t not in corpus:
            return True
    return False


def validate(result, corpus=None):
    """Check the schema, drop unusable or ungrounded findings, calibrate confidence, sort and cap at 8."""
    if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
        raise GemmaError("Gemma returned JSON in an unexpected format. Please try again.")
    findings = []
    for f in result["findings"]:
        if not isinstance(f, dict):
            continue
        sev, cat = str(f.get("severity", "")).lower(), str(f.get("category", "")).lower()
        evidence = [str(e).strip() for e in f.get("evidence", []) if str(e).strip()] if isinstance(f.get("evidence"), list) else []
        if sev not in SEVERITIES or cat not in CATEGORIES or not evidence:
            continue  # evidence-first: no evidence, no finding
        if not all(isinstance(f.get(k), str) and f[k].strip() for k in ("title", "explanation", "recommendation")):
            continue
        try:
            conf = float(f.get("confidence", 0.8))
        except (TypeError, ValueError):
            conf = 0.8
        conf = conf / 100 if 1 < conf <= 100 else conf
        if corpus is not None:  # grounding guard: drop evidence that cites files we never sent
            grounded = [e for e in evidence if not _ungrounded(e, corpus)]
            if not grounded:
                print(f"[gemma] dropped ungrounded finding: {f['title'][:60]}")
                continue
            if len(grounded) < len(evidence):
                conf = min(conf, 0.8)
            evidence = grounded
        conf = min(conf, 0.98 if len(evidence) >= 2 else 0.95)  # never 1.0; a single piece of evidence caps lower
        patch = f.get("suggested_patch")
        patch = patch if isinstance(patch, str) and patch.strip() and patch.strip().lower() != "null" else None
        findings.append({"severity": sev, "category": cat, "title": f["title"].strip(), "explanation": f["explanation"].strip(),
                         "evidence": evidence, "recommendation": f["recommendation"].strip(),
                         "confidence": round(max(0.05, conf), 2), "suggested_patch": patch})
    findings.sort(key=lambda x: (SEVERITIES.index(x["severity"]), -x["confidence"]))
    summary = result.get("summary") if isinstance(result.get("summary"), str) else ""
    return {"summary": summary.strip() or "Analysis complete.", "findings": findings[:8]}


def analyze_with_gemma(repository, checks):
    key = os.environ.get("GEMMA_API_KEY", "").strip()
    if not key:
        raise GemmaError("GEMMA_API_KEY is not set. Copy .env.example to .env, add your key, and restart the server.")
    evidence = json.dumps(build_evidence(repository, checks), separators=(",", ":"))
    corpus = (evidence + " " + ALWAYS_OK).lower()
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": "EVIDENCE:\n" + evidence}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096, "responseMimeType": "application/json"},
    }
    data = _call(body, key)
    return validate(_extract_json(_response_text(data)), corpus)

def chat_with_gemma(question, analysis, history):
    _load_dotenv()
    key = os.environ.get("GEMMA_API_KEY")

    if not key:
        raise GemmaError("GEMMA_API_KEY is not configured.")

    evidence = build_evidence(
        analysis.get("repository", {}),
        analysis.get("checks", {})
    )

    findings = analysis.get("ai_analysis", {}).get("findings", [])
    summary = analysis.get("ai_analysis", {}).get("summary", "")

    prompt = f"""
You are RepoDoctor, an AI engineer helping a developer understand and fix
a GitHub repository.

You are NOT a generic chatbot.

You have been given the results of RepoDoctor's deterministic inspection
and its existing AI findings. Use ONLY the supplied information when making
claims about the repository.

IMPORTANT RULES:
- Never invent files, code, dependencies, errors, or behavior.
- If the supplied evidence does not answer the question, say that clearly.
- Do not claim that you executed the repository.
- Do not claim that you tested a fix.
- Give practical developer-friendly explanations.
- When suggesting a fix, explain exactly what the developer should change.
- If a patch would be useful, provide a small concrete patch.
- Do not expose secrets or credential values.
- Keep answers concise but useful.

REPOSITORY EVIDENCE:
{evidence}

EXISTING REPO DOCTOR FINDINGS:
{findings}

REPO DOCTOR SUMMARY:
{summary}

CONVERSATION:
{history}

USER QUESTION:
{question}
"""

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1200
        }
    }

    try:
        data = _call(body, key)
        answer = _response_text(data).strip()

        if not answer:
            raise GemmaError("Gemma returned an empty response.")

        return answer

    except GemmaError:
        raise
    except Exception as e:
        raise GemmaError(f"Chat request failed: {e}")
