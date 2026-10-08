"""Deterministic repository analysis. Reads files only; never executes anything from the repo."""
import json
import os
import re

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "vendor", "target", ".idea", ".tox"}
SOURCE_EXT = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
              ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
              ".php": "PHP", ".c": "C", ".cpp": "C++", ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".woff", ".woff2",
              ".ttf", ".eot", ".mp3", ".mp4", ".mov", ".exe", ".dll", ".so", ".jar", ".pyc", ".class", ".bin"}
SKIP_SCAN = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock"}
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs"}
TEST_FILE = re.compile(r"^(test_.*\.py|.*_test\.(py|go)|.*\.(test|spec)\.[cm]?[jt]sx?)$", re.I)
TEST_CONFIGS = {"jest.config.js", "jest.config.ts", "jest.config.cjs", "jest.config.mjs", "vitest.config.ts",
                "vitest.config.js", "pytest.ini", "tox.ini", "conftest.py", "karma.conf.js"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
MAX_FILES, MAX_SCAN, MAX_READ = 5000, 1500, 500_000

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("API key (sk- style)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
]
QUOTED_ASSIGN = re.compile(r"""(?i)\b[\w-]*(?:api_?key|secret|token|passw(?:or)?d)[\w-]*["']?\s*[:=]\s*["']([^"'\s]{8,})["']""")
ENV_ASSIGN = re.compile(r"""(?i)^\s*(?:export\s+)?[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*\s*=\s*['"]?([^'"\s#]{8,})""")
PLACEHOLDER_WORDS = ("your", "example", "xxx", "changeme", "placeholder", "<", ">", "${", "{{", "process.env",
                     "os.environ", "dummy", "test", "sample", "...")
ENV_USE = [re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)"), re.compile(r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]"),
           re.compile(r"os\.environ(?:\.get)?[\[(]\s*['\"]([A-Z][A-Z0-9_]+)['\"]")]
SCRIPT_RX = re.compile(r"\b(npm|yarn|pnpm)\s+(run\s+)?([\w:.-]+)")
NON_SCRIPTS = {"install", "i", "ci", "add", "remove", "init", "upgrade", "dlx", "create", "global", "config", "link",
               "publish", "audit", "info", "why", "up", "exec", "cache", "version", "workspace", "workspaces", "is", "and", "or"}
TODO_RX = re.compile(r"\b(TODO|FIXME|HACK)\b")


def mask(v):
    return v[:3] + "*" * max(len(v) - 7, 4) + v[-4:] if len(v) >= 12 else "*" * len(v)


def is_placeholder(v):
    low = v.lower()
    return any(w in low for w in PLACEHOLDER_WORDS) or len(set(low)) <= 2


def is_env_file(name):
    n = name.lower()
    return n == ".env" or (n.startswith(".env.") and not n.endswith(("example", "sample", "template", "dist")))


def read_text(path, limit=MAX_READ):
    try:
        with open(path, "rb") as f:
            raw = f.read(limit)
    except OSError:
        return None
    if b"\0" in raw[:1024]:
        return None
    return raw.decode("utf-8", errors="replace")


def readme_commands(text, scripts, package_exists, has_server_js):
    mentioned, missing, in_code = [], [], False
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if not (in_code or "`" in line):
            continue
        for m in SCRIPT_RX.finditer(line):
            mgr, run, name = m.group(1), m.group(2), m.group(3)
            if name.startswith("-"):
                continue
            if mgr == "npm" and not run and name not in ("start", "test"):
                continue
            if mgr != "npm" and not run and name in NON_SCRIPTS:
                continue
            cmd = m.group(0).strip()
            mentioned.append({"line": n, "command": cmd, "text": line.strip()[:200]})
            if name in scripts or (name == "start" and has_server_js):
                continue
            reason = f'package.json has no "{name}" script' if package_exists else "package.json not found"
            missing.append({"line": n, "command": cmd, "text": line.strip()[:200], "reason": reason,
                            "available_scripts": sorted(scripts)})
    return mentioned, missing


