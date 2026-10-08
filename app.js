const $ = (id) => document.getElementById(id);

let currentPatch = "";
let currentAnalysis = null;
let chatHistory = [];

async function getAnalysis(url) {
  let res;
  try {
    res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_url: url }),
    });
  } catch {
    throw new Error("Could not reach the RepoDoctor server. Is server.py running?");
  }
  const data = await res.json().catch(() => null);
  if (!data || !data.success) throw new Error((data && data.error) || "Analysis failed. Please try again.");
  return data;
}

function validUrl(url) {
  return /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/.test(url.trim());
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function show(id) {
  ["landing", "loading", "results"].forEach((s) => ($(s).hidden = s !== id));
}

async function analyze() {
  const url = $("repo-url").value;
  $("error").hidden = true;
  if (!validUrl(url)) {
    $("error").textContent = "That doesn't look like a GitHub repository URL. Use the form https://github.com/owner/repo.";
    $("error").hidden = false;
    return;
  }
  show("loading");
  try {
    render(await getAnalysis(url));
    show("results");
  } catch (e) {
    show("landing");
    $("error").textContent = e.message;
    $("error").hidden = false;
  }
}

function render(data) {
  const { repository: r, ai_analysis: ai } = data;
  currentAnalysis = data;
chatHistory = [];
  const f = ai.findings;
  
  $("repo-name").textContent = r.name;
  $("repo-meta").textContent = `${r.language} · ${r.file_count} files · ${r.important_files.join(", ")}`;
  const high = f.filter((x) => x.severity === "high").length;
  $("snapshot").innerHTML =
    `<div class="stat high"><b>${high}</b><span>High</span></div>` +
    `<div class="stat warn"><b>${f.length - high}</b><span>Warnings</span></div>` +
    `<div class="stat ok"><b>${data.checks.healthy_count}</b><span>Healthy checks</span></div>`;
  $("summary").textContent = ai.summary;
  $("summary").className = data.ai_error ? "summary error" : "summary";
  $("raw-evidence").textContent = JSON.stringify(data.checks, null, 2);

  $("findings").innerHTML = !f.length ? '<p class="summary">No findings reported.</p>' : f.map((x, i) => `
    <article class="card ${x.severity}">
      <div class="card-top">
        <span class="badge">${x.severity} severity</span><span>${x.category.replace("_", " ")}</span>
        <span class="conf">confidence ${Math.round(x.confidence * 100)}%</span>
      </div>
      <h4>${esc(x.title)}</h4>
      <p>${esc(x.explanation)}</p>
      <ul class="evidence" id="ev-${i}">${x.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
      <p class="rec">${esc(x.recommendation)}</p>
      <div class="actions">
        ${x.suggested_patch ? `<button class="small" data-fix="${i}">Generate fix</button>` : ""}
      </div>
    </article>`).join("");

  document.querySelectorAll("[data-fix]").forEach((b) =>
    b.addEventListener("click", () => openFix(f[b.dataset.fix])));
}

function openFix(finding) {
  currentPatch = finding.suggested_patch;
  $("modal-explain").textContent = finding.recommendation;
  $("modal-diff").innerHTML = currentPatch.split("\n").filter((l, i, a) => l || i < a.length - 1).map((l) => {
    const cls = l.startsWith("@@") ? "hunk" : l.startsWith("+") && !l.startsWith("+++") ? "add"
      : l.startsWith("-") && !l.startsWith("---") ? "del" : "";
    return `<div class="${cls}">${esc(l) || " "}</div>`;
  }).join("");
  $("copy-btn").textContent = "Copy patch";
  $("modal").hidden = false;
}

$("analyze-btn").addEventListener("click", analyze);
$("repo-url").addEventListener("keydown", (e) => { if (e.key === "Enter") analyze(); });
$("reset-btn").addEventListener("click", () => show("landing"));
$("modal-close").addEventListener("click", () => ($("modal").hidden = true));
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) $("modal").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("modal").hidden = true; });
$("copy-btn").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(currentPatch); $("copy-btn").textContent = "Copied"; }
  catch { $("copy-btn").textContent = "Copy failed. Select the text manually."; }
});
async function askRepoDoctor(question) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      question,
      analysis: currentAnalysis,
      history: chatHistory,
    }),
  });

  const data = await res.json().catch(() => null);

  if (!res.ok || !data || !data.success) {
    throw new Error(
      (data && data.error) || "RepoDoctor could not answer right now."
    );
  }

  return data.answer;
}

function addChatMessage(role, message) {
  const container = $("chat-messages");

  const div = document.createElement("div");
  div.className = `chat-message ${role}`;

  const label = role === "user" ? "You" : "RepoDoctor";

  div.innerHTML = `
    <div class="chat-label">${label}</div>
    <p>${esc(message).replace(/\n/g, "<br>")}</p>
  `;

  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

async function sendChatMessage() {
  const input = $("chat-input");
  const question = input.value.trim();

  if (!question || !currentAnalysis) return;

  $("chat-error").hidden = true;
  input.value = "";

  addChatMessage("user", question);

  chatHistory.push({
    role: "user",
    content: question,
  });

  const loading = document.createElement("div");
  loading.className = "chat-message loading";
  loading.id = "chat-loading";
  loading.innerHTML = `
    <div class="chat-label">RepoDoctor</div>
    <p>Thinking…</p>
  `;

  $("chat-messages").appendChild(loading);
  $("chat-messages").scrollTop = $("chat-messages").scrollHeight;

  $("chat-send").disabled = true;

  try {
    const answer = await askRepoDoctor(question);

    $("chat-loading")?.remove();

    addChatMessage("assistant", answer);

    chatHistory.push({
      role: "assistant",
      content: answer,
    });
  } catch (e) {
    $("chat-loading")?.remove();

    $("chat-error").textContent = e.message;
    $("chat-error").hidden = false;
  } finally {
    $("chat-send").disabled = false;
    input.focus();
  }
}

$("chat-send").addEventListener("click", sendChatMessage);

$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});
