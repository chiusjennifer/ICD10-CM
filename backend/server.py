import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
SKILL_NORM_FILE = ROOT / "skills" / "icd10-mcp-encoder" / "references" / "normalization.md"
SKILL_NORM_RULES_FILE = ROOT / "skills" / "icd10-mcp-encoder" / "references" / "normalization_rules.json"
MCP_URL = "http://127.0.0.1:8000/mcp"
ENV_FILE = ROOT / ".env"


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ.setdefault(key, value)


load_env_file(ENV_FILE)

LLM_API_URL = os.getenv("ICD10_LLM_API_URL", "https://api.openai.com/v1/chat/completions").strip()
LLM_API_KEY = os.getenv("ICD10_LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("ICD10_LLM_MODEL", "gpt-4o-mini").strip()
LLM_SPELLING_ENABLED = os.getenv("ICD10_LLM_SPELLING_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}

F_DISCHARGE = "\u51fa\u9662\u8a3a\u65b7"
F_COURSE = "\u9ad4\u6aa2\u767c\u73fe/\u4f4f\u9662\u6cbb\u7642\u7d93\u904e"
F_HISTORY = "\u75c5\u53f2"
F_CHIEF = "\u4e3b\u8a34"
F_LAB = "\u6aa2\u9a57\u5831\u544a"
EXTRACT_FIELDS = [F_DISCHARGE, F_HISTORY, F_CHIEF, F_COURSE, F_LAB]
FIELD_PRIORITY = {F_DISCHARGE: 0, F_HISTORY: 1, F_CHIEF: 2, F_COURSE: 3, F_LAB: 4}


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s))).strip(" .;,:-\n\t")


def load_norm_markdown() -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not SKILL_NORM_FILE.exists():
        return mapping
    for raw in SKILL_NORM_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("-") and "->" in line:
            left, right = line.lstrip("-").strip().split("->", 1)
            mapping[left.strip().lower()] = right.strip()
    return mapping


def load_norm_assets() -> tuple[dict[str, str], list[dict[str, Any]], dict[str, str], set[str]]:
    mapping = load_norm_markdown()
    rules: list[dict[str, Any]] = []
    corrections: dict[str, str] = {}
    excluded_terms: set[str] = {"pci", "stent", "angiography"}
    if not SKILL_NORM_RULES_FILE.exists():
        return mapping, rules, corrections, excluded_terms
    try:
        payload = json.loads(SKILL_NORM_RULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return mapping, rules, corrections, excluded_terms
    for left, right in dict(payload.get("spelling_corrections") or {}).items():
        lk = _clean_text(str(left)).lower()
        rv = _clean_text(str(right))
        if lk and rv:
            corrections[lk] = rv
    for rule in list(payload.get("rules") or []):
        if not isinstance(rule, dict):
            continue
        target = _clean_text(str(rule.get("target") or ""))
        if not target:
            continue
        src_raw = rule.get("source")
        if isinstance(src_raw, list):
            source = [_clean_text(str(s)).lower() for s in src_raw if _clean_text(str(s))]
        else:
            source = [_clean_text(str(src_raw)).lower()] if _clean_text(str(src_raw or "")) else []
        if not source:
            continue
        for src in source:
            mapping.setdefault(src, target)
        rules.append(
            {
                "source": source,
                "target": target,
                "category": str(rule.get("category") or ""),
                "require_any": [_clean_text(str(s)).lower() for s in list(rule.get("require_any") or []) if _clean_text(str(s))],
                "deny_any": [_clean_text(str(s)).lower() for s in list(rule.get("deny_any") or []) if _clean_text(str(s))],
                "priority": int(rule.get("priority") or 0),
            }
        )
    excluded_terms.update({str(i).lower() for i in list(payload.get("exclude_terms") or []) if str(i).strip()})
    return mapping, rules, corrections, excluded_terms


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _clean(s: str) -> str:
    return _clean_text(s)


def _negated(s: str) -> bool:
    lower = s.lower()
    if re.search(r"\b(no|denies|without|negative for|rule out|ruled out|not)\b", lower):
        return True
    return any(x in s for x in ["\u5426\u8a8d", "\u7121", "\u6392\u9664", "\u672a\u898b"])


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def parse_stream(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.split("data:", 1)[1].strip())
    raise ValueError("Unable to parse MCP response")


def mcp_initialize() -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "icd10-ui", "version": "1.0"}},
    }
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        _ = resp.read().decode("utf-8", errors="ignore")
        session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("MCP init failed")
    return session_id


