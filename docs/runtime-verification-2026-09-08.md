# 實機接續驗收 · 2026-09-08

目前不能宣稱「視覺機已完成」。原有 630 個測試在本次重跑全部通過，
但 TouchDesigner 網路仍未在引擎內執行，沒有可供檢視的真實輸出。

## 本次完成

- 透過 Homebrew 安裝官方 TouchDesigner 2025.33230 Apple Silicon 版。
- 啟動時的 Apple 已檢查下載 App 提示已確認開啟。
- 程序接著等待 macOS 管理員授權。SecurityAgent 密碼視窗不允許桌面工具操作，需使用者完成。
- 修正導演的過期結果：推理期間已換段，或超過一個決策週期，就以當前音樂摘要走規則導演。
- 修正推理中的手動接管／模式切換／結束：舊回覆不再發送。
- 歷史時間戳改為實際發布時間，避免把 10 多秒前的請求時間當作視覺開始時間。
- Codex 呼叫加入 `--ephemeral`，不用每次決策留下 session 再額外清理。
- 一次真實 gpt-6-astra／low 決策通過，合規 JSON，耗時 10.4 秒（單次資料，非延遲保證）。
- 修正後全套測試：638 passed，7.17 秒；`git diff --check` 通過。
- BlackHole 48 kHz 真實 loopback：輸入 440 Hz、讀回 440 Hz，誤差 0 Hz，RMS 0.177617。
  這證明驅動可收發，不代表 Apple Music 已接入或 TD 已跟拍。
- 核對 TD 安裝包內官方文件，修正 Trim CHOP 的 `startunit`／`endunit`／`relative=abs`，
  以及 Analyze CHOP 的 `function=rmspower`。這兩項尚待引擎實測。
- 提供 `td/verify_runtime.py` 保存真實 TOP 像素、節點錯誤、警告、特徵值；不會把快照自動判為 PASS。

## 授權完成後的實際操作

1. 完成 TD 登入／啟用，開新專案。
2. 在 Textport 執行：

   ```python
   exec(open('/Users/leohuang/Repos/Agentic-Music-Visualizer/td/build_network.py').read())
   ```

3. 先修正 Textport 與節點顯示的實際錯誤，確認 `out` 有畫面。不要只看建構腳本有沒有退出。
4. 在 Audio MIDI Setup 建立喇叭 + BlackHole 的多重輸出，確認真實音樂進入 TD。
5. 網路運行後保存證據：

   ```python
   import verify_runtime
   verify_runtime.capture(op('/project1/amv'))
   ```

   本機 `artifacts/td-runtime/<時間>/` 會包含 `output.png` 與 `report.json`。
   此目錄不進 Git；沒有捕獲成功就沒有可交付的效果截圖。

6. 檢查三個場景、五個色盤、參數滑動、音訊同步、實際 FPS，再接規則導演與 GPT。

## 尚未驗證或尚未完成

- TD 實際建構、畫面、音訊反應、長時間 FPS 與斷網演練。
- 多重輸出裝置的系統路由（BlackHole 已安裝，但未接到 Apple Music）。
- 真粒子、真正的多向對稱、兩個仍映射到其他場景的選項。
- OSC 實際發送頻率；Resample 的 sample rate 不能作為 cook rate 實測證據。
- projectM 側鏈、MIDI 實體控制器、錄影、60 分鐘演出驗收。

這些項目不能由 stub 測試、單次 GPT 成功、程序已啟動或建構腳本存在來代替。
