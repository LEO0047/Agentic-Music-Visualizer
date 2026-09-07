# Phase 6 · 演出強化（不需要 TouchDesigner 的那一半）

SPEC §6 的 Phase 6 是「持續」階段：反重複、`on_drop`、錄影、MIDI 覆寫、app-server 評估。
這份文件負責的是其中**在沒有 TouchDesigner 的機器上就能做完、也能驗收**的部分——
上場前檢查表、60 分鐘 dry run、Codex session 清理、啟動 smoke test——
以及兩件必須先把設計寫清楚、實作跟驗收都還在 TD 那邊的事（MIDI 覆寫、錄影對時）。

SPEC 對應：§2 額度、§5 三種模式、§6 Phase 6、§7 風險表、§8 上場前檢查。

| 東西 | 檔案 | 一句話 |
|---|---|---|
| 上場前檢查表 | `tools/preflight.py` | SPEC §8 變成一個會 exit 1 的指令 |
| 60 分鐘 dry run | `tools/dry_run.py` | 跑滿一場，回答「額度撐不撐得住」與「會不會重複」 |
| session 清理 | `tools/codex_sessions.py` | 只動本專案產生的 Codex session，`list` / `archive` / `clean` |
| 啟動 smoke test | `DirectorLoop.startup_check()` | GPT 路徑活不活，在 load-in 就知道，不是在第一個 drop |

---

## 1 · `tools/preflight.py` — 上場前檢查表

```bash
uv run python tools/preflight.py                     # 預設：不花任何額度
uv run python tools/preflight.py --json              # 機器可讀
uv run python tools/preflight.py --real-codex        # 唯一會花 ~25k tokens 的用法
uv run python tools/preflight.py --record-dir /Volumes/SSD/amv --meter-seconds 10
uv run python tools/preflight.py --skip-audio        # 音訊介面還沒接
```

八項，照人會做的順序跑，每項印 `✅` / `⚠️` / `❌` 加一行理由。**沒有 `❌` 就 exit 0。**

| # | 項目 | 內容 | 什麼情況會是 ❌ |
|---|---|---|---|
| 1 | environment | 直接跑 `tools/check_env.py` 的每一列 | python < 3.11、codex 找不到、`~/.codex/auth.json` 不在 |
| 2 | blackhole loopback | `audio_check loopback`：自己放 1 kHz 進 BlackHole 再讀回來 | 驅動裝了卻讀不回測試音 |
| 3 | multi-output routing | `audio_check meter` 聽 3 秒 | （不會 ❌）沒訊號是 ⚠️ |
| 4 | codex binary | `codex --version` | 二進位在但叫不動 |
| 5 | codex smoke test | 一次真的 `decide()` | 只在有跑的時候才會 ❌ |
| 6 | fallback drill | fake codex `fail` → `GPTDirector` → 必須 2 秒內給出 rule 決策 | 沒落到 rule、或超過 2 秒 |
| 7 | record disk space | `~/Movies`（或 `--record-dir`）剩餘 > 5 GB | 空間不夠 |
| 8 | `~/.codex/sessions` | 檔案數 > 500 就 ⚠️ | （不會 ❌） |

### ⚠️ 跟 ❌ 的分界線（這是整個工具的設計）

`⚠️` 是**你自己決定不做的事**：projectM 沒裝、TouchDesigner 不在這台、音樂還沒開始播。
`❌` 是**會讓演出當場死掉的事**。一張分不清這兩者的檢查表，第三場就沒有人會看。

所以「multi-output routing 沒訊號」是 ⚠️ 而不是 ❌——那也是「檢查表在有人按下播放之前就跑了」
的正常樣子；理由那一行會直接說 `no signal — Multi-Output not set or nothing playing`。
同理，TouchDesigner.app 不在只會 ⚠️：這張表管的是 sidecar 那半邊。

### 額度

會花 ChatGPT 訂閱 Codex 額度的**只有第 5 項**，而且預設是跳過的：

- 加 `--real-codex` → 用真的 codex 跑一次決策（約 25k tokens，SPEC §2）。
  （`find_codex()` 仍然優先吃 `$AMV_CODEX_BIN`，所以兩個一起設的時候用的是那個二進位——
  這種情況理由行下面會多一行講清楚。）
