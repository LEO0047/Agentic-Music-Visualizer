# AMV dry run — SPEC §2 額度與反重複驗收

- 產生時間：2026-09-08T02:10:14
- 指令：`dry_run.py --minutes 3 --director gpt --report /tmp/amv_dry_real.md`
- 模式：`--director gpt`，period 18.0 s，speed ×1.0
- 節目長度：3.0 分鐘（實際牆鐘 3.0 分鐘）
- 特徵來源：tools/fake_td.py（重播 2 次）
- codex 二進位：`(預設 find_codex)`
- 決策 log：`/var/folders/s5/vxn2m0rj53z_0k16gcx4qdnm0000gn/T/amv-dry-run-1pkfdn0w/decisions.jsonl`

## 決策

| 項目 | 值 |
|---|---|
| decisions | 10 |
| by_source | {"gpt": 10} |
| gpt 佔比 | 100% |
| section 分布 | {"steady": 6, "build": 2, "drop": 2} |

## 延遲（實秒，不受 --speed 影響）

| 項目 | 秒 |
|---|---|
| mean | 11.22 |
| p50 | 11.40 |
| p95 | 11.67 |
| max | 11.79 |

## 節奏（節目時間）

| 項目 | 值 |
|---|---|
| 決策間隔 min / mean / max | 12.5 / 17.4 / 18.7 s |
| 最長沒換 scene | 1 次決策 / 0 s |
| 60 s 內 scene+palette 重複 | 0  ✅ |

## Codex 額度推估（SPEC §2）

每次決策以 **25,000 tokens** 估（2026-09-08 實測 25,192，大多是 Codex 系統提示）。

| 項目 | 值 |
|---|---|
| decisions / hour | 200 |
| tokens / hour | 5,000,000 |
| tokens / 5 小時窗 | 25,000,000 |

> **SPEC §2 警告**：用量計入 ChatGPT 訂閱的 Codex 額度，而額度是以 **5 小時滾動窗**與**每週窗**結算的。以這個節奏連跑 5 小時就是 25.0M tokens，很可能在演出中途撞窗。
>
> 對策照 SPEC §7：規則式 fallback 常駐（本次全部走 gpt，fallback 沒被觸發——`tools/preflight.py` 的 fallback drill 才是它的驗收），並在 set 中隨時把 `--period` 從 18 s 拉到 30 s——那會把每小時的量降到約 3,000,000 tokens。

## 判讀

- ✅ 60 s 內沒有重複的 scene+palette。
