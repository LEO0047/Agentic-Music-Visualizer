# Phase 5 · projectM 側鏈（MilkDrop 圖層進 TouchDesigner）

目標：projectM 獨立跑、聽 BlackHole 的同一份音訊，畫面用 Syphon 或 NDI 送進 TD，
由 Composite TOP 疊在 TD 場景上，混合量只由導演的 `projectm_mix` 控制。

SPEC 對應：§3.3 `/director/projectm_mix`、§6 #5、§7 延遲表、Phase 5 細節、§8 上場前檢查最後一項。

## 本機現況（2026-09-08，`uv run python tools/projectm_check.py`）

| 項目 | 狀態 |
|---|---|
| projectM | missing（`brew install projectm`） |
| projectMSDL on PATH | missing |
| OBS.app | missing（`brew install --cask obs`） |
| Syphoner.app / 任何 Syphon app | missing（<https://syphon.github.io/>） |
| NDI runtime | missing（<https://ndi.video/tools/>） |
| BlackHole 2ch | **已安裝**（`/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver`） |

TouchDesigner 本機也沒有裝。所以這份文件是**設定手冊 + 驗收清單**，不是實測報告：
下面所有延遲數字都是 SPEC §7 的估計值，標「實測」的只有 BlackHole 那一項（Phase 1）。
`tools/projectm_check.py` 永遠 exit 0，它是儀表板不是關卡——什麼都沒裝也能建網路，
`Projectmsource` 留在 `none` 就是一張黑底，Composite 照樣良好定義。

## 為什麼 projectM 是「另一個程式」而不是 TD 裡的一顆 TOP

V1 刻意**不做** libprojectM 內嵌 Custom TOP，這是 SPEC Phase 5 細節與 VISION 都寫死的取捨：

> macOS 版 TouchDesigner 的算繪走 **Metal**，而 libprojectM 需要 **OpenGL context**。

要在 TD 的 Metal 管線裡開一個 OpenGL context 給 libprojectM、再把它的 framebuffer
零複製交回 Metal texture，是一整包 IOSurface / 跨 API 共享的工程，做不好就是每 frame
一次 GPU→CPU→GPU 來回，Phase 5 的驗收（「`projectm_mix` 0→1 全程 fps 不掉」）反而先死。
所以 V1 用**行程間的畫面傳輸**（Syphon 或 NDI）換掉這個問題，
把 libprojectM 深度整合留給 **V3**（VISION：「projectM 深度整合 + Performance Memory」）。

代價很誠實：多一個要顧的程式、多 1–4 frame 延遲、preset 不受導演控制（見最後一節）。

## 步驟 1 · 裝 projectM 並獨立跑起來

```bash
brew install projectm
uv run python tools/projectm_check.py    # 再跑一次，前兩列應該不是 missing
```

`projectM` formula 裝的是**函式庫**；要能單獨播放的是 SDL 前端 `projectMSDL`
（專案名 `frontend-sdl2`）。`projectm_check.py` 的第二列就是在找它：
formula 沒附的話要自己從 <https://github.com/projectM-visualizer/frontend-sdl2> 建。

先不接 TD，單獨確認三件事：

1. 視窗打得開、有 MilkDrop 圖案在動。
2. **音源是 BlackHole 2ch**（下一步）。
3. 播 Apple Music 時圖案會跟著鼓點跳，不是自轉。

### 音源設成 BlackHole

Phase 1 已經把系統輸出設成「喇叭 + BlackHole 2ch」的多重輸出裝置
（`docs/phase1-audio-routing.md`），所以 **projectM 和 TD 讀的是同一份 PCM**——
CoreAudio 輸入允許多個客戶端，兩邊同時讀 BlackHole 是合法的，也是這條側鏈能「對得上拍」
的原因：兩層聽的是同一秒的音樂，不是兩個各自跑的分析。

projectMSDL 選輸入裝置有兩條路（**兩者都待第一次實機確認**）：

- 設定檔（`projectMSDL.properties` 之類，可用 `--configurationFile` 指定）裡的
  audio device 欄位，填 `BlackHole 2ch`。
- 執行時用熱鍵循環切換輸入裝置，切到 BlackHole 為止。

確認方法不必看設定：**把音樂暫停，圖案應該立刻變得平順沒有節拍**；再播放，
低頻應該立刻把圖案撐開。若暫停了圖案照跳，它聽的是麥克風或別的裝置。

## 步驟 2 · 兩條擷取路徑，擇一（兩條都先建好了）

`td/build_network.py` 的 `build_projectm_input()` 會把兩顆擷取 TOP 與一張黑底
**並排**建出來，選哪一條是 TD 裡一個下拉選單，不是重建網路。

