# Linux 畫面預覽

**Linux 已能實際渲染五種動態場景、跟著音樂反應，並輸出有音軌的影片。**
這一版用既有 GLSL 圖形程式，加上獨立的 Linux 控制器；使用者提供的 PCM WAV 或內建測試音樂都能用。

## 先看結果

- 可播放的兩分鐘示範：`ai-vj-linux-preview-120s.mp4`，640 × 360、30fps、有字幕與合成音軌
- 已逐幀解碼 3,600 幀、檢查音訊，並抽樣看過場景、轉場與手動控制
- 新增音樂輸入→特徵分析→實際 UDP→真實 GLSL 的比對測試；畫面確實隨音訊變化
- 修正後完整測試 **970 項通過**，沒有跳過；220 秒 AI 到實際畫面整合的 42 項檢查全過
- 修正版本連續一小時真正渲染通過全部 18 項檢查：107,805 張畫面，包含 6,849 張粒子關閉備援畫面
- 實測細節與一小時渲染結果：[Linux 畫面驗證](docs/linux-visual-verification-2026-10-06.md)

影片的 30fps 是播放速度。這台雲端只有 CPU 軟體繪圖：640 × 360 使用離線輸出；
即時壓測採用 320 × 180、目標 30fps。

## 在 Linux 重跑

需要 Python 3.11+、uv、系統的 EGL／OpenGL，以及含 H.264／AAC 的 FFmpeg。
以下命令不會登入帳號、不會用到真實 AI 額度，也不會碰音訊裝置。

```sh
uv sync --locked
uv run --no-sync python tools/linux_visualizer.py \
  --seconds 120 --exercise-controls --output artifacts/my-linux-demo
```

請使用新的輸出資料夾，避免覆蓋之前的報告或影片。
輸出資料夾會有乾淨影片、附說明字幕的影片、原始 WAV、截圖與 JSON 測試紀錄。
預設是程式合成的 145 BPM 測試音樂，沒有使用現成歌曲。

使用自己的 PCM WAV：

```sh
uv run --no-sync python tools/linux_visualizer.py \
  --wav /absolute/path/music.wav --director rule --output artifacts/my-track
```

持續即時渲染，並留下截圖與效能紀錄：

```sh
uv run --no-sync python tools/linux_visualizer.py --realtime \
  --seconds 3600 --width 320 --height 180 --fps 30 \
  --exercise-controls --snapshot-seconds 60 --status-seconds 10 \
  --output artifacts/my-live-render
```

這個即時模式是無視窗渲染：不會自行開全螢幕，也不會播放到喇叭。
按 Ctrl-C 可停止。不要用中斷的紀錄當成完整一小時通過。

## 控制方式

- `--director showcase`：依序展示五場景，方便驗收
- `--director rule`：由既有規則導演根據音樂選畫面
- `--director external --osc-port 9001`：接收外部導演的 OSC 控制；一次決策須依既有順序完整送出，最後送 heartbeat
- `/manual/<欄位>`：立即手動修改，該欄位保留 30 秒
- `/manual/mode`：切換 `gpt`、`rule`、`manual` 控制模式；這個指令本身不會啟動 AI

控制器支援平滑交疊、下一次偵測到 kick 才切換、沒有 kick 時的備援，以及預先準備的 drop 變化。
`projectm_blend` 使用內建圖形備援，外部 projectM 來源尚未接入這個 Linux 預覽器。

`particle_field` 同時關閉粒子時，原始 shader 只剩固定背景。Linux 合成器會明確改用既有
`kaleido_mesh` 的不含粒子動畫，保留控制值與「粒子關閉」；字幕、截圖及報告會標出實際繪製場景。
這個保護只實作在 Linux 合成器；原生 TouchDesigner 路徑尚未套用。

跑完整測試，包含 220 秒的模擬 AI 故障／恢復／手動接管及真正像素檢查：

```sh
AMV_TEST_EGL=1 AMV_TEST_AI_EGL=1 uv run --no-sync pytest -q
```

這個 AI 測試固定使用本 repo 的模擬子程序，不會登入帳號或呼叫真實模型。

## 已知邊界

開始前會用來源前兩秒做音量基準校準；之後每 100ms 分析一次，再平滑送進畫面。
鼓點偵測是啟發式，因此音訊／影片檔長度對齊不代表每個鼓點都毫秒級貼齊。

TouchDesigner、Metal、實體 MIDI、AirPods、外部 projectM，以及舞台用 1280 × 720／60fps，
仍需要各自驗收。此預覽器的控制與後製是 Linux 實作，沒有冒充 TD 引擎。
