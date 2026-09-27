# RetroCast Skill 對話流程範本

## 標準流程（5 步）

```
1. 列出 profile → 2. 收集欄位 → 3. 跑 analyze → 4. 產 HTML（選擇性） → 5. Relay 結果
```

## 對話範例 A：完整分析（使用者主動請求）

```
U: 幫我跑 kadela 的分析
A: 好的，請確認幾個參數：
   - 回測年數 N？預設 10 年。
   - 退休相關參數用預設（55/60/90、月提款 30000）嗎？
U: N 用 5 年，退休參數 kadela 是 55→60→90、月提款 80000、月年金 50000、年金 60 歲開始
A: [build body, run analyze, run export forecast]
   分析完成（19 秒）。摘要：
   - 目前資產：7,304,253 NT$
   - 共同期間 5 年 CAGR：21.18%
   - 波動率 / MDD：17.95% / -29.75%
   - Sharpe：1.196
   - 5 年後 Base 情境：19.63% 年化（pv 7,286,619）
   - F2 退休存活率：96.3%（最早破產 72 歲）
   - 最佳化狀態：SUCCESS
   
   要不要也產 rebalance 報告？
```

## 對話範例 B：使用者只給部分參數

```
U: 跑一下 sample_stock 的回測
A: sample_stock 是公開範例，用預設參數分析：
   - N = 10（預設）
   - 退休 55→65→90（預設）
   - 月提款 0（預設，不模擬退休提款）
   確認嗎？
U: 確認
A: [analyze + export forecast]
   完成。摘要：[relay summary]
```

## 對話範例 C：只跑健康檢查

```
U: RetroCast 還能跑嗎？
A: [retrocast_cli.py health --v2]
   健康檢查全綠：
   - python / pandas OK
   - FinMind token ✓
   - 6 個 v2 module 都 importable
   - profiles: kadela, sample_stock
```

## 對話範例 D：錯誤處理

```
U: 跑 foo 的分析
A: [analyze -i body]
   失敗：`PROFILE_NOT_FOUND` — foo.csv 不存在於 user_profile/
   目前可用 profile：kadela, sample_stock
   
   要我幫你：
   A. 換其中一個？
   B. 上傳 foo 的 CSV（你給路徑）？
```

## 對話範例 E：使用者想看報告具體內容

```
U: 報告裡的三模式 KPI 完整列出來
A: [從 stdout summary 區塊直接 relay，不打開 HTML]
   Common mode (2014-10-31 → 2026-08-31, 11.83 年)：
   - 累積總報酬率：870.78%
   - CAGR：21.18%
   - MDD：-29.75%
   - 波動率：17.95%
   - Sharpe：1.196
   
   Dynamic mode (2000-01-04 → 2026-08-31, 26.66 年)：
   - 累積總報酬率：699.69%
   - CAGR：8.11%
   - MDD：-34.73%
   - 波動率：12.87%
   - Sharpe：0.683
   
   Full mode (2000-01-04 → 2026-08-31, 26.66 年)：
   - 累積總報酬率：1739.45%
   - CAGR：11.54%
   - MDD：-52.30%
   - 波動率：19.68%
   - Sharpe：0.665
```

## 關鍵提醒

- **不要解讀數字**：relay `21.18%` 不等於說「這個報酬率很好」。Agent 是 messenger，不是 advisor。
- **不要做計算**：F2 存活率 96.3% + 月提款 80000 ≠ 自行算出「30 年後資產」。那要再跑一次 sequence_risk 看 path。
- **報告檔路徑絕對**：永遠給完整路徑 + 副檔名，讓使用者可以直接 `xdg-open` 或瀏覽器開。
- **錯誤時不要瞎猜**：`code=TOKEN_MISSING` → 請使用者貼 token；`code=PROFILE_NOT_FOUND` → 列出現有 profile 請選；不要 try 修東修西。