def mcp_call(session_id: str, name: str, arguments: dict[str, Any], req_id: int) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Session-Id": session_id})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return parse_stream(resp.read().decode("utf-8", errors="ignore"))


def search_codes(session_id: str, keyword: str, req_id: int) -> list[dict[str, Any]]:
    raw = mcp_call(session_id, "search_medical_codes", {"keyword": keyword, "type": "diagnosis"}, req_id)
    result = raw.get("result", {})
    payload = result.get("structuredContent", {}).get("result", "")
    if result.get("isError") or not payload or payload.startswith("No results"):
        return []
    try:
        diagnoses = json.loads(payload).get("diagnoses", [])
    except json.JSONDecodeError:
        return []
    return [{"code": i.get("code"), "name_en": i.get("name_en"), "name_zh": i.get("name_zh")} for i in diagnoses if i.get("code")]


def nearby_codes(session_id: str, code: str, req_id: int) -> list[dict[str, Any]]:
    raw = mcp_call(session_id, "get_nearby_codes", {"code": code, "type": "diagnosis"}, req_id)
    result = raw.get("result", {})
    payload = result.get("structuredContent", {}).get("result", "")
    if result.get("isError") or not payload or payload.startswith("No nearby"):
        return []
    try:
        rows = json.loads(payload).get("nearby_codes", [])
    except json.JSONDecodeError:
        return []
    return [{"code": i.get("code"), "name_en": i.get("name_en"), "name_zh": i.get("name_zh")} for i in rows if i.get("code")]


def fetch_chart(chart_no: str) -> dict[str, Any] | None:
    sql = ("SELECT row_to_json(t) FROM (SELECT * FROM icd_data WHERE 病歷號='{}' LIMIT 1) t;").format(chart_no)
    proc = subprocess.run(["docker", "exec", "icd-postgres", "psql", "-U", "icd_user", "-d", "icd_db", "-Atc", sql], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "DB query failed")
    return json.loads(proc.stdout.strip()) if proc.stdout.strip() else None


NORM_MAP, NORM_RULES, SPELLING_MAP, EXCLUDE_DIAG_TERMS = load_norm_assets()


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9/+,\-\s]", " ", _clean(s).lower())


def _resolved_or_history(s: str) -> bool:
    lower = _norm_text(s)
    return bool(re.search(r"\b(history of|hx of|resolved|improved|old)\b", lower)) or any(x in s for x in ["病史", "已改善", "既往"])


def _contains_term(text: str, term: str) -> bool:
    t = _norm_text(text)
    token = _norm_text(term).strip()
    if not token:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
    return re.search(pattern, t) is not None


def _contains_any(text: str, terms: list[str]) -> bool:
    if not terms:
        return True
    return any(_contains_term(text, t) for t in terms if t)


def _canonical_source_field(raw_field: str, evidence_text: str, record: dict[str, Any]) -> str:
    raw = _clean(str(raw_field or ""))
    lower = _norm_text(raw)
    ev = _clean(str(evidence_text or "")).lower()

    # Prefer direct evidence-text matching in chart fields; if multiple match, prefer discharge diagnosis.
    if ev:
        matched_fields: list[str] = []
        for field in EXTRACT_FIELDS:
            text = _clean(str(record.get(field) or "")).lower()
            if text and ev in text:
                matched_fields.append(field)
        if matched_fields:
            matched_fields.sort(key=lambda f: FIELD_PRIORITY.get(f, 99))
            return matched_fields[0]

    token_map = [
        (F_DISCHARGE, ["discharge diagnosis", "discharge", "diagnosis", "出院診斷"]),
        (F_HISTORY, ["history", "clinical history", "past history", "病史"]),
        (F_CHIEF, ["chief complaint", "complaint", "主訴"]),
        (F_COURSE, ["hospital course", "course", "clinical findings", "physical examination", "住院治療經過", "體檢發現"]),
        (F_LAB, ["laboratory", "lab", "test report", "檢驗報告"]),
    ]
    for canonical, tokens in token_map:
        for t in tokens:
            if _contains_term(lower, t):
                return canonical
    return F_HISTORY


def _sorted_by_field_priority(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: (FIELD_PRIORITY.get(str(x.get("sourceField") or ""), 99), int(x.get("seq", 999999))))


