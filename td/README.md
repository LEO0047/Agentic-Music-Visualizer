# `td/` · 反射層（TouchDesigner）· Phase 2 + Phase 5

> **這份程式沒有在 TouchDesigner 裡跑過。** 本機沒有安裝 TD（SPEC Phase 0 的
> 「TouchDesigner 2023+」尚未滿足），所以整層是「可審查、可重跑的建構腳本」加上
> 「抽出來的純 Python 邏輯 + 單元測試」。所有無法在此驗證的 TD 參數名稱都標了
> `# VERIFY`，第一次在 TD 裡執行時要照著清單逐條確認。

## 檔案

| 檔案 | 在哪裡跑 | 說明 |
|---|---|---|
| `parspec.py` | 純 Python / TD | `director_schema.json` → TD Custom Parameter 規格；`lag_seconds()` |
| `osc_in_callbacks.py` | TD 的 OSC In DAT callback（也可單獨 import） | 把 `/director/*`、`/feat/section` 寫進 Custom Parameter；30 s 凍結；整數/字串排隊等 kick |
| `drop_executor.py` | TD 的 CHOP Execute / Execute DAT | `on_kick()` 執行 `on_drop` 並套用排隊中的整數/字串；`flush_pending()` 2 s 保險；`heartbeat_watchdog()` 45 s 警示 |
| `build_network.py` | **只在 TD 裡跑** | 建出整個 `/project1/amv`，可重複執行 |
| `td_stub.py` | 只給測試用 | TD Python 介面的極小假物件（`Par` / `Comp` / `TextDAT`） |

對應測試：`tests/test_td_parspec.py`、`tests/test_td_callbacks.py`、
`tests/test_td_drop_executor.py`、`tests/test_td_build_compiles.py`。
`uv run pytest -q` 不需要 TD 也不需要音訊裝置。

## 在 TouchDesigner 裡建網路

1. 開一個新的 `.toe`，確認有 `/project1`。
2. 打開 **Textport**（Alt/Option + T）。
3. 貼上並執行：

```python
exec(open('/Users/leohuang/Repos/Agentic-Music-Visualizer/td/build_network.py').read())
```

腳本會：先 `destroy()` 既有的 `/project1/amv`（**冪等**：改完程式直接再跑一次就好），
重建整個網路，最後印出所有建立的 operator 路徑與警告行。

路徑不同時（例如 repo 搬家）有三種指定方式，任選一種：

```python
AMV_TD_DIR = '/somewhere/else/td'          # 先設 global，再 exec
# 或
import os; os.environ['AMV_TD_DIR'] = '/somewhere/else/td'
# 或（td/ 已在 sys.path 時）
import build_network; build_network.build(td_dir='/somewhere/else/td')
```

`build_network.py` 會把 `td/` 塞進 `sys.path`，並在產生的 DAT 文字裡也寫一份
`sys.path` 設定，所以 TD 重開檔案後 callback 依然 import 得到 `parspec` /
`drop_executor`。

### 建出來的東西