OUTPUT_DIRS = ("dist/", "build/", "out/", ".next/", "node_modules/", "coverage/", "venv/", ".venv/", "target/", "__pycache__/")
FILE_REF = re.compile(r"(?<![\w@.:/-])((?:\.{1,2}/)?(?:[\w@.-]+/)*[\w@-][\w@.-]*\.(?:js|jsx|ts|tsx|mjs|cjs|py|json|ya?ml|sh|toml|txt|html|css|md))(?![\w/])")
ENV_REF = re.compile(r"(?<![\w/])(\.env\.(?:example|sample|template))\b")
MGR_RX = re.compile(r"\b(npm|yarn|pnpm|bun)\s+(?:run\s+|install|i\b|add|ci\b|start|test|dev|build)")
LOCK_MGR = {"package-lock.json": "npm", "yarn.lock": "yarn", "pnpm-lock.yaml": "pnpm", "bun.lockb": "bun", "bun.lock": "bun"}
CONFIG_FAMILIES = (("Next.js", ("next.config.",)), ("Vite", ("vite.config.",)), ("webpack", ("webpack.config.",)),
                   ("Tailwind", ("tailwind.config.",)), ("PostCSS", ("postcss.config.", ".postcssrc")),
                   ("Jest", ("jest.config.",)), ("Babel", ("babel.config.", ".babelrc")),
                   ("ESLint", (".eslintrc", "eslint.config.")), ("Prettier", (".prettierrc", "prettier.config.")))
CREDENTIAL_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL")


def ref_exists(token, all_paths, basenames, strict=False):
    """True if a referenced file exists (or can't be judged). Bare file names match anywhere in the tree unless strict."""
    t = token[2:] if token.startswith("./") else token
    if t.startswith("../") or t.startswith(OUTPUT_DIRS):
        return True
    return t in all_paths or (not strict and "/" not in t and t in basenames)


def script_file_refs(scripts, all_paths, basenames):
    out, seen = [], set()
    for name, cmd in scripts.items():
        for tok in FILE_REF.findall(str(cmd)):
            if (name, tok) not in seen and not ref_exists(tok, all_paths, basenames):
                seen.add((name, tok))
                out.append({"script": name, "command": str(cmd)[:200], "missing_file": tok})
    return out[:10]


def entry_refs(pkg, all_paths):
    paths = []
    if isinstance(pkg.get("main"), str):
        paths.append(("main", pkg["main"]))
    b = pkg.get("bin")
    for v in ([b] if isinstance(b, str) else list(b.values()) if isinstance(b, dict) else []):
        if isinstance(v, str):
            paths.append(("bin", v))
    return [{"field": f, "path": p} for f, p in paths
            if "." in os.path.basename(p) and not ref_exists(p, all_paths, set(), strict=True)][:5]


def code_lines(text):
    """Yield (line_number, line) for README lines that are in a code block or contain inline code."""
    in_code = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
        elif in_code or "`" in line:
            yield n, line


def readme_file_refs(text, all_paths, basenames):
    out, seen = [], set()
    for n, line in code_lines(text):
        for tok in FILE_REF.findall(line) + ENV_REF.findall(line):
            if tok not in seen and not ref_exists(tok, all_paths, basenames):
                seen.add(tok)
                out.append({"line": n, "path": tok, "text": line.strip()[:200]})
    return out


def readme_managers(text):
    return sorted({m.group(1) for _, line in code_lines(text) for m in MGR_RX.finditer(line)})


