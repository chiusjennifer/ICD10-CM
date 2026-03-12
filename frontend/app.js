const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const chatLog = document.getElementById("chatLog");
const timeline = document.getElementById("timeline");
const keywordList = document.getElementById("keywordList");
const mcpList = document.getElementById("mcpList");
const finalList = document.getElementById("finalList");
const statusPill = document.querySelector(".status-pill");

const dictForm = document.getElementById("dictForm");
const dictInput = document.getElementById("dictInput");
const dictResults = document.getElementById("dictResults");
const dictDetail = document.getElementById("dictDetail");

const DEFAULT_TIMELINE = ["讀取病歷", "抽取關鍵字", "術語正規化", "MCP 查碼", "主次診斷決策"];

function toChineseSourceLabel(sourceField) {
  const raw = String(sourceField || "").trim();
  if (!raw) return "未標示";

  if (raw.includes("出院診斷")) return "出院診斷";
  if (raw.includes("住院治療經過") || raw.includes("體檢發現")) return "體檢發現/住院治療經過";
  if (raw.includes("病史")) return "病史";
  if (raw.includes("主訴")) return "主訴";
  if (raw.includes("檢驗報告")) return "檢驗報告";

  const normalized = raw.toLowerCase().replace(/[\s_\-./:]+/g, "");
  const rulePairs = [
    ["dischargediagnosis", "出院診斷"],
    ["diagnosis", "出院診斷"],
    ["hospitalcourse", "體檢發現/住院治療經過"],
    ["course", "體檢發現/住院治療經過"],
    ["history", "病史"],
    ["chiefcomplaint", "主訴"],
    ["complaint", "主訴"],
    ["lab", "檢驗報告"],
    ["laboratory", "檢驗報告"],
    ["testreport", "檢驗報告"]
  ];

  for (const [token, label] of rulePairs) {
    if (normalized.includes(token)) return label;
  }
  return raw;
}

function addMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function resetPanels() {
  timeline.innerHTML = "";
  keywordList.innerHTML = "";
  mcpList.innerHTML = "";
  finalList.innerHTML = "";
}

function renderTimeline(steps, doneIndex = -1) {
  timeline.innerHTML = "";
  steps.forEach((step, idx) => {
    const li = document.createElement("li");
    li.textContent = `${idx + 1}. ${step}`;
    if (idx <= doneIndex) li.classList.add("done");
    timeline.appendChild(li);
  });
}

function appendEmpty(list, message) {
  const li = document.createElement("li");
  li.className = "empty";
  li.textContent = message;
  list.appendChild(li);
}

function renderCodingResult(data) {
  const steps = Array.isArray(data.timeline) && data.timeline.length ? data.timeline : DEFAULT_TIMELINE;
  renderTimeline(steps, steps.length - 1);

  const extractedAll = Array.isArray(data.extracted) ? data.extracted : Array.isArray(data.extracted_keywords) ? data.extracted_keywords : [];
  const extracted = extractedAll.filter((k) => String(k?.extractor || "").toLowerCase() === "llm");
  extracted.forEach((k) => {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${k.term || ""}</strong><div class="meta">來源：${toChineseSourceLabel(k.sourceField)}</div>`;
    keywordList.appendChild(li);
  });
  if (!extracted.length) {
    appendEmpty(keywordList, "尚未抽取到 LLM 關鍵字。");
  }

  const mcpMatches = Array.isArray(data.mcpMatches) ? data.mcpMatches : Array.isArray(data.mcp_matches) ? data.mcp_matches : [];
  const mcpByKeyword = new Map();
  mcpMatches.forEach((m) => {
    const key = String(m?.keyword || "").trim().toLowerCase();
    if (key && !mcpByKeyword.has(key)) {
      mcpByKeyword.set(key, m);
    }
  });

  extracted.forEach((k) => {
    const keyword = String(k?.term || "").trim();
    if (!keyword) return;
    const li = document.createElement("li");
    const matched = mcpByKeyword.get(keyword.toLowerCase());
    const candidates = Array.isArray(matched?.candidates) ? matched.candidates : [];
    if (!candidates.length) {
      li.innerHTML = `<strong>${keyword}</strong><div class="meta">查無候選碼</div>`;
    } else {
      const codeText = candidates.map((c) => c.code).filter(Boolean).join("、");
      li.innerHTML = `<strong>${keyword}</strong><div class="meta">候選碼：${codeText || "無"}</div>`;
    }
    mcpList.appendChild(li);
  });
  if (!extracted.length || !mcpList.children.length) {
    appendEmpty(mcpList, "尚未產生 MCP 查碼結果。");
  }

  const finalCodes = Array.isArray(data.finalCodes) ? data.finalCodes : [];
  finalCodes.forEach((f) => {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${f.role || "診斷"} ${f.code || ""}</strong><div class="meta">${f.title || ""}</div>`;
    finalList.appendChild(li);
  });
  if (!finalCodes.length) {
    appendEmpty(finalList, "尚未產生最終診斷碼。");
  }
}