- 有設 `$AMV_CODEX_BIN` → 用它跑，這就是拿 `tools/fake_codex.py` 免費驗收的方法。
- 兩者都沒有 → `⚠️ skipped`。

第 4 項的 `codex --version` 跟第 6 項的 fallback drill 都不花錢：前者是子行程的 exit code，
後者跑的是 `tools/fake_codex.py`。

### 本機實測（2026-09-08）

```
$ AMV_CODEX_BIN=$PWD/tools/fake_codex.py uv run python tools/preflight.py
AMV preflight — SPEC §8 上場前檢查
------------------------------------------------------------------------
⚠️  environment           選配缺席：TouchDesigner.app、projectM、OBS
✅  blackhole loopback    BlackHole 回讀 1000 Hz / RMS 0.178
⚠️  multi-output routing  no signal — Multi-Output not set or nothing playing (mean RMS 0.0000)
✅  codex binary          codex-cli 0.153.4 (amv fake) — .../tools/fake_codex.py
✅  codex smoke test      0.5s → tunnel/acid_lime（fake_codex.py，未花額度）
✅  fallback drill        codex fail → rule 接手 0.55s（kaleido_mesh/violet_cyan）
✅  record disk space     /Users/leohuang/Movies 還有 73 GB
✅  ~/.codex/sessions     167 個檔案 / 1317 MB
```

### 這張表**不**檢查的（SPEC §8 的另一半，要開著 TD 用眼睛看）

- TD 波形在動、Trail 看得到 kick 尖峰
- 不開 Director，TD 能自己演完一首歌
- 熱鍵切 gpt / rule / manual 真的有到網路上
- `on_drop` 有填，而且 drop 命中時畫面真的有反應
- Movie File Out 真的在錄
- projectM 若啟用，`projectm_mix` 0→1 fps 穩定

---

## 2 · `tools/dry_run.py` — SPEC §2 的 60 分鐘 dry run

```bash
# 正式演出前一天，跑滿一小時
uv run python tools/dry_run.py --minutes 60 --director gpt --report dry_run.md

# 壓縮測試：60 分鐘的節目在 3 分鐘牆鐘裡跑完（決策數、額度推估一樣準）
uv run python tools/dry_run.py --minutes 60 --speed 20 --director gpt --report /tmp/dry.md

# 免費版：用假的 codex 驗證節奏與反重複，不花額度
AMV_CODEX_BIN=$PWD/tools/fake_codex.py \
  uv run python tools/dry_run.py --minutes 2 --speed 20 --director gpt --report /tmp/dry.md

# 真的 TD 在送 /feat/*：不要開合成源
uv run python tools/dry_run.py --minutes 60 --live --listen 127.0.0.1:9000
```

它把 `python -m amv.sidecar` 和 `tools/fake_td.py` 當子行程開起來，跑滿 `--minutes` 的
**節目時間**，然後把決策 JSONL 讀回來出一份 Markdown 報告。Ctrl-C 隨時可以停，
兩個子行程都會被收掉，而且照樣出報告（exit 130）。

### `--speed` 是怎麼算的

`--speed` 同時餵給兩個子行程，所以 sidecar 的決策週期和段落判定窗都留在**節目時間**，
只有牆鐘變快。`--minutes 60 --speed 20` = 3 分鐘真實時間，但仍然產生一小時該有的
約 200 次決策。

延遲永遠是**真秒**（`codex exec` 不會因為時鐘被縮放就變快），所以壓縮跑出來的
latency 統計是誠實的；間隔（gap）統計則是節目時間。

`tools/fake_td.py` 的劇本大約 116 秒，播完就結束——dry_run 會一直重開它直到時間到。
對「額度」跟「反重複」這兩個問題夠用；它不是一場音樂彩排。

### 報告裡有什麼