def build_signals(repository, checks):
    """Ranked candidate issues computed in Python. Gemma explains and prioritizes them; it does not discover them."""
    f, pkg, rd, t = checks["files"], checks["package"], checks["readme"], checks["tests"]
    st, env, pm = checks["structure"], checks["environment"], checks["package_manager"]
    readme = f["readme"] or "README"
    signals = []

    def add(id_, kind, basis, severity, summary, facts):
        if facts:
            signals.append({"id": id_, "kind": kind, "basis": basis, "severity_hint": severity, "summary": summary, "facts": facts})

    secrets = checks["security"]["possible_secrets"]
    add("possible_secrets", "risk", "direct" if any(x["type"] != "secret-like assignment" for x in secrets) else "heuristic",
        "high" if any(x["type"] != "secret-like assignment" for x in secrets) else "medium",
        "Values that look like credentials were found (masked).",
        [f'{x["file"]}:{x["line"]} {x["type"]} (masked: {x["masked"]})' for x in secrets[:8]])
    secret_files = {x["file"] for x in secrets}
    add("env_file_committed", "risk", "direct", "high" if secret_files & set(f["env_files"]) else "medium",
        "An environment file is tracked in the repository.",
        [f"{p}: tracked in the repository" for p in f["env_files"][:5]]
        + ([f".gitignore mentions .env: {'yes' if f['gitignore_mentions_env'] else 'no'}"] if f["env_files"] else []))
    add("readme_command_mismatch", "inconsistency", "direct", "high",
        "The README documents commands that package.json does not define.",
        [x for m in rd["commands_missing_from_scripts"][:5]
         for x in (f'{readme} line {m["line"]}: {m["text"]}',
                   f'{m["reason"]}; available scripts: {", ".join(m["available_scripts"]) or "none"}')])
    add("script_references_missing_file", "inconsistency", "direct", "high",
        "A package.json script points at a file that is not in the repository.",
        [f'package.json scripts.{m["script"]}: "{m["command"]}" references {m["missing_file"]}, which is not in the repository'
         for m in pkg["broken_script_refs"][:5]])
    add("package_entry_missing", "inconsistency", "direct", "medium",
        "package.json main/bin points at a file that is not in the repository.",
        [f'package.json {m["field"]}: {m["path"]} not found in the repository' for m in pkg["missing_entry_files"]])
    add("manifest_missing", "inconsistency", "direct", "high", "Source files exist without a dependency manifest.",
        st["manifest_missing"])
    add("readme_references_missing_file", "inconsistency", "heuristic", "medium",
        "The README mentions files that were not found in the repository (heuristic match on code snippets).",
        [x for m in rd["missing_file_refs"][:5] for x in (f'{readme} line {m["line"]}: {m["text"]}', f'{m["path"]} not found in the repository')])
    add("package_manager_mismatch", "inconsistency", "direct", "medium",
        "The package manager implied by the README, packageManager field and lockfiles disagree.",
        pm["mismatches"] + ([f'Lockfiles found: {", ".join(pm["lockfiles"])}'] if pm["mismatches"] else []))
    add("conflicting_config", "inconsistency", "direct", "medium", "Multiple configuration files for the same tool.",
        st["duplicate_config"])
    add("test_config_without_tests", "inconsistency", "direct", "medium", "Test tooling is configured but no tests were detected.",
        [f"{c}: test configuration present" for c in t["configs"][:3]] + ["No test files detected"] if t["config_without_tests"] else [])
    add("empty_test_directory", "inconsistency", "direct", "medium", "A test directory exists but contains no files.",
        [f"{d}/: test directory contains no files" for d in t["empty_test_dirs"][:3]])
    undoc = env["not_documented"]
    add("env_vars_undocumented", "inconsistency", "heuristic", "medium",
        "Environment variables are read in code but not documented in the README or an .env.example.",
        [f'{v} read at {env["locations"].get(v, "unknown location")}' for v in undoc[:6]]
        + ["Not mentioned in the README or .env.example"]
        + (["Credential-like names: " + ", ".join(env["credential_like"][:5])] if env["credential_like"] else [])
        + ([] if env["env_example_present"] else [".env.example: not found"]) if undoc else [])
    add("oversized_files", "quality", "direct", "low", "Very large files or source files.",
        [f'{x["path"]}: {x["size_mb"]} MB' for x in st["large_files"][:3]]
        + [f'{x["path"]}: {x["lines"]} lines' for x in st["long_source_files"][:3]])
    add("many_todos", "quality", "direct", "low", "Many TODO/FIXME markers.",
        [f'{st["todo_count"]} TODO/FIXME/HACK markers'] + [f'{x["path"]}: {x["count"]}' for x in st["todo_top_files"][:3]]
        if st["todo_count"] >= 10 else [])
    src_files = sum(l["files"] for l in repository["languages"])
    no_tests = not t["detected"] and not t["config_without_tests"] and not t["empty_test_dirs"]
    add("no_tests", "absence", "direct", "medium" if src_files >= 100 else "low", "No automated tests were detected.",
        ["No test files detected"] + ([] if t["test_dirs"] else ["No test directory found"])
        + (['package.json scripts.test is the default npm placeholder'] if rd["placeholder_test_script"] else [])
        + [f'Project size: {repository["file_count"]} files, {pkg["dependency_count"]} dependencies'] if no_tests else [])
    add("no_license", "absence", "direct", "low", "No license file.", [] if f["license"] else ["LICENSE: not found"])
    thin = (not f["readme"]) or rd["chars"] < 400 or not rd["has_setup_section"]
    add("readme_thin", "absence", "direct", "medium" if not f["readme"] else "low", "README is missing or sparse.",
        (["README: not found"] if not f["readme"] else
         [f'{readme}: {rd["chars"]} characters'] + ([] if rd["has_setup_section"] else ["No install/usage/setup heading found"])) if thin else [])
    add("no_gitignore", "absence", "direct", "low", "No .gitignore.", [] if f["gitignore"] else [".gitignore: not found"])
    return signals


