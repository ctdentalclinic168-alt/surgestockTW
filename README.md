# 盤後籌碼訊號日報

每晚 20:05（台灣時間）自動更新兩份名單：

1. **外資轉買**：近 5 個交易日外資買超前 10 名，且先前連續賣超 ≥3 日後轉買，且週線由下轉彎向上
2. **00991A 加碼**：主動復華未來 50 較前一日加碼（含新進場）的個股，且週線由下轉彎向上

## 部署步驟（一次設定，之後全自動）

1. 在 GitHub 建新 repo（例如 `tw-signal`），把整包檔案上傳（保留資料夾結構，`.github/workflows/update.yml` 一定要在）。
2. Repo → **Settings → Actions → General → Workflow permissions** → 勾選 **Read and write permissions** → Save。
3. Repo → **Settings → Pages** → Source 選 **Deploy from a branch** → Branch 選 `main` / root → Save。
   網址會是 `https://<帳號>.github.io/tw-signal/`
4. Repo → **Actions** → 選 `daily-update` → **Run workflow** 手動跑第一次。
   跑完後 `data/latest.json` 會自動 commit，網頁即可看到資料。

之後每個交易日 20:05 會自動執行（GitHub 排程偶爾會延遲 5–15 分鐘，屬正常）。

## 00991A 持股資料來源（需要你做一次）

復華投信的申購買回清單頁面是 JS 動態載入，腳本內建的抓取可能失敗。兩種解法擇一：

**方法 A（建議，一次搞定）**
1. 用 Chrome 開 https://www.fhtrust.com.tw/ETF/trade_list ，選 00991A
2. 按 F12 → Network → 重新整理 → 找到回傳持股 JSON 的那支 API（Preview 裡看得到股票代號與股數）
3. 複製該網址，到 Repo → **Settings → Secrets and variables → Actions → Variables** → New variable
   名稱 `PCF_URL`，值貼上該網址

**方法 B（手動備援）**
把持股存成 `data/manual_holdings.csv`（格式：`代號,名稱,股數` 每行一檔）commit 上去，腳本會優先讀這個檔。

> 注意：加碼比對需要「至少兩天」的持股快照，第一天執行只會建立基準，第二天起才有加碼名單。
> 條件一同理：外資買賣超需累積約 8 個交易日（腳本每天會自動回補歷史，第一次手動執行就會一次抓齊）。

## 可調參數（scripts/update.py 開頭）

| 參數 | 預設 | 說明 |
|---|---|---|
| `WEEK_WINDOW` | 5 | 「每週」= 近幾個交易日累計買超 |
| `SELL_STREAK_MIN` | 3 | 轉買前需連續賣超天數 |
| `TOP_N` | 10 | 取週買超前幾名 |

**週線翻揚定義**：本週收盤 > 上週收盤，且上週收盤 ≤ 上上週收盤（V 轉）。想改成別的定義（如週 MA 上彎），改 `weekly_turn_up()` 即可。

## 範圍說明

- 外資買賣超使用證交所 T86（**上市**個股）；上櫃個股不在條件一範圍內。
- 00991A 加碼股若為上櫃，腳本會嘗試 TPEx API 取股價；若失敗該檔會標示「週資料不足」。
- 週三、四執行時「本週收盤」為當週最新收盤（週線尚未完成），屬即時判斷而非收週確認。