- **決策**：總數、`by_source`（`gpt` / `rule (CodexError)` / …）、gpt 佔比、section 分布
- **延遲**：mean / p50 / p95 / max
- **節奏**：決策間隔 min / mean / max、最長沒換 scene（次數與秒數）
- **反重複**：60 秒內重複的 scene+palette 數量——**這個數字必須是 0**。
  `amv.director.enforce_variety` 的工作就是讓它不可能發生，所以不是 0 代表
  反重複規則有洞，不是「這場比較單調」。報告會把每一次重複列出來。
- **額度推估**：decisions/hour × `--tokens-per-decision`

`--decisions-log PATH` 可以指定決策 log 位置；沒給的話它在暫存目錄，跑完就消失
（報告本身仍然會留在 `--report`）。

exit code：0 = 有決策且 0 重複；1 = 沒有任何決策或有重複；130 = 被 Ctrl-C。

### 本機實測（2026-09-08，假 codex，2 分鐘 ×20）

```
decisions 7  ({"gpt": 7})
latency  mean 0.55s  p50 0.55s  p95 0.56s  max 0.56s
gaps     min 14.8s  mean 17.6s  max 18.4s  (show time)
variety  longest scene run 1 decisions / 0s   repeats within 60s: 0
quota    210 decisions/h × 25,000 tokens = 5,250,000 tokens/h → 26.2M per 5h window
```

間隔比 `--period 18` 大一點點，是因為導演只能在 sidecar 的 status tick 上開火
（`--status-rate`，預設 1 Hz 節目時間）；比 18 小的那幾次是段落事件觸發
（`--min-interval` 之上，build / drop / breakdown 到站時可以插隊）。

---

## 3 · 額度數學（SPEC §2）

2026-09-08 實測：**一次決策 25,192 tokens、端到端 13.2 秒**（含冷啟動，effort low）。
那 25k 幾乎全是 Codex 自己的系統提示——我們的 prompt 被 `MAX_PROMPT_CHARS = 2000` 壓著，
不是預算所在。**所以 token 用量只跟「一小時開幾次 `codex exec`」有關，跟 prompt 內容幾乎無關。**

| `--period` | 決策 / 小時 | tokens / 小時 | 5 小時窗 |
|---|---|---|---|
| 15 s | 240 | 6.0M | 30.0M |
| **18 s（預設）** | **200** | **5.0M** | **25.0M** |
| 20 s | 180 | 4.5M | 22.5M |
| 30 s | 120 | 3.0M | 15.0M |

（實際會再高幾 %，因為 build / drop / breakdown 到站時會事件觸發插隊；
上面實測那次是 210/h。）

Codex 額度是以 **5 小時滾動窗**和**每週窗**結算的，而不是「每天多少」。
一場兩小時的 set 在 18 s 週期下就是約 10M tokens；連著兩場、或當天還用 Codex 寫過程式，
撞窗是**預期中的事**，不是意外。

**演出前請跑 `uv run python tools/dry_run.py --minutes 60`**，看報告裡的
`tokens / 5 小時窗` 那一格，然後做三件事之一：

1. 週期留 18 s，接受中途可能撞窗——**因為 rule fallback 是常駐的**，撞窗只會讓
   `by_source` 從 `gpt` 變成 `rule (CodexError)`，畫面不會停（SPEC §7）。
2. 開場就把 `--period` 設 30 s，把量砍到 3.0M/h，換取全場都是 GPT。
3. set 中途發現額度在燒：切 `r`（rule）撐一段，再切回 `g`。

第 1 條是預設立場。SPEC §7 寫得很直白：「規則式 fallback 必須常駐」——
`RuleDirector` 不是道歉，它是一個能自己演完一小時的狀態機。

---

## 4 · fallback 鏈：gpt → rule → manual

```
        ┌─────────────────────────── 熱鍵 g / r / m（Hotkeys，SPEC §5）
        ↓
   ┌────────┐  codex 失敗/逾時/額度用完   ┌────────┐  人手動一動   ┌──────────┐
   │  gpt   │ ─────────────────────────→ │  rule  │ ────────────→ │  manual  │
   └────────┘   （每一次決策各自 fallback）└────────┘   （只凍那一欄）└──────────┘
        │                                     │                        │
        └──────────────── 三種模式都照送 /director/heartbeat ───────────┘
```