function renderDictDetail(item) {
  dictDetail.innerHTML = `
    <h4>${item.code || ""} ${item.title || ""}</h4>
    <p><strong>章節：</strong>${item.chapter || "ICD-10-CM"}</p>
    <p><strong>備註：</strong>${item.notes || "無"}</p>
  `;
}

function renderDictResults(results) {
  dictResults.innerHTML = "";
  if (!results.length) {
    dictResults.innerHTML = '<li class="empty">查無符合結果</li>';
    dictDetail.innerHTML = '<p class="empty">請輸入代碼或關鍵字開始查詢</p>';
    return;
  }

  results.forEach((item, idx) => {
    const li = document.createElement("li");
    li.className = `dict-item${idx === 0 ? " active" : ""}`;
    li.innerHTML = `<strong>${item.code || ""}</strong><span>${item.title || ""}</span>`;
    li.addEventListener("click", () => {
      document.querySelectorAll(".dict-item").forEach((n) => n.classList.remove("active"));
      li.classList.add("active");
      renderDictDetail(item);
    });
    dictResults.appendChild(li);
  });

  renderDictDetail(results[0]);
}

async function checkApiHealth() {
  try {
    const resp = await fetch("/api/health");
    if (!resp.ok) throw new Error("health check failed");
    statusPill.textContent = "Live API";
  } catch (_err) {
    statusPill.textContent = "API 離線";
  }
}

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const command = chatInput.value.trim();
  if (!command) return;

  addMessage("user", command);
  chatInput.value = "";
  resetPanels();
  renderTimeline(DEFAULT_TIMELINE, 0);

  try {
    const resp = await fetch("/api/code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command })
    });

    const payload = await resp.json();
    if (!resp.ok) {
      addMessage("assistant", `編碼失敗：${payload.error || "未知錯誤"}`);
      return;
    }

    renderCodingResult(payload);
    const replyText = payload.replyText
      ? `病歷號 ${payload.chartNo} 編碼完成：\n${payload.replyText}`
      : `病歷號 ${payload.chartNo} 編碼完成。`;
    addMessage("assistant", replyText);
  } catch (err) {
    addMessage("assistant", `編碼失敗：${err.message}`);
  }
});

dictForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = dictInput.value.trim();
  if (!query) return;

  try {
    const resp = await fetch(`/api/dictionary?q=${encodeURIComponent(query)}`);
    const payload = await resp.json();
    if (!resp.ok) {
      dictResults.innerHTML = '<li class="empty">查詢失敗</li>';
      dictDetail.innerHTML = `<p class="empty">${payload.error || "未知錯誤"}</p>`;
      return;
    }
    renderDictResults(payload.results || []);
  } catch (err) {
    dictResults.innerHTML = '<li class="empty">查詢失敗</li>';
    dictDetail.innerHTML = `<p class="empty">${err.message}</p>`;
  }
});

addMessage("assistant", "請輸入指令，例如：對病歷號 7224088 的病人進行編碼。");
checkApiHealth();
