# 📈 VCP Scanner — Mark Minervini 自動選股系統

> 基於 Mark Minervini《Trade Like a Stock Market Wizard》的 **Volatility Contraction Pattern (VCP)** 方法論，自動掃描美股前 1000 大股票，每個交易日收盤後寄送符合條件的股票清單至您的信箱。

---

## 🧠 VCP 核心要點（MM 方法論）

### 什麼是 VCP？

VCP 是一種**價格波動收縮型態**（Volatility Contraction Pattern），是 Mark Minervini 在多年實戰中總結出的高勝率突破前型態。

```
     ┌─ 第1次收縮 ─┐  ┌─ 第2次 ─┐  ┌─ 第3次（最緊）─┐
     │   -20%      │  │  -12%   │  │    -6%         │  ← 突破！
─────┘             └──┘         └──┘                 └──────▶
     Volume ██████      ████       ██        █ (放量突破)
```

### VCP 的 6 大核心原則

| # | 原則 | 說明 |
|---|------|------|
| 1 | **波動收縮** | 每次回撤幅度比前一次更小（例：-25% → -15% → -8% → -4%） |
| 2 | **量能萎縮** | 每次整理過程中成交量遞減，代表賣壓耗盡 |
| 3 | **收縮次數** | 理想 2-4 次，最後一次收縮應極緊（<5%） |
| 4 | **基部深度** | 整體回撤 15-50%，過淺或過深皆不理想 |
| 5 | **放量突破** | 突破樞軸價位時成交量需 ≥ 均量 150% 以上 |
| 6 | **Stage 2 確認** | 股票必須處於 Stan Weinstein 的第二階段（上升趨勢） |

---

## 📊 MM 趨勢模板（8 項必要條件）

Minervini 的趨勢模板是進入 VCP 交易的**前置篩選器**，全部 8 項必須通過：

```
✅ 1. 股價 > 150日均線
✅ 2. 股價 > 200日均線
✅ 3. 150日均線 > 200日均線
✅ 4. 200日均線呈上升趨勢（過去一個月）
✅ 5. 50日均線 > 150日均線 且 50日均線 > 200日均線
✅ 6. 股價 > 50日均線
✅ 7. 股價距 52 週高點不超過 25%（接近高點）
✅ 8. 股價比 52 週低點高出至少 30%（顯示上升動能）
```

---

## 🎯 評分系統

### 評分權重

| 維度 | 權重 | 說明 |
|------|------|------|
| VCP 收縮品質 | **30%** | 收縮次數、幅度遞減、量能遞減 |
| MM 趨勢模板 | **25%** | 8 項均線排列條件 |
| Stage 分析 | **15%** | 必須為 Stage 2（上升段） |
| 量能模式 | **15%** | 基部量縮 + 突破量增 |
| RS 相對強度 | **10%** | 相對 S&P 500 的表現 |
| 緊縮度 | **5%** | 手柄/最後整理的緊縮程度 |

### 評分等級

| 等級 | 分數 | 意義 |
|------|------|------|
| **A+** | 85-100 | 教科書級 VCP，優先關注 |
| **A**  | 75-84  | 高品質 VCP，值得追蹤 |
| **B**  | 65-74  | 良好設置，需配合市場環境 |
| **C**  | 50-64  | 尚在成形，可加入觀察清單 |
| **D**  | <50    | 不符合條件，略過 |

---

## 🛠️ 為什麼常常掃不到股票？

常見原因：
1. **趨勢模板過嚴**：原本需 9/9 全過，盤整市很容易全軍覆沒。
2. **分數門檻過高**：若同時要求高分 + 完美模板，命中率會很低。
3. **收縮條件過嚴**：有些強勢股還在早期整理，收縮次數未達理想值。

新版會在 log 顯示淘汰統計（趨勢不符、收縮不足、分數不足），幫你快速找到瓶頸。

---

## 🚀 快速部署

### Step 1：Fork 或 Clone 本倉庫

```bash
git clone https://github.com/YOUR_USERNAME/vcp-scanner.git
cd vcp-scanner
```

### Step 2：設定 GitHub Secrets

前往 `Settings → Secrets and variables → Actions → New repository secret`

| Secret 名稱 | 說明 | 範例 |
|-------------|------|------|
| `EMAIL_SENDER` | 寄件 Gmail 帳號 | `your.scanner@gmail.com` |
| `EMAIL_PASSWORD` | Gmail App Password（非帳號密碼）| `xxxx xxxx xxxx xxxx` |
| `EMAIL_RECIPIENT` | 收件信箱 | `your@email.com` |

> ⚠️ **Gmail App Password 設定方式**：
> Google 帳號 → 安全性 → 兩步驟驗證（需開啟）→ 應用程式密碼 → 產生

### Step 3：啟用 GitHub Actions

確認 `.github/workflows/vcp_scan.yml` 存在，Actions 會自動在以下時間執行：
- **平日（週一至週五）UTC 22:00**（台灣時間 隔天 06:00）
- 或手動觸發（Actions → Run workflow）

### Step 4：本地測試（選擇性）

```bash
pip install -r requirements.txt

export EMAIL_RECIPIENT="you@email.com"
export EMAIL_SENDER="sender@gmail.com"
export EMAIL_PASSWORD="your-app-password"

python vcp_scanner.py
```

---

## 📧 Email 報告範例

收到的 Email 包含：
- 每隻股票的 **評分（0-100）與等級（A+~D）**
- 現價、**入場位 / 止賺位 / 停損位**（技術面交易計畫）
- Stage 階段、收縮次數、RS 評分
- MM 趨勢模板 8 項通過狀況（✅/❌）
- 分析師建議（`BUY` / `WATCH`）、RSI、風險報酬比（R 倍數）

---

## ⚙️ 自訂設定

### 調整篩選標準（在 workflow_dispatch 或環境變數設定）

```yaml
MIN_SCORE: "50"          # 總分門檻（建議 45~60）
TREND_MIN_PASSED: "8"    # MM 趨勢模板最少通過幾項（0~9，建議 7~8）
MIN_CONTRACTIONS: "1"    # 最少收縮次數（建議 1，若行情弱可調成 0）
ONLY_BUY_RECOMMENDATION: "true"  # 只輸出分析師判定可買（BUY）的股票
```

> 若你常常「完全沒有股票」，先把 `MIN_SCORE` 降到 45，或把 `TREND_MIN_PASSED` 由 8 降到 7，通常就能看到候選名單。

### 修改掃描股票池

在 `vcp_scanner.py` 的 `get_sp500_tickers()` 函數中調整。

---

## 📁 輸出檔案

| 檔案 | 說明 |
|------|------|
| `vcp_results.json` | 當日掃描結果（JSON 格式） |
| `vcp_scan.log` | 執行日誌 |
| `results/vcp_YYYY-MM-DD.json` | 歷史結果存檔 |

---

## ⚠️ 免責聲明

本程式僅供學習研究用途，**不構成任何投資建議**。股市投資有賺有賠，請自行評估風險，任何交易決策請搭配個人判斷與完整研究。

---

## 📚 參考資料

- Mark Minervini, *Trade Like a Stock Market Wizard* (2013)
- Mark Minervini, *Think & Trade Like a Champion* (2017)
- Stan Weinstein, *Secrets for Profiting in Bull and Bear Markets* (1988)