三層，各自負責不同的失敗：

**第一層 · 每次決策的 fallback（`GPTDirector.decide`）。**
`CodexError`、逾時、輸出不合 schema、甚至這個檔案自己的 bug——任何例外都不會往外丟，
而是改由 `RuleDirector` 回答，並把原因記在 `_source`（例如 `rule (CodexError)`）。
**模式不會因此改變**：下一次決策還是會再試一次 GPT。這是刻意的，網路抖一下不該讓整場降級。
代價是「GPT 四十分鐘前就靜靜死了」這種失敗要靠 `_source` 才看得出來——
所以它會出現在 log、在 `loop.stats()`、在 dry run 報告的 `by_source` 裡。

**第二層 · 啟動 smoke test（`DirectorLoop.startup_check`，SPEC §7）。**
「Codex 版本改 flag」的對策。它會**改模式**：

```python
loop = DirectorLoop(td, GPTDirector(CodexClient(), RuleDirector()))
loop.startup_check(timeout_s=20.0)               # 只跑 codex --version，免費
loop.startup_check(timeout_s=40.0, smoke=True)   # 再跑一次真的 decide()，~25k tokens
```

- 一定跑 `client.version()`：子行程能不能起來、CLI 還在不在。零 token。
- `smoke=True` 才跑一次真的 `decide()`：走完 argv → schema → parse → clamp 整條路，
  這是**唯一**抓得到「flag 改名了」「`--output-schema` 契約變了」的檢查，所以要明講。
- 任何一步失敗 → 印警示 → `mode` 變成 `rule`。修好之後熱鍵 `g` 可以切回去。
- smoke 那次決策會被丟掉：不送 OSC、不寫 log、不進 history，
  所以當晚第一個真決策看到的還是空的 history（60 秒不重複窗不會被吃掉）。

它**沒有**被接進 `amv/sidecar.py`：`--director gpt` 在找不到二進位時本來就會退成 rule，
而「每次開行程都偷偷花 25k tokens」不是一個 CLI 該做的事。
要花錢的那個版本住在 `tools/preflight.py` 第 5 項。

**第三層 · manual 與 TD 端的 30 秒凍結（SPEC §5）。**
`manual` 模式不做決策，但**照送 heartbeat**——TD 的 45 秒 watchdog 不該因為你切了手動就報警。

熱鍵（`amv.director.Hotkeys`：`g` / `r` / `m` 切模式、`q` 停）改的是 sidecar 那邊的
「要不要產生決策」。TD 那邊還有一個獨立的、**逐欄位**的凍結：

> 任何參數被手動一動，該欄位凍結 30 秒。

兩者的分工是這樣的：

| 你做的事 | 誰接住 | 影響範圍 | 時間 |
|---|---|---|---|
| 按 `r` | sidecar `DirectorLoop.mode` | 全部欄位，改由 RuleDirector 產生 | 直到你按別的 |
| 按 `m` | sidecar `DirectorLoop.mode` | 全部欄位，完全不產生決策（只有 heartbeat） | 直到你按別的 |
| 在 TD 裡拖一個 Custom Par | TD `osc_in_callbacks` 的 Parameter Execute DAT | **只有那一欄** | 30 秒（`FREEZE_SECONDS`） |
| 轉 MIDI 控制器的旋鈕 | 同上（見第 5 節） | **只有那一欄** | 30 秒 |

關鍵在於**這是兩套獨立的機制**：sidecar 完全不知道 TD 凍結了哪一欄，
它照樣把整包決策送出去；TD 端在寫入前檢查凍結，把被凍的欄位丟掉、其餘照寫。
所以「手動壓著 feedback 不放，但場景還是會跟著音樂換」是設計，不是 bug。

TD 端還有一個 `Mode` Custom Par（`td/parspec.py` 的 `MODES`），它跟 sidecar 的模式
是**同一個概念的兩個副本**：`Mode = manual` 時 TD 端拒收所有 `/director/*`。
上場前請確認兩邊是一致的——這是 SPEC §8「熱鍵切 gpt / rule / manual」那一項要用眼睛看的原因。

---

