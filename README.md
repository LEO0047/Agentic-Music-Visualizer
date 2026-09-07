# Agentic Music Visualizer

TouchDesigner 負責 60 fps 的反射神經，Python sidecar 加上透過 Codex OAuth 呼叫的 GPT-6 Astra 當導演，每 15–20 秒下一次創意決策；兩層之間只靠 OSC 傳 JSON。反射層直接把 bass / mid / high / energy / kick 綁到幾何、shader 與 feedback，導演掛掉也不會黑畫面；導演層只改「目標值」，TD 用 Lag CHOP 花 2–4 個 beat 滑過去，整數與字串類參數只在下一個 kick 切換。導演走的是本機既有的 `codex exec --output-schema`（ChatGPT 登入額度，不需要 API key），完整規格見 [SPEC.md](SPEC.md)。

## Repo 結構

```
SPEC.md                  V1 工程規格（主文件）
VISION.md                V2–V3 願景 / roadmap
docs/index.html          GitHub Pages 版藍圖
director_schema.json     導演決策 JSON Schema（給 codex exec --output-schema 用）
amv/
  schema.py              schema 載入、列舉常數、validate_and_clamp
  codex_client.py        find_codex() 與 CodexClient.decide()
  smoke.py               python -m amv.smoke，跑一次真實決策
scripts/smoke_codex.sh   smoke 的 shell 包裝
tools/check_env.py       環境檢查表（唯讀，永遠 exit 0）
tests/                   pytest（codex 全部用 mock，不燒額度）
```

## Quickstart

需要 Python ≥ 3.11 與 [uv](https://docs.astral.sh/uv/)。系統內建的 python3 是 3.9，不要用。

```bash
uv sync --group dev --extra audio    # 安裝依賴（見下方 aubio 註記）
uv run python tools/check_env.py     # 環境檢查表
uv run pytest -q                     # 單元測試，不會呼叫真的 codex
scripts/smoke_codex.sh               # Phase 0 驗收：跑一次真實 codex exec
tools/audio_check.py                 # Phase 1 驗收：devices / loopback / meter
amv/audio_features.py                # 純 numpy 頻帶能量、Normalizer、KickDetector
```

`scripts/smoke_codex.sh` 會消耗 ChatGPT 訂閱的 Codex 額度（一次約 25k tokens），不要拿來輪詢。

`aubio`（`--extra bpm`）目前無法建置：0.4.9 的 C extension 對 numpy 2.x 的 `npy_intp` 型別不相容，`--all-extras` 會整包失敗，所以刻意不放進預設安裝。它仍保留為 optional extra，BPM 偵測留到 Phase 3 再解（釘 numpy 1.x、等 aubio 新版，或換一套 beat tracking）。`--extra audio` 只裝 `sounddevice`，這是與 `--all-extras` 唯一的差別。

`uv sync` 目前解到 Python 3.11（`requires-python = ">=3.11"`，符合 SPEC）；要換直譯器用 `uv python pin`。

環境變數：`AMV_CODEX_BIN` 指定 codex 執行檔路徑，`AMV_SCHEMA` 指定 schema 檔路徑；兩者都不必設，預設會自己找。

## 相關文件

- [SPEC.md](SPEC.md) — V1 工程規格：資料契約、段落判定、階段驗收
- [VISION.md](VISION.md) — V2–V3 架構願景
- [GitHub Pages 藍圖](https://leo0047.github.io/Agentic-Music-Visualizer/) — 可視化版本

## 階段進度

| # | 階段 | 驗收 | 狀態 |
|---|---|---|---|
| 0 | 環境準備 | `codex exec` 回傳合規 JSON | ✅ |
| 1 | 音訊路由 | 喇叭有聲、TD CHOP 波形在動 | 🟡 sidecar 側完成：`tools/audio_check.py loopback` PASS；多重輸出裝置與 TD 端待使用者操作，見 [docs/phase1-audio-routing.md](docs/phase1-audio-routing.md) |
| 2 | 反射層 | 不開 Director 也能 60 fps 反應 | ⬜ |
| 3 | 特徵匯流 | build/drop/breakdown 時間戳與耳朵一致 | ⬜ |
| 4 | 導演層 | 連跑 60 分鐘；拔網路 30 s 內 fallback | ⬜ |
| 5 | projectM 側鏈 | projectm_mix 0→1 fps 不掉 | ⬜ |
| 6 | 演出強化 | 反重複、on_drop、錄影、MIDI 覆寫 | ⬜ |

## 安全

Repo 內不放任何金鑰。導演層的認證完全交給既有的 `~/.codex/auth.json`（ChatGPT 登入模式）；本專案的程式只檢查該檔是否存在、只讀 `auth_mode` 這個鍵，不會讀取、列印或複製 token。