def build_normalized_candidates(raw_term: str, context_text: str = "", preferred_term: str = "") -> list[str]:
    raw = _clean(raw_term)
    pref = _clean(preferred_term)
    source_probe = _norm_text(f"{raw} {pref}")
    context = _norm_text(f"{context_text} {raw} {pref}")
    queue: list[str] = [t for t in [pref, raw] if t]
    out: list[str] = []
    seen: set[str] = set()
    for term in queue:
        norm_term = _norm_text(term)
        if not norm_term:
            continue
        corrected = SPELLING_MAP.get(norm_term, "")
        if corrected:
            queue.append(corrected)
        if norm_term in NORM_MAP:
            queue.append(NORM_MAP[norm_term])
    for rule in sorted(NORM_RULES, key=lambda x: int(x.get("priority") or 0), reverse=True):
        source_terms = list(rule.get("source") or [])
        if not any(_contains_term(source_probe, src) for src in source_terms):
            continue
        require_any = list(rule.get("require_any") or [])
        deny_any = list(rule.get("deny_any") or [])
        if require_any and not _contains_any(context, require_any):
            continue
        if deny_any and _contains_any(context, deny_any):
            continue
        queue.append(str(rule.get("target") or ""))
    heuristic_text = _norm_text(raw)
    if "pneumonia" in heuristic_text and "aspiration" not in heuristic_text:
        queue.append("pneumonia, unspecified organism")
    if ("coronary" in heuristic_text or "cad" in heuristic_text) and "angina" in context:
        queue.append("atherosclerotic heart disease with angina pectoris")
    if "coronary" in heuristic_text or "cad" in heuristic_text:
        queue.append("atherosclerotic heart disease")
    for term in queue:
        clean_term = _clean(term)
        key = clean_term.lower()
        if not clean_term or key in seen:
            continue
        seen.add(key)
        out.append(clean_term)
    return out


def normalize(term: str, context_text: str = "", preferred_term: str = "") -> str:
    cands = build_normalized_candidates(term, context_text=context_text, preferred_term=preferred_term)
    return cands[0] if cands else _clean(term)


def _candidate_phrases(text: str) -> list[str]:
    raw = _clean(text)
    lower = _norm_text(raw)
    phrases: list[str] = []
    seen: set[str] = set()
    for src in sorted(NORM_MAP.keys(), key=len, reverse=True):
        if src and _contains_term(lower, src) and src not in seen:
            seen.add(src)
            phrases.append(src)
    chunks = re.split(r"[,，;/、]|(?i:\band\b)|(?i:\bwith\b)|(?i:\bplus\b)|及|和|併|合併", raw)
    for chunk in chunks:
        chunk = _clean(chunk)
        if not chunk:
            continue
        if any(t in chunk.lower() for t in EXCLUDE_DIAG_TERMS):
            continue
        has_ascii = bool(re.search(r"[A-Za-z]", chunk))
        if has_ascii and not (1 <= len(chunk.split()) <= 8):
            continue
        if not has_ascii and not (2 <= len(chunk) <= 24):
            continue
        key = chunk.lower()
        if key not in seen:
            seen.add(key)
            phrases.append(chunk)
    return phrases


def _strip_field_prefix(term: str) -> str:
    t = _clean(term)
    t = re.sub(r"^(?:出院診斷|主訴|病史|檢驗報告|體檢發現|住院治療經過)\s*[:：]?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:chief complaint|history(?:\s+of)?|hospital course|course|diagnosis|lab(?:oratory)?)\s*[:：]?\s*", "", t, flags=re.IGNORECASE)
    return _clean(t)


def _is_non_diagnostic_phrase(term: str, evidence_text: str = "") -> bool:
    t = _norm_text(term)
    ev = _norm_text(evidence_text)
    joined = f"{t} {ev}".strip()
    if not joined:
        return True
    if t in {"resolved", "improved", "stable", "normal", "negative"}:
        return True
    # Exclude normal status / consciousness / stable condition descriptions.
    normal_markers = [
        "conscious", "alert", "clear", "oriented", "awake", "stable", "normal",
        "wnl", "nad", "no acute distress", "vital signs stable", "mental status",
        "意識", "清楚", "清醒", "生命徵象穩定", "正常", "無異常", "神智清楚",
    ]
    for m in normal_markers:
        if _contains_term(joined, m):
            # Keep if there is explicit disease/symptom wording in the term itself.
            if not re.search(r"(failure|disease|syndrome|pain|infection|pneumonia|angina|糖尿病|高血壓|心衰竭|肺炎|感染|疼痛)", t):
                return True
    # Pure exam-status phrases often end up as noise terms.
    if re.fullmatch(r"(conscious|alert|awake|oriented|stable|normal|clear)(\s+[a-z]+){0,3}", t):
        return True
    return False