## 5 · MIDI 覆寫（TD 端設計）— **pending，尚未在 TouchDesigner 裡跑過**

SPEC §6 Phase 6：「MIDI 覆寫：一台小控制器接 TD，任何參數你手動一動就凍結該欄位 30 秒」。

### 設計：不要發明第二套凍結

TD 裡已經有一條「人碰過 → 凍結 30 秒」的路，走的是 Parameter Execute DAT 的
`onValueChange`。所以 MIDI 覆寫**不需要任何新的凍結簿記**，只需要讓旋鈕走進同一條路：

```
MIDI 控制器
   └ MIDI In CHOP（channel 1，每個 CC 一個 CHOP channel：ch1c1、ch1c2 …）
        └ CHOP Execute DAT（director COMP 內）
             └ onValueChange(channel) → 把 0–127 換算成該 par 的值 → 直接寫 Custom Par
                                          （**故意不留 script-write 標記**）
                  └ Parameter Execute DAT 的 onValueChange 照常觸發
                       └ claim_script_write() 找不到標記 → 當成人手
                            └ note_touch() → 該欄位凍結 30 秒
                                 └ 下一個 /director/<該欄位> 被跳過（gpt 與 rule 都一樣）
```

整個機制就是**少寫一個標記**。`osc_in_callbacks.write_par` 和 `drop_executor` 在動參數前
會留一個一次性的「這次是我寫的」記號，讓 `onValueChange` 分得出腳本回音和人手；
MIDI 那條路不留記號，於是 TD 完全把它當成有人在拖滑桿——因為那本來就是實話。

兩個跟 OSC 寫入刻意不同的地方：

- **不等下一個 kick。** 導演的 `Scene` / `Palette` / `Symmetry` / `Particlemode` 會排隊等
  kick 再切（SPEC §3.3，避免閃畫面）。手轉旋鈕是「有人現在就要改」，直接寫穿——
  轉 Scene 旋鈕就是當下那一 frame 切場景。
- **不加 Lag。** float 直接寫進 par，下游的 Lag CHOP 照樣平滑，跟人拖滑桿完全一樣。

### 現況

`td/midi_override.py` 已經存在（另一位代理在 Phase 6 同時交付，含 `DEFAULT_CC_MAP`
七個旋鈕、`DEFAULT_NOTE_MAP` 三個 pad 對應 `Mode` 的 gpt/rule/manual），
`tests/test_td_midi.py` 用 `td/td_stub.py` 跑得過。

**但它沒有在真的 TouchDesigner 裡跑過，所以這一項標記為 pending。**
上線第一件要驗的是 MIDI In CHOP 的 channel 命名（`ch1c1` 還是 `c1` 還是 `ctrl1`），
那是唯一一個測試無法替我們回答的問題——`td/midi_override.py` 裡的 `# VERIFY` 註解
指的就是這件事。驗收方式：接上控制器，轉一個旋鈕，Textport 應該印出
`[amv midi]` 那一行，而且該欄位接下來 30 秒不再被導演改動。

---

## 6 · codex `app-server` 常駐評估 — **V2**

SPEC §2 的路線 B：不要每次決策開一個 `codex exec`，改成常駐一個
`codex app-server`（或 `mcp-server`），省掉冷啟動。

**V1 不做。** SPEC 已經把理由寫清楚了：experimental，而 V1 的決策週期是 15–20 秒、
drop 反應本來就交給反射層，13.2 秒的冷啟動雖然難看但不致命；
而路線 A 的「每輪一個獨立行程」有一個 app-server 換不到的性質——
**一次掛住只會賠掉一個週期**，行程結束就把狀態一起帶走了。

要在 V2 推翻這個決定，需要量的是三件事：

1. **冷啟動 vs 熱呼叫。** 現在的 13.2 秒裡有多少是行程啟動與模型冷啟動？
   量法：在同一個 app-server 上連續發 20 次同樣的決策請求，記 p50 / p95。
   如果熱呼叫穩定落在 3–5 秒，那 `--period` 就有機會從 18 s 降到 8–10 s，
   導演層才真的追得上段落，而不是永遠慢半拍。
   （注意：**這不會省 token**。25k 幾乎都是系統提示，每次請求照樣要付。
   app-server 買的是延遲，不是額度。）