def analyze(path, repo_name):
    root_files = {f.lower(): f for f in os.listdir(path)}
    files, test_dirs, test_files, test_configs, env_files = [], [], [], [], []
    truncated = False

    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_root = os.path.relpath(root, path)
        rel_root = "" if rel_root == "." else rel_root
        if rel_root and os.path.basename(root).lower() in TEST_DIRS:
            test_dirs.append(rel_root)
        in_test_dir = any(rel_root == d or rel_root.startswith(d + os.sep) for d in test_dirs)
        for name in names:
            rel = os.path.join(rel_root, name).replace(os.sep, "/")
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            files.append((rel, size))
            ext = os.path.splitext(name)[1].lower()
            if TEST_FILE.match(name) or (in_test_dir and ext in SOURCE_EXT):
                test_files.append(rel)
            if name.lower() in TEST_CONFIGS:
                test_configs.append(rel)
            if is_env_file(name):
                env_files.append(rel)
        if len(files) >= MAX_FILES:
            truncated = True
            break

    # languages / project type
    counts = {}
    for rel, _ in files:
        lang = SOURCE_EXT.get(os.path.splitext(rel)[1].lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    languages = [{"name": k, "files": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    has_pkg = "package.json" in root_files
    has_py_manifest = any(f in root_files for f in ("requirements.txt", "pyproject.toml", "setup.py"))
    project_type = "Node.js" if has_pkg else "Python" if has_py_manifest else (languages[0]["name"] if languages else "other")

    # package.json
    pkg = {}
    package = {"exists": has_pkg, "scripts": {}, "dependency_count": 0}
    if has_pkg:
        try:
            pkg = json.loads(read_text(os.path.join(path, root_files["package.json"])) or "{}")
            package["scripts"] = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}
            package["dependency_count"] = len(pkg.get("dependencies", {})) + len(pkg.get("devDependencies", {}))
        except (ValueError, AttributeError):
            package["parse_error"] = True
    pkg = pkg if isinstance(pkg, dict) else {}
    all_paths = {rel for rel, _ in files}
    basenames = {os.path.basename(p) for p in all_paths}
    package["broken_script_refs"] = script_file_refs(package["scripts"], all_paths, basenames)
    package["missing_entry_files"] = entry_refs(pkg, all_paths)
    placeholder_test = "no test specified" in str(package["scripts"].get("test", "")).lower()

    # README
    readme_name = next((orig for low, orig in root_files.items() if low.startswith("readme")), None)
    readme_text = (read_text(os.path.join(path, readme_name)) or "") if readme_name else ""
    has_setup = bool(re.search(r"^#+\s*.*(install|setup|getting started|usage|quick ?start|running)", readme_text, re.I | re.M))
    mentioned, missing_cmds = readme_commands(readme_text, package["scripts"], has_pkg, "server.js" in root_files)
    readme_refs = readme_file_refs(readme_text, all_paths, basenames)
    readme_mgrs = readme_managers(readme_text)

    # single pass over text files: secrets, TODOs, env vars, long files
    secrets, todo_by_file, env_used, long_files = [], {}, set(), []
    scanned = 0
    env_where = {}
    for rel, size in files:
        name = os.path.basename(rel)
        ext = os.path.splitext(name)[1].lower()
        if size > MAX_READ or ext in BINARY_EXT or name.lower() in SKIP_SCAN or name.endswith(".min.js") or scanned >= MAX_SCAN:
            continue
        text = read_text(os.path.join(path, rel))
        if text is None:
            continue
        scanned += 1
        lines = text.splitlines()
        if ext in SOURCE_EXT and len(lines) > 1500:
            long_files.append({"path": rel, "lines": len(lines)})
        env_file = is_env_file(name)
        for n, line in enumerate(lines, 1):
            if len(line) > 1000:
                continue
            if TODO_RX.search(line):
                todo_by_file[rel] = todo_by_file.get(rel, 0) + 1
            for rx in ENV_USE:
                for v in rx.findall(line):
                    env_used.add(v)
                    env_where.setdefault(v, f"{rel}:{n}")
            if len(secrets) >= 20:
                continue
            hit = None
            for label, rx in SECRET_PATTERNS:
                m = rx.search(line)
                if m:
                    hit = (label, "[private key header]" if label == "private key" else mask(m.group(0)))
                    break
            if not hit:
                m = (ENV_ASSIGN if env_file else QUOTED_ASSIGN).search(line)
                if m and not is_placeholder(m.group(1)):
                    hit = ("secret-like assignment", mask(m.group(1)))
            if hit:
                secrets.append({"file": rel, "line": n, "type": hit[0], "masked": hit[1]})

    env_example = next((f for low, f in root_files.items() if low in (".env.example", ".env.sample", ".env.template")), None)
    doc_text = readme_text + (read_text(os.path.join(path, env_example)) or "" if env_example else "")
    undocumented = sorted(v for v in env_used if v not in doc_text)[:20]

    large_files = [{"path": r, "size_mb": round(s / 1_000_000, 1)} for r, s in sorted(files, key=lambda x: -x[1]) if s > 1_000_000][:5]
    gitignore_text = read_text(os.path.join(path, root_files[".gitignore"])) if ".gitignore" in root_files else ""
    present = lambda *names: next((root_files[n] for n in names if n in root_files), None)
    license_name = next((orig for low, orig in root_files.items() if low.startswith(("license", "copying"))), None)

    manifest_missing = []
    if counts.get("Python") and not has_py_manifest:
        manifest_missing.append("Python files found but no requirements.txt, pyproject.toml or setup.py")
    if (counts.get("JavaScript") or counts.get("TypeScript")) and not has_pkg:
        manifest_missing.append("JavaScript/TypeScript files found but no package.json")
    lock_found = sorted(f for f in root_files.values() if f in LOCKFILES)
    duplicate_config = []
    if len(lock_found) > 1:
        duplicate_config.append("Multiple lockfiles: " + ", ".join(lock_found))
    for label, prefixes in CONFIG_FAMILIES:
        found = [f for f in root_files.values() if f.startswith(prefixes)]
        if len(found) > 1:
            duplicate_config.append(f"Multiple {label} configs: " + ", ".join(sorted(found)))

    lock_mgrs = sorted({LOCK_MGR[f] for f in root_files.values() if f in LOCK_MGR})
    pm_field = str(pkg.get("packageManager", "")).split("@")[0]
    pm_mismatches = []
    if has_pkg and lock_mgrs:
        if pm_field and pm_field not in lock_mgrs:
            pm_mismatches.append(f'package.json packageManager is "{pm_field}" but the lockfile indicates {", ".join(lock_mgrs)}')
        extra = sorted(set(readme_mgrs) - set(lock_mgrs))
        if extra:
            pm_mismatches.append(f'{readme_name} uses {", ".join(extra)} commands but the lockfile indicates {", ".join(lock_mgrs)}')
    empty_test_dirs = [d for d in test_dirs if not any(rel.startswith(d + "/") for rel, _ in files)]
    detected = bool(test_files)
    checklist = {
        "readme_present": bool(readme_name),
        "readme_has_setup_info": bool(readme_name) and has_setup,
        "license_present": bool(license_name),
        "gitignore_present": ".gitignore" in root_files,
        "no_env_file_committed": not env_files,
        "dependency_manifest_present": not manifest_missing,
        "tests_present": detected,
        "no_possible_secrets": not secrets,
        "no_oversized_files": not large_files and not long_files,
        "readme_commands_match_scripts": not missing_cmds and not placeholder_test,
        "no_broken_file_references": not (package["broken_script_refs"] or package["missing_entry_files"] or readme_refs),
        "package_manager_consistent": not pm_mismatches,
        "no_conflicting_configs": not duplicate_config,
    }
    top_todos = sorted(todo_by_file.items(), key=lambda kv: -kv[1])[:5]

    important = [f for f in (readme_name, license_name) if f] + [
        root_files[f] for f in ("package.json", "requirements.txt", "pyproject.toml", "setup.py", ".gitignore", ".env")
        if f in root_files]
    repository = {
        "name": repo_name, "language": languages[0]["name"] if languages else project_type,
        "project_type": project_type, "languages": languages[:5], "file_count": len(files),
        "truncated": truncated, "important_files": important,
        "top_level_entries": sorted(e for e in os.listdir(path) if e != ".git")[:40],
    }
    checks = {
        "files": {"readme": readme_name, "license": license_name, "gitignore": present(".gitignore"),
                  "env_files": env_files, "env_example": env_example,
                  "gitignore_mentions_env": ".env" in (gitignore_text or ""),
                  "gitignore_lines": (gitignore_text or "").splitlines()[:60],
                  "gitignore_line_count": len((gitignore_text or "").splitlines())},
        "package": package,
        "readme": {"chars": len(readme_text), "has_setup_section": has_setup, "commands": mentioned[:20],
                   "commands_missing_from_scripts": missing_cmds[:10], "placeholder_test_script": placeholder_test,
                   "missing_file_refs": readme_refs[:10]},
        "tests": {"detected": detected, "paths": test_files[:10], "test_dirs": test_dirs[:10],
                  "empty_test_dirs": empty_test_dirs, "configs": test_configs,
                  "config_without_tests": bool(test_configs) and not detected},
        "security": {"possible_secrets": secrets},
        "environment": {"variables_used_in_code": sorted(env_used)[:20], "not_documented": undocumented,
                        "locations": {v: env_where[v] for v in sorted(env_used)[:20]},
                        "credential_like": [v for v in undocumented if CREDENTIAL_NAME.search(v)],
                        "env_example_present": bool(env_example)},
        "structure": {"manifest_missing": manifest_missing, "duplicate_config": duplicate_config,
                      "large_files": large_files, "long_source_files": sorted(long_files, key=lambda x: -x["lines"])[:5],
                      "todo_count": sum(todo_by_file.values()),
                      "todo_top_files": [{"path": p, "count": c} for p, c in top_todos]},
        "package_manager": {"lockfiles": lock_found, "managers_from_lockfiles": lock_mgrs, "package_manager_field": pm_field or None,
                            "readme_managers": readme_mgrs, "mismatches": pm_mismatches},
        "checklist": checklist,
        "healthy_count": sum(checklist.values()),
    }
    checks["signals"] = build_signals(repository, checks)
    return repository, checks
