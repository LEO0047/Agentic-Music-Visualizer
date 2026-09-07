# Agentic Music Visualizer — V1 工程規格

> 主規格。V2–V3 願景見 [VISION.md](VISION.md)。可視化版本：[docs/index.html](docs/index.html)（GitHub Pages）。
> 日期：2026-09-08。平台：macOS Apple Silicon。

## 0. 一句話

TouchDesigner 負責 60 fps 的反射神經；Python sidecar + GPT-6 Astra（透過 Codex OAuth，不用 API key）當導演，每 15–20 秒下一次創意決策；兩層之間只靠 OSC 傳 JSON。

## 1. 兩層，兩種時間尺度

| 層 | 執行者 | 週期 | 職責 |
|---|---|---|---|
| 反射層 | TouchDesigner | 每 frame（60 fps） | Audio Device In → 頻譜 → bass/mid/high/energy/kick，直接調變幾何、shader、feedback。GPT 掛了也不黑畫面。 |
| 導演層 | Python sidecar + `codex exec -m gpt-6-astra` | 每 15–20 s | 看 30 s / 120 s 趨勢、段落（build/drop/breakdown）、最近 8 筆視覺歷史，輸出參數 JSON 含 `on_drop`。 |

導演改的是「目標值」，TD 用 Lag CHOP 花 2–4 個 beat 滑過去。整數與字串類參數只在下一個 kick 切換。

**每一 frame 問 GPT 是死路**：60 fps = 每小時 216,000 次呼叫。

## 2. 導演層為什麼走 Codex OAuth

- 本機已有 `codex-cli 0.153.4`（`~/.codex/plugins/.plugin-appserver/codex`），`~/.codex/auth.json` 為 chatgpt 登入模式，`config.toml` 預設 model `gpt-6-astra`。
- `codex exec --output-schema <file> -o <out>` 直接給 structured output。用量計入 ChatGPT 訂閱的 Codex 額度。
- **2026-09-08 實測**：一次決策端到端 13.2 s（含冷啟動，effort low），25,192 tokens（大多是 Codex 系統提示），輸出合規 JSON，exit 0。

| 路線 | 決定 |
|---|---|
| A. 每次決策開一個 `codex exec` | **V1 採用**。已實測。決策週期 15–20 s，drop 反應交給反射層。 |
| B. 常駐 `codex app-server` / `mcp-server` | 第二階段。省冷啟動，但 experimental。 |
| C. 直接拿 access_token 打 chatgpt.com 後端 | **不採用**。非官方端點、會被擋、偏離登入使用範圍。 |

### stdin 陷阱
`codex exec` 在非 TTY 下會等 stdin EOF。Python 必須 `stdin=subprocess.DEVNULL`，shell 加 `< /dev/null`。

### 額度
每次 ~25k tokens；每小時 180 次 ≈ 4.5M tokens，會撞 Codex 5 小時 / 每週用量窗。正式演出前 dry run 60 分鐘看額度；規則式 fallback 必須常駐。

## 3. 資料契約

### 3.1 TD → Director：特徵（OSC，10 Hz，UDP 127.0.0.1:9000）

| Address | 型別 | 範圍 | TD 端怎麼算 |
|---|---|---|---|
| `/feat/bass` | float | 0–1 | Audio Spectrum 20–150 Hz 平均 → Lag 0.05 s → 正規化 |
| `/feat/mid` | float | 0–1 | 150–2 kHz |
| `/feat/high` | float | 0–1 | 2–16 kHz |
| `/feat/energy` | float | 0–1 | Analyze RMS，滑動 1 s，除以 set 最大值自適應 |
| `/feat/kick` | int | 0/1 | bass Slope 超門檻 → Logic，脈衝 1 frame |
| `/feat/centroid` | float | Hz | Script CHOP numpy 頻譜重心（選配） |

sidecar 判定段落後回送 `/feat/section`（string：`build` / `drop` / `breakdown` / `steady`）。

### 3.2 Director 決策 JSON（`director_schema.json`）

```json
{
  "scene":         "fractal_temple | tunnel | particle_field | kaleido_mesh | projectm_blend",
  "palette":       "violet_cyan | acid_lime | amber_dusk | mono_white | infrared",
  "feedback":      0.00,
  "symmetry":      1,
  "camera_speed":  0.0,
  "particle_mode": "spiral | burst | rain | orbit | none",
  "projectm_mix":  0.0,
  "transition":    { "mode": "cut | glide | on_next_kick", "beats": 1 },
  "on_drop":       { "scene": "...", "palette": "...", "particle_mode": "..." },
  "intent":        "≤120 字"
}
```
範圍：feedback 0–0.98、symmetry 1–16、camera_speed 0–1、projectm_mix 0–1、transition.beats 1–16。

`on_drop` 是關鍵欄位：GPT 提前決定下一個 drop 要切什麼，TD 反射層在偵測到 drop 的那一 frame 直接執行。

### 3.3 Director → TD：OSC（UDP 127.0.0.1:9001）