| 路徑 | 怎麼接 | 延遲（SPEC §7 估計） | 什麼時候用 |
|---|---|---|---|
| **A. Syphon** | projectM 視窗 → **Syphoner** 發佈成 Syphon server → TD `pm_syphon`（Syphon Spout In TOP） | **≈ 1 frame** | 預設。GPU 上的零複製共享，最快 |
| **B. OBS + NDI** | projectM 視窗 → **OBS** 視窗擷取 → NDI 輸出外掛 → TD `pm_ndi`（NDI In TOP） | **2–4 frames** | 最穩。projectM 在別的 Space、別的螢幕甚至別台機器都能送 |

### A. Syphon（Syphoner，≈ 1 frame）

1. 裝一個 Syphon server app。Syphoner 就是把「某個視窗／某塊螢幕」發佈成 Syphon
   來源的那種工具；其他 Syphon app 列在 <https://syphon.github.io/>。
2. 開 Syphoner，來源選 projectM 的視窗。
3. 記下它發佈的 server 名稱。`build_network.py` 的
   `PROJECTM_SYPHON_SENDER` 預設猜 `"projectM"`；名字對不上就改那個常數再重跑腳本，
   或直接在 TD 裡改 `pm_syphon` 的 Sender 參數。
4. TD 裡 `pm_syphon` 應該立刻出畫面。沒有的話先看 Textport 有沒有
   `[amv] SKIP pm_syphon`（＝這個 TD build 沒有 Syphon Spout In TOP 這個類別名）。

macOS 上這顆 TOP 只有 Syphon 模式（Spout 是 Windows 的），所以除了 server 名稱沒別的要設。

### B. OBS + NDI（2–4 frames，最穩）

1. `brew install --cask obs`
2. 裝 OBS 的 NDI 輸出外掛（`obs-ndi`，現名 **DistroAV**）與 **NDI runtime**
   （NDI Tools 內含：<https://ndi.video/tools/>）。`projectm_check.py` 的 NDI 那列
   找的就是 runtime，不是外掛。
3. OBS 開一個場景，加一個 **視窗擷取**，抓 projectM 的視窗。
4. 開 NDI 輸出（Tools → NDI Output Settings），把輸出名稱設成 `projectM`——
   要跟 `build_network.py` 的 `PROJECTM_NDI_SOURCE` 一致（或反過來改常數）。
5. TD 裡 `pm_ndi`（NDI In TOP）選這個來源。

多的 1–3 frame 就是 OBS 編碼 + NDI 走網路堆疊的成本。SPEC §7 把它列為「最穩」是因為
它不依賴視窗在不在前景、在哪個 Space，也允許 projectM 跑在另一台機器上分攤 GPU。

## 步驟 3 · TD 端：`Projectmsource` 選路，導演只碰 `projectm_mix`

```
pm_black   (Constant TOP, 黑, alpha 1, 1280²)  ─┐  index 0 = none
pm_syphon  (Syphon Spout In TOP, "projectM")   ─┼─→ projectm_in (Switch TOP)
pm_ndi     (NDI In TOP, "projectM")            ─┘  index 2 = ndi
                                                      │
                                                      ↓
                                          pm_fit (Fit TOP, fill, 1280²)
                                                      ↓
                                   projectm_level (Level TOP)
                                     opacity = op('lag_params')['projectm_mix']
                                                      ↓
              fb_mix ──────────────→ composite (Composite TOP, operand = over) ──→ out
```

- **`Projectmsource`**（`director` COMP 的 **Runtime** 頁，選單 `none` / `syphon` / `ndi`，
  預設 `none`）是 `projectm_in` 這顆 Switch TOP 的 index。
  它**沒有 OSC address**，是刻意的：SPEC §3.3 給導演的 projectM 控制只有一個，
  就是 `/director/projectm_mix`。「畫面從哪條線進來」是這台機器的接法，
  由設定它的人決定，不是 GPT 該決定的事。
- **`pm_black`** 是預設，也是這條側鏈不會炸的原因：什麼都沒裝、projectM 沒開、
  Syphoner 關掉了，`projectm_in` 仍然 cook 出一張合法的 1280² 黑底，
  Composite TOP 永遠良好定義。`projectm_mix` 這時把黑底疊上去只會壓暗畫面，
  所以**沒接來源時把 `Projectmmix` 留在 0**。
- **`pm_fit`**（Fit TOP，fill）把任何解析度的 projectM 畫面撐到 1280² 畫布。
  沒有它，一個 640×480 的 SDL 視窗會變成畫面角落的一小塊長方形。
- **`projectm_level`** 的 opacity 綁 `lag_params` 的 `projectm_mix`——
  就是 SPEC §3.3 那條 Lag（lag 秒 = `Transitionbeats` × 60 / `Bpm`），
  所以導演把 mix 從 0 拉到 1 是**滑**過去的，不是跳。
- **Composite 的 `operand` 是 `over`**：projectM 在上、TD 場景在下。

導演端不必改任何東西：`/director/projectm_mix` 這條路 Phase 2 就打通了
（`osc_in_callbacks` → `Projectmmix` → `dir_vals` → `lag_params`），
Phase 5 只是讓它終於有東西可以混。

## Preset 控制：導演不管 preset

