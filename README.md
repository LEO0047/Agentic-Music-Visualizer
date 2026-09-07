# Agentic Music Visualizer

TouchDesigner 負責目標 60 fps 的即時視覺，Python sidecar 加上透過 Codex OAuth 呼叫的 GPT-6 Astra 當導演，每 15–20 秒下一次創意決策；兩層之間只靠 OSC 傳 JSON。反射層直接把 bass / mid / high / energy / kick 綁到幾何、shader 與 feedback，導演掛掉也不會黑畫面；導演層只改「目標值」，TD 用 Lag CHOP 花 2–4 個 beat 滑過去，整數與字串類參數只在下一個 kick 切換。導演走的是本機既有的 `codex exec --output-schema`（ChatGPT 登入額度，不需要 API key），完整規格見 [SPEC.md](SPEC.md)。

## 在這台 Mac 直接播放

開啟根目錄的 **Start Visualizer.command**：它會建立 AirPods／目前聆聽裝置 + BlackHole
雙輸出、以可關閉的 960 × 540 一般視窗開啟已建好的 `Agentic-Music-Visualizer.toe`，並啟動 GPT 導演（失敗自動用規則）。
Apple Music 的輸出選「這台 Mac」，音量用 Apple Music 自己的滑桿調整。
點輸出視窗左上角 × 關閉畫面。在啟動終端機按 Ctrl-C 會停止導演並還原音訊；也可開 **Restore AirPods.command**。
需要先完成 TouchDesigner 免費授權與 Codex 登入。請將 `.toe` 和 `td/` 留在同一個 repo。

2026-09-08 已在 TD 2025.33230 實測音樂輸入、五個原生 GLSL 場景與 GPT → OSC → TD 控制。
這是可播放 MVP；錄影時實測約 47 fps，projectM、實體 MIDI 與 60 分鐘演出仍待驗收。
詳見 [實機驗收紀錄](docs/runtime-verification-2026-09-08.md)。

![TouchDesigner 實際輸出](docs/assets/td-live-preview.png)

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
amv/features.py                      # FeatureBuffer：1/30/120 s 平均、斜率、趨勢、summary()
amv/sections.py                      # SectionDetector：steady/build/drop/breakdown（SPEC §4）
amv/osc_io.py                        # FeatureReceiver（/feat/* 進）、TDClient（/director/* 出）
amv/sidecar.py                       # python -m amv.sidecar：Phase 3 接收、判段、記 log、回送 /feat/section
tools/fake_td.py                     # 沒有 TD 時的合成特徵源（145 BPM 劇本，--speed 加速）
amv/director.py                      # RuleDirector、GPTDirector(fallback)、enforce_variety、DirectorLoop、Hotkeys
tools/fake_codex.py                  # 假 codex 二進位：AMV_FAKE_CODEX_MODE=ok|fail|hang|garbage，無額度測容錯
tools/projectm_check.py              # Phase 5：projectM / OBS / Syphon / NDI / BlackHole 安裝狀態與安裝提示
tools/preflight.py                   # Phase 6：SPEC §8 上場前檢查（環境、loopback、路由、codex、fallback 演練、磁碟、sessions）
tools/dry_run.py                     # Phase 6：N 分鐘 dry run，輸出決策/延遲/節奏/反重複/額度預估 Markdown 報表
tools/codex_sessions.py              # Phase 6：只列/歸檔/清理本專案產生的 ~/.codex/sessions 檔
td/build_network.py                  # 在 TD Textport 執行，一鍵建出 /project1/amv 反射層網路
td/parspec.py                        # schema → TD Custom Parameters（純 Python）
td/osc_in_callbacks.py               # OSC In DAT callbacks：/director/* 路由、30 s 凍結、離散參數待 kick
td/drop_executor.py                  # on_kick 執行 on_drop、套用待決離散值、45 s heartbeat 看門狗
td/td_stub.py                        # 沒有 TD 時用來跑測試的最小 td 介面
td/midi_override.py                  # Phase 6：MIDI CC → 參數（人手寫入，觸發 30 s 凍結）、note → Mode
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

> **2026-09-08：可播放 MVP 已在實機驗證。** 下列狀態分開記錄實測與未完成項目。
> 詳見 [實機驗收紀錄](docs/runtime-verification-2026-09-08.md)。

| 階段 | 實際結果 | 仍待驗收 |
|---|---|---|
| 環境／導演 | 真實 Codex JSON、GPT → OSC → TD 參數成功 | 長時間延遲與額度 |
| 音訊路由 | AirPods 與 BlackHole 雙輸出、TD 特徵有變化 | Bluetooth 延遲校準 |
| 原生視覺 | 五個 GLSL 場景、一般視窗輸出；20 秒平均 59.08 fps | 穩定 60 fps、長時間測試 |
| 特徵／段落 | 實際 OSC 接收及段落變化 | 真曲鼓點與段落人工比對 |
| 錄影 | Non-Commercial 的 MJPEG 錄影可用；錄製時約 47 fps | 長時間錄影壓力 |
| projectM／MIDI | 介面與程式碼保留，預設關閉 | 外部來源與實體控制器 |

## 安全

Repo 內不放任何金鑰。導演層的認證完全交給既有的 `~/.codex/auth.json`（ChatGPT 登入模式）；本專案的程式只檢查該檔是否存在、只讀 `auth_mode` 這個鍵，不會讀取、列印或複製 token。