| Address | 型別 | TD 端接法 |
|---|---|---|
| `/director/scene` | string | Switch TOP index，依 transition.mode 決定 cut 或等下一個 kick |
| `/director/palette` | string | Lookup TOP ramp 切換，顏色用 Cross TOP 滑 |
| `/director/feedback` | float | Feedback TOP 混合量 → Lag，lag 秒 = beats × 60 / BPM |
| `/director/symmetry` | int | GLSL uniform，整數不滑，下一個 kick 才換 |
| `/director/camera_speed` | float | Camera COMP rotate 速度 → Lag |
| `/director/particle_mode` | string | Particle GPU force preset |
| `/director/projectm_mix` | float | Composite TOP 對 projectM 圖層 opacity → Lag |
| `/director/on_drop` | json string | 存 Text DAT，drop 命中時讀出執行 |
| `/director/heartbeat` | int | 每次決策遞增；TD 45 s 沒收到就警示並維持現狀 |

## 4. 段落判定（sidecar）

- `breakdown`：energy < 0.35 持續 > 4 s
- `build`：energy 連續上升 20 s
- `drop`：breakdown 之後 bass 突然回到 ≥ 0.7
- 其他：`steady`

## 5. 三種模式

`gpt` / `rule` / `manual`，熱鍵切換。任何參數被手動一動，該欄位凍結 30 s。GPT 失敗或逾時 30 s → rule 接管，畫面不得閃。

## 6. 執行階段與驗收

| # | 階段 | 估時 | 驗收 |
|---|---|---|---|
| 0 | 環境準備 | 半天 | `codex exec -m gpt-6-astra --output-schema schema.json -o out.json "..." < /dev/null` 回傳合規 JSON |
| 1 | 音訊路由 | 1 h | 喇叭有聲、TD CHOP 波形在動、Trail 看到 kick 尖峰 |
| 2 | 反射層 | 1–2 天 | 不開 Director，手動拖 Custom Parameter，60 fps 反應且參數會滑 |
| 3 | 特徵匯流 | 半天 | 播完整曲，log 的 build/drop/breakdown 時間戳與耳朵一致 |
| 4 | 導演層（先規則再 GPT） | 1 天 | 連跑 60 分鐘不中斷；拔網路 30 s 內 fallback 接手且不閃 |
| 5 | projectM 側鏈 | 半天–1 天 | projectm_mix 0→1 全程 fps 不掉 |
| 6 | 演出強化 | 持續 | 反重複、on_drop、錄影、MIDI 覆寫、app-server 評估 |

### Phase 0 細節
- `brew install blackhole-2ch`（本機已裝）
- TouchDesigner 2023+（Non-Commercial 輸出上限 1280×1280）
- Python 3.11+ venv：`python-osc numpy sounddevice`，選配 `aubio`
- 選配 `brew install projectm`

### Phase 1 細節
Audio MIDI Setup → 「+」→ 多重輸出裝置：勾喇叭 + BlackHole 2ch；主裝置設喇叭，BlackHole 勾 Drift Correction；設為系統輸出。TD Audio Device In 選 BlackHole 2ch，48 kHz。

### Phase 2 細節
- Audio Spectrum → 三段 Select → Analyze → Lag → Math 正規化；bass 走 Slope + Logic 做 kick
- `/director` Base COMP 上每個 schema 欄位一個 Custom Parameter，視覺網路只引用這裡
- 至少三個場景（tunnel、fractal_temple、particle_field）掛 Switch TOP
- Feedback、Kaleido、Lookup 色盤做共用後製鏈
- OSC In DAT 監聽 9001，callback 只把值寫進 Custom Parameter

### Phase 5 細節
projectM 獨立跑，音源 BlackHole；畫面進 TD 走 Syphon（Syphoner，≈1 frame）或 OBS + NDI（2–4 frames，最穩）。導演只控 `projectm_mix`。libprojectM 內嵌 Custom TOP **不做**（macOS TD 走 Metal，libprojectM 要 OpenGL）。

## 7. 延遲與風險

| 路徑 | 延遲 |
|---|---|
| BlackHole → TD CHOP | ≈ 1 buffer |
| TD 特徵 → 視覺 | 1 frame |
| OSC localhost | < 1 ms |
| codex exec 一次決策 | 13.2 s（實測） |
| projectM → Syphon → TD | ≈ 1 frame |
| projectM → OBS → NDI → TD | 2–4 frames |

| 風險 | 對策 |
|---|---|
| Codex 額度演出中用完 | rule fallback 常駐；dry run；週期可即時拉到 30 s |
| codex exec 掛住 | stdin=DEVNULL、timeout 30 s、每輪獨立程序 |
| 決策風格單調 | 歷史帶時長進 prompt；60 s 內不重複；每 10 次強制換 scene |
| 參數跳變閃畫面 | float 全走 Lag；整數/字串只在下一個 kick 切 |
| `~/.codex/sessions` 堆滿 | 演出後 `codex archive` 或定期清 |
| Codex 版本改 flag | 啟動先 smoke test，失敗直接進 rule 模式並警示 |

## 8. 上場前檢查
- [ ] 多重輸出裝置為系統輸出，喇叭有聲，TD 波形在動
- [ ] TD 不開 Director 能獨立演完一首歌
- [ ] `codex exec` smoke test 通過，額度有空間
- [ ] 拔網路：30 s 內 fallback 接管，無閃爍
- [ ] 熱鍵切 gpt / rule / manual
- [ ] on_drop 有填且 drop 命中有反應
- [ ] Movie File Out 錄影與 Director log 同時啟動
- [ ] projectM 若啟用，projectm_mix 0→1 fps 穩定
