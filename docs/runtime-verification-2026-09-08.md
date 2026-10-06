# 實機驗收 · 2026-09-08

已在 macOS 的 TouchDesigner 2025.33230 Non-Commercial 內建構並渲染。
這是可播放的原生視覺 MVP，仍不是完成 60 分鐘演出驗收的版本。

## 已確認

- Apple Music 選這台 Mac → AMV 多重輸出 → AirPods Pro 2 + BlackHole 2ch。
  使用者確認 AirPods 有聲音；BlackHole 48 kHz 連續 40 個區塊無靜音、無 overflow，
  RMS 0.08672–0.137328。TD 同時收到 bass / mid / high / energy 變化。
- 裝置使用 `BlackHole2ch_UID` 並啟用 missing-device error，避免無效選項偷偷落回麥克風。
- 修正頻谱線性頻率、Trim sample 單位與 timeslice、RMS 選項、立體聲合併。
- 5 個 GLSL 場景實際渲染，1280 × 720。包含隧道、徑向幾何、程式粒子及混合變化。
  粒子是 shader 內計算的位置，不是物理模擬；`projectm_blend` 在沒有外部來源时使用原生混合圖形。
- 五個場景及色盤截圖已目視檢查；無缺圖、shader 編譯錯誤或 unintended black output。
- 修正 feedback 缺少輸入、Transform 參數、Composite 前後景順序；projectM 未啟用時不再蓋黑底。
- MIDI 預設停用且 Device Table 留空，不再引用不存在的 `1`。
- OSC Out DAT 用牆鐘節流且保留短 kick pulse。靜態執行實測 10.008 Hz；
  同步截圖與切場時約 8.815 Hz，截圖 readback 會卡主執行緒，不能當作固定 10 Hz 保證。
- 65 秒真實 GPT sidecar 測試：4,080 個特徵訊息，3 次發布，1 次 GPT 成功（11.363 秒），
  2 次推理期間音樂換段而改走當前規則。無 sidecar 錯誤。TD 實際參數及 heartbeat 已改變。
- 手動模式、30 秒欄位保留、失效決策抑制由既有測試保護；638 個測試通過。
- 不錄影、不截圖的 20 秒取樣：平均 59.08 fps，p95 幀間隔 18.76 ms，最大 121.90 ms。
- 五場景切換加同步截圖：15 秒平均 53.51 fps，最長一幀 431 ms。
  MJPEG 錄影：10 秒平均 46.88 fps，沒有節點或腳本錯誤。這些不是穩定 60 fps 證據。
- 免費版不支援 GPU H.264/H.265 編碼；改用原生 MJPEG，另轉出小型 MP4 預覽。
  預覽無音軌；轉檔調整時間軸以對應錄製牆鐘時間。

本機證據在 `artifacts/td-runtime/`，不進公開 Git：包含實際 TOP 圖像、特徵值、
OSC 頻率、導演決策與錄影。`verify_runtime.capture()` 仍明確標成需人工檢查，
不會因為圖片存在就自動給 PASS。

## 本次實際修正

原始建構碼的 stub 測試無法發現 TD menu token、cook 時序與缺少 input 的問題。
本次按引擎實際值修正這些問題，並把三個 placeholder noise 場景換成五個 GLSL 場景。
顏色直接在 shader 內計算，執行網路不再建立用不到的 Ramp bank。

CoreAudio 的 `stacked=0` 在本機產生了 2-in/4-out 聚集裝置，並非鏡像輸出。
本機驗證 `stacked=1` 才產生 0-in/2-out 的多重輸出；不能只依 header 註解判斷路由已成功。

## 使用限制與仍待驗收

- 多重輸出沒有 macOS 主音量控制。使用 Apple Music 自己的音量滑桿，或在 Audio MIDI Setup
  調整 AirPods 個別裝置；停止後以 `Restore AirPods.command` 還原系統輸出。
- Bluetooth 聽覺延遲與畫面同步尚未做時間校準；目前未補償 AirPods 延遲。
- kick 是低頻上升沿啟發式，不是準確 BPM／鼓點辨識；真實音樂可能過度觸發，需要校準。
- centroid 仍為保留通道 0，不能稱為已計算頻譜重心。
- projectM／Syphon／NDI 外部來源、實體 MIDI、長時間穩定性與 60 分鐘演出未驗收。
- GPU 粒子與圖形已可見，但不宣稱達成 VISION 的 3D 物理模擬或 V2/V3 全部願景。

## 全螢幕回退

使用者要求全螢幕後曾將 Window COMP 設為 fill 並關閉 borders；這在本機遮住其他操作，
且 Escape 沒有成功恢復。已透過 TD 原生 Quit 選單結束程序，保留已存檔與音訊路由。
交付檔已回退為 960 × 540、有標題列及關閉按鈕的一般視窗，不自動進全螢幕。
因此全螢幕控制尚未驗收通過，不能把設定值寫入成功當作操作成功。
