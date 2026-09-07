# Phase 1 · 音訊路由（Apple Music → BlackHole → TD / sidecar）

目標：Apple Music 的聲音同時進喇叭與 BlackHole 2ch，TouchDesigner 與 Python sidecar 都能從 BlackHole 讀到 PCM。

## 本機現況（2026-09-08 實測）

| 項目 | 狀態 |
|---|---|
| BlackHole 2ch 驅動 | 已安裝，CoreAudio 裝置 index 1，2 in / 2 out，48 kHz |
| 系統預設輸出 | MacBook Pro 揚聲器（尚未建立多重輸出裝置） |
| sounddevice 讀 BlackHole | 可以；沒播歌時 1 s RMS = 0.0（正常） |

## 一次性設定（GUI，只有你能做）

1. 開 **Audio MIDI Setup**（音訊 MIDI 設定）。
2. 左下「+」→ **建立多重輸出裝置**。
3. 右側勾選：你的喇叭或音訊介面 **和** BlackHole 2ch。
4. 「主裝置」選喇叭；BlackHole 那一列勾 **漂移校正（Drift Correction）**。
5. 右鍵這個多重輸出裝置 → **用於聲音輸出**。或到系統設定 → 聲音 → 輸出選它。
6. Apple Music 播一首歌，喇叭要有聲。

改名建議：把多重輸出裝置命名為 `Speakers+BlackHole`，之後 check 工具會直接找這個名字。

## 驗收

### 不需要 TD 的自動驗收（sidecar 端）

```bash
uv run python tools/audio_check.py loopback   # 對 BlackHole 輸出播測試音，從 BlackHole 輸入讀回，驗驅動與取樣率
uv run python tools/audio_check.py meter      # 邊播 Apple Music 邊看 RMS / bass / kick 即時表
```

- `loopback`：不需要任何 GUI 設定，只驗 BlackHole 驅動本身能通。回讀 RMS 必須 > 0.05，頻率誤差 < 2 Hz。
- `meter`：需要多重輸出裝置已設好。連續 10 秒 RMS > 0.01 即通過；表上要看得到 kick 脈衝。

### TD 端（需要 TouchDesigner，本機目前未安裝）

- Audio Device In CHOP，Device 選 `BlackHole 2ch`，Sample Rate 48000。
- 後接 Trail CHOP，播 Psytrance 時要看到 kick 的尖峰。

## 已知陷阱

- 系統輸出切到多重輸出裝置後，**macOS 的音量鍵會失效**（多重輸出裝置不支援系統音量），要用 Apple Music 自己的音量或喇叭實體音量。
- 取樣率不一致會爆音：喇叭、BlackHole、多重輸出裝置三者都要 48 kHz。
- BlackHole 只在有程式讀取時才消耗資源；沒人讀時無害。
- 兩個程式同時讀 BlackHole（TD + sidecar）是被允許的，CoreAudio 輸入可多重客戶端。