**導演不控制 projectM 的 preset，V1 刻意如此。**

- SPEC §3.2 的決策 JSON 裡根本沒有 preset 欄位，§3.3 的 OSC 表也只有
  `/director/projectm_mix`。加一個 preset 欄位等於改資料契約。
- projectM 是獨立行程，V1 沒有對它的遙控通道（Syphon/NDI 都是**單向的畫面**）。
  要遙控就得再開一條 IPC，那是 V3「projectM 深度整合」的範圍。
- 更實際的理由：preset 名稱是使用者自己那包 `.milk` 檔案，
  GPT 沒看過也無從挑起——挑不動的東西不要放進 schema。

所以 preset 由 projectM 自己處理，兩種方式：

1. **自動輪播**：projectMSDL 設定檔裡的 preset duration / shuffle
   （每 N 秒換一張，隨機順序）。這是演出時的預設做法——導演用 `projectm_mix`
   決定「現在要不要看到 MilkDrop」，projectM 自己決定「看到哪一張」。
2. **熱鍵**：projectM 沿用 MilkDrop 的慣例綁定，常見的是
   `N` 下一張 / `P` 上一張 / `R` 隨機 / `Space` 鎖住目前這張（**待實機確認**）。
   想在某一段鎖住某張圖，就用鎖定鍵，這是唯一需要人手介入的地方。

配色打架的解法也在這裡：不是叫導演換 preset，而是**縮掉一批**——
把跟 TD 五個色盤（`violet_cyan` / `acid_lime` / `amber_dusk` / `mono_white` / `infrared`）
會吵架的 preset 從 preset 資料夾拿掉，讓輪播只抽得到合得來的那些。

## 驗收（SPEC §6 #5、§8）

> **驗收標準：`projectm_mix` 0→1 全程 fps 不掉，MilkDrop 圖層與 TD 場景在同一個色盤下不打架。**

**這份清單需要 TouchDesigner + projectM 都裝好才能跑，本機兩者皆無，所以以下全部未執行。**
自動測試（`uv run pytest -q tests/test_td_projectm.py`）只蓋到「腳本要求 TD 建什麼、接到哪」，
蓋不到任何一 frame 真的畫出來的畫面。

- [ ] `uv run python tools/projectm_check.py` 至少 projectM + 一條擷取路徑不是 missing
- [ ] projectM 單獨跑，暫停音樂圖案就平順 → 音源確定是 BlackHole，跟 TD 同一份
- [ ] TD 重跑 `build_network.py`，Textport 沒有 `[amv] SKIP pm_syphon` / `pm_ndi`
      （有的話代表這個 TD build 沒有那個 operator 類別，見 `td/README.md` 的 VERIFY 表）
- [ ] `Projectmsource` 切到 `syphon`（或 `ndi`），`projectm_in` 的 viewer 看得到 MilkDrop
- [ ] `pm_fit` 之後畫面**填滿** 1280²，不是角落一小塊、也不是被拉變形到不能看
- [ ] `Projectmsource` 切回 `none`，畫面應該只剩 TD 場景（黑底 × mix，不該有殘影）
- [ ] **fps**：`Projectmmix` 從 0 慢慢拉到 1，右下角 fps **全程維持 60**。
      掉 fps 的排查順序：先關 `record` → 改用 Syphon（NDI 較貴）→ 把 projectM 視窗縮小
- [ ] **色盤不打架**：五個 `Palette` 各切一次，`Projectmmix` 停在 0.5，
      看有沒有哪個組合糊成一團或互相抵消；有就照上一節縮 preset 池
- [ ] `Transitionbeats` 設 4、`Bpm` 設對，導演把 `projectm_mix` 從 0 送到 1，
      應該花約 4 拍**滑**上去而不是瞬間跳（SPEC §3.3 的 Lag）
- [ ] 演出中途把 projectM 直接關掉：TD **不得閃、不得黑**，
      最差只是那顆擷取 TOP 停格或轉黑（這也是 `pm_black` 存在的理由）

## 已知未驗證的猜測

以下 TD 名稱本機無法查證，都標在 `build_network.py` 裡（`# VERIFY`，見 `td/README.md` 的表）：

- operator 類別名 `syphonspoutinTOP`、`ndiinTOP`、`fitTOP`、`constantTOP`
- `pm_syphon` 的 sender 參數名（猜 `sender` / `sendername` / `syphonsender`）
- `pm_ndi` 的來源名稱參數（猜 `name` / `sourcename` / `ndiname`）
- `pm_fit` 的 fit 模式參數與 `fill` 選單值拼法、輸出解析度參數
- `pm_black` 的顏色／alpha 參數名

猜錯不會中斷建構：`create()` 找不到類別會印一行 `[amv] SKIP` 留 `None` 佔位，
`set_par()` 找不到參數名會印一行 `WARN ...; skipped`。第一次在 TD 裡執行後，
把 Textport 掃過一遍，那就是實際要改的清單。