def rule_extract(record: dict[str, Any], fields_to_extract: list[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    seq = 0
    fields = fields_to_extract or EXTRACT_FIELDS
    for field in fields:
        text = str(record.get(field) or "")
        if not text:
            continue
        parts = re.split(r"[\n。;；]+|(?<=[.!?])\s+", text)
        for p in parts:
            p = _clean(p)
            if len(p) < 3:
                continue
            if any(x in p.lower() for x in EXCLUDE_DIAG_TERMS):
                continue
            for phrase in (_candidate_phrases(p) or [p]):
                phrase = _strip_field_prefix(phrase)
                if len(phrase) < 2 or _negated(phrase):
                    continue
                if _is_non_diagnostic_phrase(phrase, p):
                    continue
                if field == F_LAB and not any(x in phrase.lower() for x in ["abnormal", "high", "low", "elevated", "decreased", "↑", "↓", "\u7570\u5e38"]):
                    continue
                norm_cands = build_normalized_candidates(phrase, context_text=p)
                term = norm_cands[0] if norm_cands else _clean(phrase)
                key = (term.lower(), field)
                if key in seen:
                    continue
                seen.add(key)
                category = "history" if field == F_HISTORY else ("finding" if field == F_LAB else "disease")
                out.append(
                    {
                        "term": _clean(phrase),
                        "normalized_term": term,
                        "normalization_candidates": norm_cands,
                        "sourceField": field,
                        "evidenceText": p,
                        "category": category,
                        "assertion": "history" if _resolved_or_history(p) else "present",
                        "lowered_priority": False,
                        "confidence": 0.55,
                        "needs_review": True,
                        "queryable": True,
                        "seq": seq,
                        "extractor": "rule",
                    }
                )
                seq += 1
    return out


def _merge_discharge_first(discharge_items: list[dict[str, Any]], other_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_terms: set[str] = set()

    def push(items: list[dict[str, Any]]) -> None:
        for item in items:
            key = _clean(str(item.get("normalized_term") or item.get("term") or "")).lower()
            if not key or key in seen_terms:
                continue
            seen_terms.add(key)
            merged.append(item)

    push(discharge_items)
    push(other_items)
    return merged


def llm_extract(record: dict[str, Any]) -> list[dict[str, Any]]:
    if not LLM_API_URL:
        return []
    content = "\n\n".join([f"[{f}]\n{str(record.get(f) or '').strip()}" for f in EXTRACT_FIELDS if str(record.get(f) or '').strip()])
    if not content:
        return []
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Extract ICD-10 diagnosable keywords. Return JSON {\"items\": [...]} with raw_term, normalized_term, category, assertion, evidence_field, evidence_text, lowered_priority, confidence."},
            {"role": "user", "content": content},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    req = urllib.request.Request(LLM_API_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            outer = json.loads(resp.read().decode("utf-8", errors="ignore"))
        msg = outer.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Accept plain JSON and fenced ```json ... ``` output.
        msg_text = str(msg).strip()
        if msg_text.startswith("```"):
            fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", msg_text, flags=re.DOTALL | re.IGNORECASE)
            if fenced:
                msg_text = fenced.group(1).strip()

        obj = json.loads(msg_text)
        if isinstance(obj, dict):
            items = obj.get("items", [])
        elif isinstance(obj, list):
            items = obj
        else:
            items = []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        raw = _clean(str(item.get("raw_term") or ""))
        if len(raw) < 3:
            continue
        raw = _strip_field_prefix(raw)
        ev_text = str(item.get("evidence_text") or raw)
        if str(item.get("assertion") or "present").lower() == "absent" or _negated(ev_text):
            continue
        if _is_non_diagnostic_phrase(raw, ev_text):
            continue
        source_field = _canonical_source_field(str(item.get("evidence_field") or ""), ev_text, record)
        norm_cands = build_normalized_candidates(raw, context_text=ev_text, preferred_term=_clean(str(item.get("normalized_term") or raw)))
        assertion = str(item.get("assertion") or "present").lower()
        if assertion not in {"present", "history", "uncertain"}:
            assertion = "history" if _resolved_or_history(ev_text) else "present"
        out.append({
            "term": raw,
            "normalized_term": norm_cands[0] if norm_cands else raw,
            "normalization_candidates": norm_cands,
            "sourceField": source_field,
            "evidenceText": ev_text,
            "category": str(item.get("category") or "disease"),
            "assertion": assertion,
            "lowered_priority": bool(item.get("lowered_priority", False)),
            "confidence": max(0.0, min(1.0, _f(item.get("confidence"), 0.0))),
            "needs_review": _f(item.get("confidence"), 0.0) < 0.60,
            "queryable": True,
            "seq": i,
            "extractor": "llm",
        })
    return out


def llm_correct_spelling(extracted: list[dict[str, Any]], record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    if not LLM_API_URL or not LLM_SPELLING_ENABLED:
        return extracted, []
    targets: list[dict[str, Any]] = []
    for i, item in enumerate(extracted):
        term = _clean(str(item.get("term") or ""))
        if not term or not re.search(r"[A-Za-z]", term):
            continue
        if len(term) < 3:
            continue
        targets.append(
            {
                "idx": i,
                "term": term,
                "normalized_term": _clean(str(item.get("normalized_term") or "")),
                "field": str(item.get("sourceField") or ""),
                "evidence_text": _clean(str(item.get("evidenceText") or "")),
            }
        )
    if not targets:
        return extracted, []
    context_parts: list[str] = []
    for field in EXTRACT_FIELDS:
        v = _clean(str(record.get(field) or ""))
        if v:
            context_parts.append(f"[{field}] {v[:280]}")
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You correct English medical spelling only. "
                    "Do not translate. Keep diagnosis meaning unchanged. "
                    "Return strict JSON object {\"items\":[{\"idx\":int,\"corrected_term\":str,\"corrected_normalized_term\":str}]}. "
                    "If no change, return original text."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "record_context": context_parts,
                        "targets": targets,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    req = urllib.request.Request(LLM_API_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            outer = json.loads(resp.read().decode("utf-8", errors="ignore"))
        msg = outer.get("choices", [{}])[0].get("message", {}).get("content", "")
        msg_text = str(msg).strip()
        if msg_text.startswith("```"):
            fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", msg_text, flags=re.DOTALL | re.IGNORECASE)
            if fenced:
                msg_text = fenced.group(1).strip()
        obj = json.loads(msg_text)
        rows = obj.get("items", []) if isinstance(obj, dict) else []
    except Exception:
        return extracted, []
    out = [dict(i) for i in extracted]
    changed_terms = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = int(row.get("idx", -1))
        if not (0 <= idx < len(out)):
            continue
        old_term = _clean(str(out[idx].get("term") or ""))
        corrected_term = _clean(str(row.get("corrected_term") or old_term))
        corrected_norm = _clean(str(row.get("corrected_normalized_term") or corrected_term or old_term))
        if corrected_term and corrected_term != old_term:
            out[idx]["spelling_corrected_from"] = old_term
            out[idx]["term"] = corrected_term
            changed_terms += 1
        if corrected_norm:
            out[idx]["normalized_term"] = corrected_norm
    notes = [f"LLM 英文拼字修正已啟用，修正 {changed_terms} 個關鍵詞。"] if changed_terms > 0 else []
    return out, notes


def extract_keywords(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    discharge_seed = rule_extract(record, fields_to_extract=[F_DISCHARGE])
    for item in discharge_seed:
        item["confidence"] = max(_f(item.get("confidence"), 0.0), 0.75)
        item["needs_review"] = False
        item["extractor"] = "rule_discharge"

    llm = llm_extract(record)
    if llm:
        llm = _sorted_by_field_priority(llm)
        notes = ["抽詞流程：LLM"]
        llm, correction_notes = llm_correct_spelling(llm, record)
        llm = _sorted_by_field_priority(llm)
        merged = _merge_discharge_first(_sorted_by_field_priority(discharge_seed), llm)
        merged = _sorted_by_field_priority(merged)
        notes.extend(correction_notes)
        if discharge_seed:
            notes.append("已套用出院診斷優先：先納入出院診斷抽詞，再以其他欄位補充推測。")
        if any(i.get("needs_review") for i in merged):
            notes.append("部分關鍵詞低信心，已標記 needs_review=true。")
        return merged, notes
    ruled = rule_extract(record)
    ruled = _sorted_by_field_priority(ruled)
    ruled, correction_notes = llm_correct_spelling(ruled, record)
    ruled = _sorted_by_field_priority(ruled)
    notes = ["LLM 抽詞不可用或無有效輸出，已改用規則式抽詞。"]
    notes.extend(correction_notes)
    return ruled, notes


def _score_candidate(code_row: dict[str, Any], keyword: str, fields: list[str], meta: dict[str, Any]) -> float:
    code = str(code_row.get("code") or "")
    name = f"{str(code_row.get('name_en') or '').lower()} {str(code_row.get('name_zh') or '').lower()}"
    has_discharge = F_DISCHARGE in fields
    field_weight = 0.55 if has_discharge else 0.08
    specificity = 0.2 if "." in code else 0.05
    keyword_tokens = [t for t in re.split(r"[^a-z0-9]+", keyword.lower()) if len(t) > 2]
    lexical = min(0.3, 0.1 * sum(1 for t in keyword_tokens if t in name))
    confidence = 0.2 * max(0.0, min(1.0, _f(meta.get("confidence"), 0.0)))
    inferred_penalty = 0.0 if has_discharge else -0.18
    history_penalty = -0.35 if bool(meta.get("history_only")) else 0.0
    lowered_penalty = -0.1 if bool(meta.get("lowered_priority")) else 0.0
    symptom_penalty = -0.15 if code.startswith("R") and not bool(meta.get("history_only")) else 0.0
    return round(field_weight + specificity + lexical + confidence + inferred_penalty + history_penalty + lowered_penalty + symptom_penalty, 4)


def choose_codes(matches: list[dict[str, Any]], keyword_meta: dict[str, dict[str, Any]], focus: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    picked: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for m in matches:
        if not m.get("candidates"):
            continue
        cand = m["candidates"][0]
        meta = keyword_meta.get(str(m.get("keyword")), {})
        picked.append({"keyword": m.get("keyword"), "cand": cand, "fields": m.get("sourceFields", []), "seq": int(m.get("keywordSeq", 999999)), "history_only": bool(meta.get("history_only", False)), "evidence": meta.get("evidence", []), "confidence": max(0.45, _f(meta.get("confidence"), 0.55)), "cand_score": _f(cand.get("_score"), 0.0)})
    if not picked:
        return [], excluded
    picked.sort(key=lambda x: (min([FIELD_PRIORITY.get(f, 99) for f in x["fields"]] or [99]), -x.get("cand_score", 0.0), x["seq"]))
    p = picked[0]
    final = [{"role": "主診斷", "code": p["cand"]["code"], "title": p["cand"].get("name_en") or p["cand"].get("name_zh") or "", "keyword": p["keyword"], "evidence_fields": p["fields"], "evidence": p["evidence"], "confidence": round(p["confidence"], 2)}]
    if focus == "principal_only":
        return final, excluded
    used = {p["cand"]["code"]}
    used_pref = {str(p["cand"]["code"])[:3]}
    for s in picked[1:]:
        code = str(s["cand"].get("code") or "")
        if not code or code in used:
            continue
        if s.get("history_only"):
            excluded.append({"term": str(s.get("keyword") or ""), "reason": "history_only"})
            continue
        if code[:3] in used_pref:
            excluded.append({"term": str(s.get("keyword") or ""), "reason": "duplicate_or_less_specific"})
            continue
        final.append({"role": "次診斷", "code": code, "title": s["cand"].get("name_en") or s["cand"].get("name_zh") or "", "keyword": s["keyword"], "evidence_fields": s["fields"], "evidence": s["evidence"], "confidence": round(max(0.45, s["confidence"]), 2)})
        used.add(code)
        used_pref.add(code[:3])
        if len(final) >= 8:
            break
    return final, excluded


def prioritize_keywords(norm_map: dict[str, set[str]]) -> tuple[list[str], bool]:
    items = list(norm_map.items())
    has_discharge = any(F_DISCHARGE in fields for _, fields in items)
    ordered = sorted(
        items,
        key=lambda kv: (
            0 if F_DISCHARGE in kv[1] else 1,
            min((FIELD_PRIORITY.get(f, 99) for f in kv[1]), default=99),
            kv[0].lower(),
        ),
    )
    return [k for k, _ in ordered], has_discharge


def build_reply_text(final_codes: list[dict[str, str]]) -> str:
    if not final_codes:
        return "未找到可編碼的 ICD-10-CM 診斷。"
    return "\n".join([f"{idx}:{item.get('code', '')}" for idx, item in enumerate(final_codes)])


def build_dictionary_results(query: str) -> list[dict[str, str]]:
    sid = mcp_initialize()
    rows = search_codes(sid, query, 50)
    return [{"code": r.get("code", ""), "title": r.get("name_en") or "", "notes": r.get("name_zh") or "", "chapter": "ICD-10-CM"} for r in rows[:20]]


def extract_chart_no(command: str, payload_chart_no: Any) -> str:
    direct = _clean(str(payload_chart_no or ""))
    if re.fullmatch(r"\d{4,10}", direct):
        return direct
    text = _clean(str(command or ""))
    m = re.search(r"病歷號\s*([0-9]{4,10})", text)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9]{4,10})\b", text)
    if m:
        return m.group(1)
    return ""


class ApiHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/api/health"):
            json_response(self, 200, {"ok": True})
            return
        if self.path.startswith("/api/dictionary"):
            q = ""
            if "?" in self.path:
                _, query_str = self.path.split("?", 1)
                for token in query_str.split("&"):
                    if token.startswith("q="):
                        q = urllib.parse.unquote_plus(token[2:])
                        break
            if not q.strip():
                json_response(self, 400, {"error": "q is required"})
                return
            try:
                results = build_dictionary_results(q.strip())
            except Exception as exc:  # noqa: BLE001
                json_response(self, 500, {"error": str(exc)})
                return
            json_response(self, 200, {"query": q, "results": results})
            return
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/code":
            json_response(self, 404, {"error": "Not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            json_response(self, 400, {"error": "Invalid JSON body"})
            return
        command = str(payload.get("command") or "").strip()
        chart_no = extract_chart_no(command, payload.get("chart_no"))
        focus = str(payload.get("focus") or "full").strip().lower()
        if focus not in {"full", "principal_only"}:
            focus = "full"
        if not chart_no:
            json_response(self, 400, {"error": "chart_no not found in command"})
            return

        try:
            record = fetch_chart(chart_no)
            if not record:
                json_response(self, 404, {"error": f"chart {chart_no} not found"})
                return
            extracted, notes = extract_keywords(record)
            norm_map: dict[str, set[str]] = {}
            norm_seq: dict[str, int] = {}
            meta: dict[str, dict[str, Any]] = {}
            for item in extracted:
                if not item.get("queryable", True):
                    continue
                src = str(item.get("sourceField") or "")
                seq = int(item.get("seq", 999999))
                norm_cands = [str(k).strip() for k in list(item.get("normalization_candidates") or []) if str(k).strip()]
                if not norm_cands:
                    fallback = normalize(str(item.get("normalized_term") or item.get("term") or "").strip(), context_text=str(item.get("evidenceText") or ""))
                    norm_cands = [fallback] if fallback else []
                assertion = str(item.get("assertion") or "").lower()
                for keyword in norm_cands:
                    norm_map.setdefault(keyword, set()).add(src)
                    if keyword not in norm_seq or seq < norm_seq[keyword]:
                        norm_seq[keyword] = seq
                    m = meta.setdefault(keyword, {"history_only": True, "confidence": 0.0, "evidence": [], "needs_review": False, "lowered_priority": True})
                    if str(item.get("category") or "").lower() != "history" and assertion != "history":
                        m["history_only"] = False
                    if not bool(item.get("lowered_priority", False)):
                        m["lowered_priority"] = False
                    m["confidence"] = max(_f(m.get("confidence"), 0.0), _f(item.get("confidence"), 0.0))
                    m["needs_review"] = bool(m.get("needs_review")) or bool(item.get("needs_review"))
                    ev = {"field": src or F_HISTORY, "text": str(item.get("evidenceText") or item.get("term") or "")}
                    if ev not in m["evidence"]:
                        m["evidence"].append(ev)

            sid = mcp_initialize()
            record_text = json.dumps(record, ensure_ascii=False).lower()
            req_id = 10
            matches: list[dict[str, Any]] = []
            excluded_candidates: list[dict[str, str]] = []
            keywords_to_code, use_discharge_only = prioritize_keywords(norm_map)
            for keyword in keywords_to_code:
                all_cands: list[dict[str, Any]] = []
                seen_codes: set[str] = set()
                for q in [keyword, keyword.lower(), re.split(r"\b(?:with|without|due to|secondary to|associated with|in|over)\b", keyword.lower(), maxsplit=1)[0].strip()]:
                    if not q:
                        continue
                    for c in search_codes(sid, q, req_id):
                        req_id += 1
                        code = str(c.get("code") or "")
                        if code and code not in seen_codes:
                            seen_codes.add(code)
                            all_cands.append(c)
                non_symptom = any(not str(c.get("code") or "").startswith("R") for c in all_cands)
                filtered: list[dict[str, Any]] = []
                for c in all_cands:
                    code = str(c.get("code") or "")
                    if code.startswith("O") and not any(t in record_text for t in ["pregnan", "妊娠", "產後", "childbirth"]):
                        excluded_candidates.append({"term": keyword, "code": code, "title": c.get("name_en") or c.get("name_zh") or "", "reason": "context_mismatch"})
                        continue
                    if code.startswith("P") and not any(t in record_text for t in ["newborn", "neonatal", "新生兒"]):
                        excluded_candidates.append({"term": keyword, "code": code, "title": c.get("name_en") or c.get("name_zh") or "", "reason": "context_mismatch"})
                        continue
                    if non_symptom and code.startswith("R"):
                        excluded_candidates.append({"term": keyword, "code": code, "title": c.get("name_en") or c.get("name_zh") or "", "reason": "symptom_overridden"})
                        continue
                    filtered.append(c)
                parents = [str(c.get("code") or "") for c in filtered[:3] if "." not in str(c.get("code") or "")]
                for pcode in parents:
                    for c in nearby_codes(sid, pcode, req_id):
                        req_id += 1
                        code = str(c.get("code") or "")
                        if code and code not in seen_codes:
                            seen_codes.add(code)
                            filtered.append(c)
                kw_fields = sorted(norm_map.get(keyword, set()))
                kw_meta = meta.get(keyword, {})
                scored = []
                for c in filtered:
                    row = dict(c)
                    row["_score"] = _score_candidate(row, keyword, kw_fields, kw_meta)
                    scored.append(row)
                scored.sort(key=lambda x: _f(x.get("_score"), 0.0), reverse=True)
                matches.append({"keyword": keyword, "sourceFields": kw_fields, "keywordSeq": norm_seq.get(keyword, 999999), "candidates": scored[:15], "needs_review": bool(meta.get(keyword, {}).get("needs_review", False))})

            selected, excluded_rule = choose_codes(matches, meta, focus)
            excluded_candidates.extend(excluded_rule)
            final_codes = [{"role": i["role"], "code": i["code"], "title": i["title"]} for i in selected]
        except urllib.error.URLError as exc:
            json_response(self, 502, {"error": f"MCP connection error: {exc}"})
            return
        except Exception as exc:  # noqa: BLE001
            json_response(self, 500, {"error": str(exc)})
            return

        principal = next((i for i in selected if i.get("role") == "主診斷"), None)
        secondaries = [i for i in selected if i.get("role") == "次診斷"]
        coding_notes = list(notes)
        if use_discharge_only:
            coding_notes.append("本次以出院診斷關鍵詞優先決策，並已納入其他欄位作為次診斷與補充依據。")
        else:
            coding_notes.append("出院診斷欄位未抽取到可查碼關鍵詞，暫以其他欄位推測編碼，請人工複核。")
        if not principal:
            coding_notes.append("找不到可確立主診斷的候選碼。")
        if principal and F_DISCHARGE not in principal.get("evidence_fields", []):
            coding_notes.append("主診斷非直接來自出院診斷欄位，建議人工複核。")
        if any(str(i.get("code") or "").startswith("R") for i in selected):
            coding_notes.append("含症狀碼，請確認是否已有更具體確診可取代。")

        principal_out = None
        if principal:
            principal_out = {"code": principal.get("code", ""), "display": principal.get("title", ""), "title": principal.get("title", ""), "evidence": principal.get("evidence", []), "evidence_fields": principal.get("evidence_fields", []), "keyword": principal.get("keyword", ""), "confidence": _f(principal.get("confidence"), 0.0)}
        secondary_out = [{"code": i.get("code", ""), "display": i.get("title", ""), "title": i.get("title", ""), "evidence": i.get("evidence", []), "evidence_fields": i.get("evidence_fields", []), "keyword": i.get("keyword", ""), "confidence": _f(i.get("confidence"), 0.0)} for i in secondaries]

        result = {
            "chartNo": chart_no,
            "timeline": ["讀取病歷", "抽取關鍵字", "術語正規化", "MCP 查碼", "主次診斷決策"],
            "focus": focus,
            "extracted": extracted,
            "extracted_keywords": extracted,
            "mcpMatches": matches,
            "mcp_matches": matches,
            "principal_diagnosis": principal_out,
            "secondary_diagnoses": secondary_out,
            "excluded_candidates": excluded_candidates,
            "coding_notes": coding_notes,
            "record_summary": {"admit_date": record.get("\u5165\u9662\u65e5"), "department": record.get("\u5165\u9662\u79d1\u5225"), "chief_complaint": record.get(F_CHIEF)},
            "finalCodes": final_codes,
            "replyText": build_reply_text(final_codes),
        }
        json_response(self, 200, result)


def run() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8080), ApiHandler)
    print("Server running on http://127.0.0.1:8080")
    server.serve_forever()


if __name__ == "__main__":
    run()