2. **協定穩定性。** app-server 的 JSON-RPC 介面在 codex-cli 的小版本之間會不會變？
   量法：把送出的 request / 收到的 response 存成 golden file，跨兩三次 CLI 升級重跑。
   `codex exec --output-schema` 這條路目前是**有契約**的（schema 檔就是契約），
   換成 app-server 等於把契約換成一個沒有版本承諾的內部介面——
   這是 SPEC §7「Codex 版本改 flag」那一列的風險放大版。
3. **失敗語意。** 常駐行程掛掉、卡住、或進到壞狀態時，怎麼偵測與重啟？
   量法：故意 kill -STOP server，看 client 幾秒發現。
   路線 A 這題的答案是「timeout 30 秒，然後這一輪走 rule」，簡單到不需要測。
   路線 B 要多一個 supervisor，而**演出當下多一個會壞的東西，就是多一個會壞的東西**。

結論寫在這裡以免下次又要重推：**除非第 1 項量出熱呼叫 < 5 秒且第 2 項在兩次 CLI
升級之間沒有破壞性變更，否則路線 A 留著。** 冷啟動不好看，但它從來不是這場演出會死掉的原因。

---

## 7 · 錄影與 log 對時

SPEC §8：「Movie File Out 錄影與 Director log 同時啟動」。

### TD 端

`td/parspec.py` 上有一個 `Record` Custom Par（toggle，預設 0），
`td/build_network.py` 把 Movie File Out TOP 的 `record` par 綁在
`op('director').par.Record` 上。所以錄影的開關就是 director COMP 上的一個勾。

`tools/preflight.py` 第 7 項先幫你確認 `~/Movies`（或 `--record-dir`）還有 > 5 GB。
錄 1280×1280（TD Non-Commercial 的輸出上限）大約十分鐘就會吃掉這個量級，
所以這個門檻的用途是「讓你在還來得及的時候發現空間不夠」，不是「五 GB 就夠了」。

### 怎麼把畫面跟決策對起來

三份東西要對到同一條時間軸：

| 來源 | 時間欄位 | 基準 |
|---|---|---|
| Movie File Out | 影片本身的時間碼 | 按下 `Record` 那一刻 = 00:00 |
| `--decisions-log`（`DirectorLoop`） | `t`（節目秒）與 `wall`（`YYYY-MM-DDTHH:MM:SS`） | `t` 從 sidecar 開始算 |
| `--log`（`Sidecar` 段落轉換） | 同上 | `t` 從**第一筆 `/feat/energy` 到達**算 |

注意兩個 `t` 的基準**不一樣**：段落 log 的 `t=0` 是音樂真的開始送進來的那一刻
（sidecar 通常比音樂早開，所以這是刻意的），決策 log 的 `t` 走的是 `ScaledClock`。
**要跨檔案對時，用 `wall` 欄位，不要用 `t`。** 兩份 log 的 `wall` 都是本地牆鐘秒，
跟你按下 `Record` 的時刻是同一支時鐘。

實務流程：

1. 先開 sidecar（`--log sections.jsonl --decisions-log decisions.jsonl`），
   確認 `t=+...s  waiting for /feat/energy ...` 已經在跑。
2. 在 TD 勾 `Record`，同時在終端機記下時間（或直接看 sidecar 印的第一行 status 時間）。
3. 演完之後：`decisions.jsonl` 每一行的 `wall` 減掉步驟 2 的時刻，就是影片時間碼。

```bash
# 「第 12 分鐘那個場景是誰決定的、為什麼」
python3 - <<'PY'
import json, datetime
started = datetime.datetime.fromisoformat("2026-09-08T22:03:11")  # 按下 Record 的時刻
for line in open("decisions.jsonl", encoding="utf-8"):
    r = json.loads(line)
    offset = datetime.datetime.fromisoformat(r["wall"]) - started
    d = r["decision"]
    print(f"{offset}  [{r['source']}] {r['section']:<9} {d['scene']}/{d['palette']}  {d['intent']}")
PY
```