```
/project1/amv
  audio_in (Audio Device In, "BlackHole 2ch", 48 kHz)
    └ spectrum (Audio Spectrum)
        ├ bass_trim → bass_avg → bass_lag → bass_norm → bass_clamp → bass  (20–150 Hz)
        ├ mid_*  → mid    (150 Hz–2 kHz)
        └ high_* → high   (2–16 kHz)
  audio_in → energy_rms → energy_lag → energy_norm → energy_clamp → energy  (RMS，lag 1 s)
  bass → kick_slope → kick_gate → kick_clamp → kick_logic → kick            (0/1 脈衝)
  centroid (Constant CHOP 佔位，Phase 3 換成 numpy Script CHOP)
  → features (Merge) → feat_names (Rename)
      → feat_rate (Resample, rate 10) → osc_out (127.0.0.1:9000)

  *_clamp 都是 Limit CHOP（type = clamp, min 0, max 1）。Math CHOP 沒有 clamp
  參數，`postclamp` 這種名字 TD 會直接忽略，所以正規化與夾限是兩顆 operator。

  director (Base COMP)             ← 所有 Custom Parameter，視覺網路只引用這裡
    osc_in (OSC In DAT, port 9001) + osc_in_callbacks (Text DAT)
    on_drop_dat (Text DAT)
    watchdog (Execute DAT，每 60 frame 呼叫 flush_pending + heartbeat_watchdog)
    kick_exec (CHOP Execute DAT，kick Off→On 呼叫 on_kick)
    par_exec (Parameter Execute DAT，手動一動就開始 30 s 凍結)

  dir_vals (Constant CHOP，值 = director 的 float 參數)
    └ lag_params (Lag CHOP，lag 秒 = Transitionbeats * 60 / Bpm)

  tunnel / fractal_temple / particle_field (三條 TOP 鏈，Null TOP 收尾)
    └ scene_switch (Switch TOP, index = Scene 的 menuIndex)
        └ palette (Lookup TOP，色盤來自 palette_switch 的五個 Ramp TOP
                   + 五個 pal_<name>_keys Table DAT)
            └ fb_mix (Cross TOP) ⇄ feedback (Feedback TOP) → fb_xform
                └ composite (Composite TOP，over projectm_level)
                    └ out (Null TOP) → window (Window COMP)
                                     → record (Movie File Out，record 綁 Record)

  pm_black (Constant TOP，黑 alpha 1) ┐  ← index 0 = none，沒有 projectM 時的保底
  pm_syphon (Syphon Spout In TOP)     ├ projectm_in (Switch TOP, index = Projectmsource)
  pm_ndi (NDI In TOP)                 ┘     └ pm_fit (Fit TOP, fill 1280²)
                                                └ projectm_level (Level TOP)
                                                   opacity = lag_params['projectm_mix']
                                                     └ composite 的 input 1
```

### Custom Parameter

`parspec.pars_from_schema()` 從 `director_schema.json` 產生，分兩頁：

- **Director**（導演會改的）：`Scene`、`Palette`、`Feedback`、`Symmetry`、
  `Cameraspeed`、`Particlemode`、`Projectmmix`、`Transitionmode`、
  `Transitionbeats`、`Ondrop`、`Intent`
