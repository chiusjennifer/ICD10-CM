---
name: icd10-mcp-encoder
description: 透過擷取病歷各臨床欄位的醫療關鍵字，並使用台灣醫療 MCP 伺服器驗證候選碼，完成 PostgreSQL 病人資料的 ICD-10-CM 診斷編碼。當使用者要求特定病歷編碼、主次診斷排序，或需要附證據來源的出院編碼結果時使用此技能。
---

# ICD10 MCP 編碼器（可執行模板）

## 適用情境

- 使用者提供單一 `病歷號`，要求輸出本次住院 ICD-10-CM 編碼。
- 需要回傳主診斷、次診斷、排除候選與證據欄位。

## 先決條件

- PostgreSQL 容器可讀取 `icd_data`。
- MCP session 已初始化，且可呼叫：
  - `search_medical_codes`（`type='diagnosis'`）
  - `get_nearby_codes`
- 載入詞彙正規化表：[references/normalization.md](references/normalization.md)
- 若可用，優先載入結構化規則：[references/normalization_rules.json](references/normalization_rules.json)

## 輸入契約

```json
{
  "chart_no": "<CHART_NO>",
  "encounter_date": "<optional YYYY-MM-DD>",
  "focus": "<optional principal_only|full>"
}
```

- `chart_no` 必填。
- `focus=principal_only` 時只輸出主診斷，其餘欄位可為空陣列。

## 步驟 1：查詢病歷

```powershell
docker exec icd-postgres psql -U icd_user -d icd_db -Atc "SELECT row_to_json(t) FROM (SELECT * FROM icd_data WHERE 病歷號='<CHART_NO>' LIMIT 1) t;"
```

執行規則：
- 查無資料：停止，回報 `找不到病歷號`。
- 查到資料：僅保留有值欄位，進入抽詞。

## 步驟 2：候選證據欄位

優先抽取以下欄位（由高到低）：

1. `出院診斷`
2. `病史`
3. `主訴`
4. `體檢發現/住院治療經過`
5. `檢驗報告`（只保留具臨床意義異常）

欄位清理規則：
- 刪除純處置敘述（如 `PCI`, `stent`, `angiography`）作為診斷候選。
- 同句若有明確疾病與被完整解釋之症狀，優先保留疾病。
- 否定句不納入候選（例：`denies chest pain`, `no pneumonia`）。

## 步驟 3：LLM 抽詞（第一層）

目標：由自由文字抽出 ICD 友善候選詞，不直接決定最終碼。

### 建議提示詞模板

```text
你是 ICD-10-CM 診斷抽詞器。請從病歷文字中抽出「可對應診斷」的關鍵詞。

限制：
1) 只抽診斷/疾病/症候群/有臨床意義的異常，不抽處置與檢查名稱。
2) 若句子是否定、排除、已排除診斷，不要抽出。
3) 同義詞先保留原詞，另提供 normalized_term。
4) 若同時有具體疾病與症狀，症狀標示 lowered_priority=true。

輸出 JSON 陣列，每筆格式：
{
  "raw_term": "",
  "normalized_term": "",
  "category": "disease|symptom|finding|history",
  "assertion": "present|absent|uncertain",
  "evidence_field": "",
  "evidence_text": "",
  "lowered_priority": false,
  "confidence": 0.0
}
```

### LLM 抽詞保底規則

- 若 LLM 無輸出或 JSON 不合法：改用規則式抽詞（英文醫療詞、縮寫詞典、逗號分句）。
- 若 `confidence < 0.60`：保留但標記 `needs_review=true`。

## 步驟 4：正規化（第二層）

對每個 `raw_term` 套用正規化規則（優先 `normalization_rules.json`，其次 `normalization.md`）：

- 優先順序：`raw_term` 查碼失敗 -> 用 `normalized_term` 重查 -> 用較廣義詞重查。
- 常見縮寫必轉：`CAD`, `HTN`, `HLD`, `PVC`, `VPC`, `LVH`。
- 拼字修正後重查（例：`pnuemonia` -> `pneumonia`）。

## 步驟 5：MCP 查碼與候選收斂

對每個候選詞執行：

1. `search_medical_codes(term, type='diagnosis')`
2. 若無結果：套用正規化同義詞重查。
3. 若結果過廣：用 `get_nearby_codes` 收斂到最符合臨床語意的子碼。

排除原則：
- 無妊娠/新生兒脈絡，不用該專屬碼群。
- 有具體確診疾病時，不選純症狀碼。
- 只有歷史病史且不影響本次住院，不列次診斷。

## 步驟 6：主次診斷決策

主診斷（`principal_diagnosis`）選擇規則：
- 能解釋本次住院主要資源使用與處置。
- 證據優先：`出院診斷` > `住院治療經過` > 其他欄位。

次診斷（`secondary_diagnoses`）納入條件：
- 有文件支持且為現行共病/併發症。
- 影響評估、治療或監測。

衝突解法：
- 同義重複碼只留一筆，保留證據最強者。
- 粒度衝突（廣義 vs 特定）保留特定碼，廣義碼移入排除清單。

## 輸出契約

```json
{
  "principal_diagnosis": {
    "code": "",
    "display": "",
    "evidence": [
      {
        "field": "",
        "text": ""
      }
    ],
    "confidence": 0.0
  },
  "secondary_diagnoses": [
    {
      "code": "",
      "display": "",
      "evidence": [
        {
          "field": "",
          "text": ""
        }
      ],
      "confidence": 0.0
    }
  ],
  "excluded_candidates": [
    {
      "term": "",
      "reason": "symptom_overridden|insufficient_evidence|context_mismatch|history_only|duplicate_or_less_specific"
    }
  ],
  "coding_notes": [
    ""
  ],
  "finalCodes": [
    ""
  ]
}
```

一致性要求（前端結果 = 回覆結果）：
- 回覆中的代碼清單必須由 `finalCodes` 生成，不可人工改碼。
- 建議顯示：`0:<code>`、`1:<code>` ...，順序須與 `finalCodes` 一致。

## 最小驗收清單

- 至少 1 個主診斷碼，且有證據欄位。
- 每個次診斷碼都要有對應證據。
- `excluded_candidates` 必須可追溯排除原因。
- 若存在低信心或語意歧義，`coding_notes` 要明確記錄。
- `finalCodes` 與回覆顯示順序一致。

## 失敗處理

- DB 查無資料：`找不到病歷號`。
- MCP 無法連線或查無可用碼：保留抽詞結果，回報 `需要人工複核`。
- LLM 抽詞異常：改走規則式抽詞流程，並在 `coding_notes` 註記。