`intent`（SPEC §3.2，≤120 字）就是為了這一刻存在的：看著影片，讀導演當時的理由。

---

## 8 · `tools/codex_sessions.py` — session 清理（SPEC §7）

```bash
uv run python tools/codex_sessions.py list                       # 唯讀
uv run python tools/codex_sessions.py list --json
uv run python tools/codex_sessions.py archive --older-than 1d --dry-run
uv run python tools/codex_sessions.py archive --older-than 1d    # 搬，不刪
uv run python tools/codex_sessions.py clean --older-than 7d --yes  # 只刪封存區
```

SPEC §7 把「`~/.codex/sessions` 堆滿」列為風險：一小時 200 次決策就是一小時 200 個
rollout 檔，跟使用者其他所有 Codex 用途混在同一個資料夾裡。
所以難的不是刪除，是**確定哪些是我們的**。

### 兩個指紋，任一個中就算

從每個檔案的前 16 KB 讀：

1. `amv.codex_client.SANDBOX_CWD` 這個路徑字串——`codex exec -C` 指的那個專用空目錄。
   它會出現在 session 的 `session_meta` 記錄裡，而機器上沒有別的東西用這個路徑。
2. `amv.director.SYSTEM_PROMPT` 的第一行——每一個導演 prompt 都有，別人的都沒有。

本機驗證（2026-09-08）：167 個 session 檔裡命中 4 個，
四個的 `session_meta.cwd` 都正好是 `/var/.../T/amv-codex-cwd`、`originator` 都是 `codex_exec`。
**兩個指紋都沒中的檔案，這個工具不列、不搬、不刪。**

### 三個動詞的權力刻意不對等

| 動詞 | 做什麼 | 安全網 |
|---|---|---|
| `list` | 數量、總大小、最舊、最新 | 唯讀 |
| `archive` | **搬**進 `~/.codex/sessions-amv-archive/`，保留 `YYYY/MM/DD` 結構 | 永不刪除，`mv` 就能還原；同名不覆蓋 |
| `clean` | 刪除 | 只刪封存區裡面的、只刪指紋命中的、而且**沒有 `--yes` 就拒絕** |

`--dry-run` 三個動詞都支援，印出「會發生什麼」然後什麼都不做。
`clean` 的路徑檢查是對 resolve 過的絕對路徑做的，所以一個指出封存區外的 symlink
不會變成刪掉現役 session 的方法。

`tools/preflight.py` 第 8 項在檔案數 > 500 時會 ⚠️ 提醒你來跑這個。

### 讀取範圍

這個工具會讀 session 檔，但**只讀前 16 KB，而且只用來測那兩個子字串**。
輸出裡只有路徑、大小、時間，沒有任何 session 內容。
（`~/.codex/auth.json` 從頭到尾沒有被碰過——`tools/check_env.py` 只確認它存在、
讀一個 `auth_mode` 鍵。）

---

## 9 · 上場前 30 分鐘的順序

```bash
# 1. 檢查表（不花額度）
uv run python tools/preflight.py --record-dir /Volumes/SSD/amv
#    → 有 ❌ 就修，⚠️ 逐項確認是你有意跳過的

# 2. 音樂開起來，再跑一次，確認 multi-output routing 這一列變成 ✅
uv run python tools/preflight.py --meter-seconds 10

# 3. session 清一清（上一場的留著沒意義）
uv run python tools/codex_sessions.py archive --older-than 1d

# 4. 額度確認（前一天做過就跳過）
uv run python tools/dry_run.py --minutes 60 --director gpt --report dry_run.md

# 5. 真的花一次額度，確認 codex 這條路今天是通的
uv run python tools/preflight.py --real-codex

# 6. 開 sidecar，兩份 log 都要
uv run python -m amv.sidecar --director gpt \
  --log sections.jsonl --decisions-log decisions.jsonl

# 7. TD 勾 Record，記下時刻
```

第 5 步是唯一會花錢的一步，而且值得：它是當天唯一一次驗證
「auth 還有效、model 還在、flag 沒改、額度還有空間」的機會——
`startup_check(smoke=True)` 做的也是同一件事，只是換一個位置。