- **Runtime**（演出狀態與這台機器的接法）：`Bpm`(145)、
  `Mode`(gpt/rule/**manual**，預設 rule)、`Section`(build/drop/breakdown/steady)、
  `Heartbeat`、`Heartbeatage`(唯讀)、`Record`、
  `Projectmsource`(none/syphon/ndi，預設 none — Phase 5，**沒有 OSC address**)

命名照 TD 規則：只有英數、首字大寫、其餘小寫，所以 `camera_speed` → `Cameraspeed`、
`projectm_mix` → `Projectmmix`。schema 裡巢狀的 `transition` 攤平成兩個參數；
`on_drop` 整包 JSON 存進 `Ondrop` 字串參數與 `on_drop_dat`。

## 依賴的 TD 行為（審查重點）

1. **OSC Out CHOP 用 channel 名當 OSC address。** 每個 sample 送一則訊息，
   address 就是 channel 名前面加 `/`。所以 `feat_names` Rename CHOP 把 channel
   改成 `feat/bass`、`feat/mid`… 送出去就是 `/feat/bass`。
   **若這版 TD 不接受 channel 名含 `/`**：拿掉 `feat_names`，改用 OSC Out CHOP 的
   address／prefix 參數設成 `/feat`，channel 名維持 `bass`、`mid`…（見 VERIFY #8/#11）。
   **10 Hz 從哪裡來**：OSC Out CHOP 是「cook 一次送一次」，沒有可靠的送出頻率參數，
   放著不管就是 60 Hz。真正決定頻率的是上游的 `feat_rate` Resample CHOP（rate = 10），
   它讓 `osc_out` 每秒只 cook 10 次。`osc_out` 上那行 `rate samplerate` 只是順手一設：
   有這個參數就跟 Resample 對齊，沒有就印一行 WARN，頻率仍然由 Resample 決定。
2. **OSC In DAT 的 callback DAT 只會呼叫慣例名稱的函式**：`onReceiveOSC(dat,
   rowIndex, message, bytes, timeStamp, address, args, peer)`。這個簽章被
   `tests/test_td_build_compiles.py` 釘住了。
3. **CHOP Execute DAT 的 Off→On 每個上升緣只呼叫一次**，所以 `on_drop` 一次 drop
   只會執行一次；`drop_executor` 另外還會清空 `on_drop_dat`，兩層保險。
4. **Menu / 字串參數沒有 Lag CHOP**，所以 `on_drop` 落地就是 cut，符合 SPEC
   「反射層在偵測到 drop 的那一 frame 直接執行」。只有 `Feedback`、`Cameraspeed`、
   `Projectmmix` 三個 float 走 `lag_params`。
5. **Ramp TOP 沒有「每個 key 一組參數」這種東西。** 漸層是一張表：一個 DAT，
   表頭 `pos r g b a`，一列一個 key（pos 0–1、顏色 0–1），由 Ramp TOP 的 `dat`
   參數指過去（TD 預設會自己生一個內部的 `<ramp>_keys` DAT）。所以每個色盤是
   「一個 `pal_<name>_keys` Table DAT + 一個 `pal_<name>` Ramp TOP」，色盤定義寫在
   `build_network.PALETTES`，每個三個 key（低／中／高），改顏色就改那個 dict。
6. **Parameter Execute DAT 分不出「人動的」與「程式寫的」。** `onValueChange`
   兩種都會觸發，而 SPEC §5 的 30 s 凍結現在三種模式都生效——不擋的話，導演每寫
   一個參數就會把那個參數自己凍住 30 s，一封 OSC 之後導演就等於失聯。所以
   `write_par()` / `drop_executor._write()` 每次寫入都會在 `comp.store('script_write')`
   留一個「這是我寫的，值是 X」的記號，`onValueChange` 比對值後吃掉它；值對不上
   （＝真的有人在動）就照常凍結。
7. **Trim CHOP 而不是 Select CHOP。** SPEC 寫「三段 Select」，但 TD 的 Select CHOP
   選的是 *channel*；要取 20–150 Hz 這種 *sample 範圍*，正確的 operator 是 Trim CHOP。
   起訖 index 用運算式 `freq / (48000/2) * op('spectrum').numSamples` 算，改 FFT 大小
   也不用重寫。
8. **Switch TOP 的 index 取模。** `Scene` 的 menu 有五個值（schema enum），但這一版只
   建了三個場景，所以 index 是 `menuIndex % 3`：`kaleido_mesh`、`projectm_blend` 會
   落回既有場景而不是黑畫面。Phase 5/6 補完後把 `IMPLEMENTED_SCENES` 加長即可。

## Phase 2 手動驗收（SPEC §6 #2）

> 驗收標準：**不開 Director，手動拖 Custom Parameter，60 fps 反應且參數會滑。**

前置：Phase 1 的多重輸出裝置已設好（`docs/phase1-audio-routing.md`），Apple Music 在播歌。

1. **音訊進得來**：點 `audio_in`，Viewer 應該看得到波形在動。接一個 Trail CHOP 到
   `bass`，播 Psytrance 時 kick 應該是明顯的尖峰。若 `spectrum` 全平，先回頭檢查
   Audio Device In 的 Device 是不是 `BlackHole 2ch`、取樣率是不是 48000。
2. **特徵在 0–1 之間**：`features` 的 bass/mid/high/energy 應該在 0–1 遊走而不是貼在
   0 或 1。貼邊就調 `*_norm` Math CHOP 的 `fromrange2`（bass 預設 0.25、其餘 0.15）。
3. **kick 是脈衝**：`kick` 應該是 0/1，且一拍一次。太密就把 `build_network.py` 的
   `KICK_THRESHOLD` 調高再重跑腳本。
4. **手動拖參數**：選 `director`，打開 Parameter 視窗的 **Director** 頁。
   - `Scene`：三個場景要能切（`fractal_temple` / `tunnel` / `particle_field`）。
   - `Palette`：五個色盤，畫面顏色要跟著換。五個看起來一樣就先點開
     `pal_violet_cyan_keys` 這五個 Table DAT——裡面該是 `pos r g b a` 表頭加三列；
     表對但畫面不變，就是 Ramp TOP 的 `dat` 參數名猜錯了（VERIFY #22）。
   - `Feedback` 0 → 0.9：拖動後畫面拖尾要**慢慢**長出來，不是瞬間跳。
   - `Cameraspeed`、`Projectmmix` 同理。
5. **glide 長度對得上**：`Runtime` 頁把 `Bpm` 設成正在播的曲子 BPM，`Transitionbeats`
   設 4。在 120 BPM 下拖 `Feedback`，應該花約 **2 秒**滑到位（4 × 60 / 120）。
   把 `Transitionbeats` 改成 1 再拖，應該明顯變快。
6. **60 fps**：右下角 fps 全程要維持 60（Non-Commercial 版輸出上限 1280×1280，
   場景解析度已設 1280）。掉 fps 先關 `record`，再把 `pf_blur` 的 size 調小。
7. **Director 不在也活著**：整個過程 sidecar 都沒開。`Heartbeatage` 會一路往上加，
   超過 45 s 後 Textport 每 5 秒印一行警告，但**畫面必須維持現狀、不得閃或黑**。
8. **凍結**（SPEC §5，三種模式都適用）：`Mode` 留在 `rule`，手動拖 `Feedback`，
   30 秒內就算 sidecar 送 `/director/feedback` 進來也不會覆蓋。切 `manual` 後
   **所有** `/director/*` 都不落地，只有 `/feat/section` 與 `/director/heartbeat`
   照收（可用 `oscsend` 或 Phase 4 的 sidecar 測）。
   反過來也要確認：`rule` 模式下連送兩則 `/director/feedback`，第二則必須也生效——
   若第一則自己把參數凍住了，就是「依賴的 TD 行為」#6 的 echo 記號沒生效。
9. **整數/字串等下一個 kick**：`Transitionmode` 設 `glide`，送
   `/director/scene particle_field`，畫面**先不要換**；下一個 kick 才切過去。
   把音樂停掉（沒有 kick）重送一次，最多 2 秒後由 watchdog 的 `flush_pending`
   補上。`Transitionmode` 設 `cut` 則是立刻換。

不需要 TD 的部分（OSC 路由、凍結、等 kick 排隊、on_drop 一次性、watchdog 門檻）
已經被 `uv run pytest -q` 蓋掉了；上面這九步是只有 TD 能回答的部分。

## Phase 5 · projectM 側鏈

設定手冊與完整驗收清單在 **[`docs/phase5-projectm.md`](../docs/phase5-projectm.md)**：
projectM 怎麼獨立跑、音源怎麼設成 BlackHole、兩條擷取路徑（Syphon ≈1 frame／
OBS+NDI 2–4 frames）怎麼接、preset 為什麼不歸導演管。
`uv run python tools/projectm_check.py` 會列出這台機器上還缺什麼（永遠 exit 0）。

### 多出來的 operator

`build_projectm_input(amv)` 取代了 Phase 2 那顆空的 `projectm_in` Null TOP，
把三條可能的來源**並排**建出來，切換是一個選單而不是重建網路：

| operator | 類別 | 做什麼 |
|---|---|---|
| `pm_black` | Constant TOP | 黑、alpha 1、1280²。Switch 的 **index 0**，也是預設 |
| `pm_syphon` | Syphon Spout In TOP | 接 Syphoner 發佈的 `projectM` server（≈1 frame） |
| `pm_ndi` | NDI In TOP | 接 OBS + NDI 外掛送出的 `projectM` 來源（2–4 frames） |
| `projectm_in` | Switch TOP | index = `op('director').par.Projectmsource.menuIndex % 3` |
| `pm_fit` | Fit TOP | 把任何解析度的 projectM 畫面 fill 到 1280² 畫布 |

`projectm_level`（Level TOP，opacity 綁 `lag_params['projectm_mix']`）與
`composite`（`operand = over`）維持 Phase 2 的接法，只是輸入從 `projectm_in` 改成 `pm_fit`。

### 三件刻意的設計

1. **`Projectmsource` 在 Runtime 頁，而且沒有 OSC address。** SPEC §3.3 給導演的
   projectM 控制只有 `/director/projectm_mix` 一條。「畫面從哪條線進來」是這台機器
   怎麼接的問題，由設定它的人決定；導演只決定看不看得到、看多濃。
2. **黑底是預設，也是保底。** Syphon / NDI 這兩顆 TOP 在沒裝外掛的 TD 上根本不存在，
   `create()` 會印 `[amv] SKIP pm_syphon` 留 `None` 佔位，Switch 的那個 input 空著。
   但 index 0 永遠是真的：什麼都沒裝、projectM 沒開、演出中途被關掉，
   `projectm_in` 仍然 cook 出一張合法的 1280² 黑底，Composite 永遠良好定義。
   （反過來說：**沒接來源時 `Projectmmix` 要留在 0**，否則就是把黑底疊上去壓暗畫面。）
3. **`pm_fit` 不是裝飾。** projectM 的視窗可能是 640×480，直接進 Composite 會變成
   畫面角落的一小塊長方形；Fit TOP（fill）才是「MilkDrop 圖層」而不是「MilkDrop 貼紙」。

**libprojectM 內嵌 Custom TOP 不做**（SPEC Phase 5 細節 / VISION V3）：macOS 版 TD 走
Metal，libprojectM 要 OpenGL context。V1 用行程間畫面傳輸換掉這個問題，
代價是多 1–4 frame 與 preset 不受控。

### 這裡測得到什麼、測不到什麼

`tests/test_td_projectm.py` 用 `tests/test_td_build_compiles.py` 的 `Recorder` harness，
釘住「腳本向 TD 要了哪些 operator 類別、接線順序、index 運算式、Composite 的接法、
`Projectmsource` 的選單」——全部不需要 TD。

測不到的是任何一 frame 真的畫出來的畫面：類別名對不對、`fitmode` 存不存在、
MilkDrop 有沒有真的進到 Composite。那些是下面 VERIFY 表的 #31–#35，
以及 `docs/phase5-projectm.md` 的手動驗收清單
（SPEC §6 #5：**`projectm_mix` 0→1 全程 fps 不掉，MilkDrop 圖層與 TD 場景在同一個色盤下不打架**）。

## `# VERIFY` 清單

TD 不同版本的參數名稱會變，而本機無法查證。`build_network.py` 裡共 **40 處** `# VERIFY`
（`grep -c '# VERIFY' td/build_network.py` 是 41，其中一處在檔頭 docstring 裡）。
Phase 5 加了 5 處，都在 `build_projectm_input()` 與 Composite 段（表格 #31–#35）。
所有設定都走 `set_par()` / `set_expr()`，名字不存在只會印一行 `WARN ...; skipped`
然後繼續，**不會中斷建構**——所以第一次執行後請先把 Textport 的 WARN 全部掃過一遍，
那就是實際猜錯的清單。

**operator 類別名猜錯也一樣不會中斷。** `create()` 找不到任何一個候選類別時會印
`[amv] SKIP <name>: none of <candidates> exist in this TD build`，回傳一個 `None` 佔位
（`set_par` / `set_expr` / `set_text` / `table_rows` / `connect` 都吃得下），最後 `summary()`
會把整份 SKIP 清單再印一次。一個名字猜錯不會留下半個網路。

`set_par(node, "a b c", value)` 的第二個參數是「候選名稱，最有把握的排前面」，
改對之後直接改字串即可。

| # | 位置 | 要確認什麼 |
|---|---|---|
| 1 | `audio_in` | operator 類別 `audiodevinCHOP` vs `audiodeviceinCHOP` |
| 2 | `audio_in.device` | Device 參數名，以及能不能直接塞字串 `"BlackHole 2ch"` |
| 3 | `audio_in.rate` | 取樣率參數是 `rate` 還是 `samplerate` |
| 4 | `*_trim.units` | Trim CHOP 單位選單值 `samples` 的拼法、`start`/`end` 參數名 |
| 5 | `*_avg.function` | Analyze CHOP 選單值 `average` / `rms` 的拼法 |
| 6 | `*_clamp.type` | **Limit CHOP 的 `clamp` 選單值拼法**（`min`/`max` 兩個參數名很穩）。Math CHOP 沒有 clamp，這五顆 Limit CHOP 才是 0–1 的保證 |
| 7 | `kick_logic` | Logic CHOP「Convert Input」參數名（`convert` vs `preop`）與選單值 `offwhenzero` |
| 8 | `*.renamefrom/renameto` | Rename CHOP 的樣式參數名 |
| 9 | `centroid` | Constant CHOP 是 `const0name`/`const0value` 還是 `name0`/`value0` |
| 10 | `feat_names` | channel 名能不能含 `/`（不行就改用 OSC Out 的 address prefix） |
| 11 | `osc_out` / `feat_rate` | `netaddress` vs `address`；Resample CHOP 的 `method` 選單值（`rate` 很穩）。10 Hz 由 `feat_rate` 決定，`osc_out.rate` 只是順手一設 |
| 12 | `osc_in.callbacks` | OSC In DAT 的 Callbacks DAT 參數名與填法（路徑字串 vs operator） |
| 13 | `osc_in.clear` | 「Clear Each Frame」toggle 名稱 |
| 14 | `watchdog.framestart` | Execute DAT 的 callback 開關名稱 |
| 15 | `kick_exec.chops` | CHOP Execute DAT 的來源 CHOP 參數名（`chops` vs `chop`）與相對路徑 `../kick` |
| 16 | `par_exec.op` | Parameter Execute DAT 的來源 operator 參數名（`op` vs `ops`） |
| 17 | `tunnel_ramp.type` | Ramp TOP 的 `radial` / `horizontal` 選單值 |
| 18 | `*_noise.type` | Noise TOP 的 `sparse` / `random` 選單值 |
| 19 | `tunnel_disp` | Displace TOP 的 UV weight 參數名 |
| 20 | `ft_noise.harmon/gain` | Noise TOP 的 fractal 參數名（`harmon` vs `harmonics`） |
| 21 | `ft_mirror.extend*` | Transform TOP Extend 選單值 `mirror` 的拼法 |
| 22 | `pal_*.dat` | **Ramp TOP 指到 key 表的參數名**（猜 `dat`）。漸層在 `pal_<name>_keys` Table DAT 裡（`pos r g b a` 表頭 + 三列），不是 key 參數。這個名字猜錯，五個色盤才會長得一模一樣 |
| 23 | `pf_thresh.threshold` | Threshold TOP 的門檻參數名 |
| 24 | `pf_blur.size` | Blur TOP 的 filter size 參數名 |
| 25 | `palette` (Lookup TOP) | 哪個 input 是影像、哪個是查表 |
| 26 | `feedback.top` | Feedback TOP 的 Target TOP 參數名 |
| 27 | `projectm_level.opacity` | Level TOP 的 opacity 參數名 |
| 28 | `composite.operand` | Composite TOP 的 `over` 選單值 |
| 29 | `window` | Window COMP 指定顯示 operator 的參數名 |
| 30 | `record.file` / `record.record` | Movie File Out TOP 的檔名與錄影參數名 |
| 31 | `pm_syphon` | **operator 類別 `syphonspoutinTOP`**（沒裝 Syphon 外掛的 TD 根本沒有這個類別，會 SKIP） |
| 32 | `pm_syphon.sender` | Syphon Spout In TOP 的 server 名稱參數（`sender` / `sendername` / `syphonsender`），以及 Syphoner 實際發佈的名字是不是 `projectM` |
| 33 | `pm_ndi` | **operator 類別 `ndiinTOP`** 與來源名稱參數（`name` / `sourcename` / `ndiname`）；名字要跟 OBS 的 NDI 輸出設定一致 |
| 34 | `pm_fit` | Fit TOP 類別名、`fit`/`fitmode` 參數與 `fill` 選單值拼法、輸出解析度參數（`outputresolution` vs `resolutionmenu`） |
| 35 | `pm_black` | Constant TOP 的顏色／alpha 參數名（`colorr` vs `color1r`、`alpha` vs `color1a`） |

（表格把同一類的多個 `# VERIFY` 合併成一列；逐行位置請 `grep`。）

## 已知的刻意取捨

- `particle_field` 目前是 Noise → Threshold → Blur 的**替身**，不是真的 Particle GPU；
  `Particlemode` 只餵給 noise 的 seed。Phase 6 才會換成真的粒子系統。
- `centroid` 送的是常數 0。SPEC §3.1 本來就把它列為選配，Phase 3 用 Script CHOP + numpy 補。
- `projectm_in` 已經是 Phase 5 的 Switch TOP（black / Syphon / NDI），但**沒有在 TD 裡跑過**，
  而且本機沒有 projectM、Syphoner、OBS、NDI runtime（`tools/projectm_check.py` 全列 missing）。
  預設的 `Projectmsource = none` 走黑底那條，所以缺什麼都不影響建構。
- `Symmetry` 目前只驅動 `ft_mirror` 的 scale / rotate，還不是 GLSL uniform。
  SPEC §3.3 的「整數與字串類參數只在下一個 kick 切換」本身已經實作了（見下一條），
  Phase 6 才把 `Symmetry` 換成真正的 GLSL uniform。
- **「等下一個 kick」有 2 秒上限。** `Scene` / `Palette` / `Symmetry` / `Particlemode`
  進來時不直接寫參數，而是存進 `comp.store('pending_discrete')`，由 `on_kick()` 在下一個
  kick 套用（`Transitionmode = cut` 則立刻套用）。但 breakdown 可能整段沒有 kick，
  所以 watchdog 每秒的 `flush_pending()` 會把等超過 `PENDING_MAX_WAIT`（2 s）的值直接落地：
  晚一拍落地，好過永遠不落地。同一顆參數若 `on_drop` 也要寫，`on_drop` 優先，
  排隊中的那個直接丟掉——那是導演**為這個 drop** 挑的。
- `heartbeat_watchdog` 在**從來沒收到過心跳**時，以第一次呼叫的時間當基準；
  所以 sidecar 根本沒開的話，45 秒後一樣會警示（開機即警示反而看不出差別，
  而「TD 先開、sidecar 晚 45 秒才開」本來就該警示一次）。
- 凍結的 echo 記號（`script_write`）用**值**比對而不是時間戳，所以完全不看時鐘；
  代價是「人手動把參數轉回導演剛寫的那個值」這一種情況不會觸發凍結。

## 時間來源

`osc_in_callbacks` 與 `drop_executor` 都用 `time.time()`，不是 TD 的 `absTime.seconds`——
兩個模組必須用同一個時鐘（凍結時間戳寫在 A、讀在 B），而 `time.time()` 在 pytest 裡也一樣
能用。45 秒的 watchdog 與 30 秒的凍結對時鐘精度都不敏感。
