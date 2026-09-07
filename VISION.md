# Architecture Vision / Future Roadmap（V2–V3）

> 本文件是「終局長什麼樣」的願景，不是施工圖。施工圖是 [SPEC.md](SPEC.md)。
> 來源：ChatGPT 版藍圖（2026-09）與 Leo 的雙版評比。

## 評比結論

| 面向 | ChatGPT 版 | Claude 版 | 勝 |
|---|---:|---:|---|
| 大方向／願景 | **9.5** | 9 | ChatGPT |
| 架構清楚度 | 9 | **9.5** | Claude |
| MVP 可落地 | 8 | **9.5** | Claude |
| Agent 架構 | 9 | **9.5** | Claude |
| 即時系統思維 | 9 | **10** | Claude |
| 故障處理 | 6.5 | **10** | Claude |
| 驗收標準 | 6 | **10** | Claude |
| projectM 實作務實度 | 7 | **9.5** | Claude |
| 長期終極架構 | **9.5** | 8.5 | ChatGPT |
| 丟給 Coding Agent | 8 | **9.5** | Claude |

總分：Claude 9.4／10，ChatGPT 8.6／10。

**判決：Claude 版當 V1 主規格；ChatGPT 版降級為 Architecture Vision / Future Roadmap。**
要今天開工：Claude 贏。要描述終極產品：ChatGPT 贏。

## Roadmap

```
V1  Claude 工程版           → 穩定、能跑、能演（SPEC.md）
V2  Scene Engine            → 更豐富的視覺語言
V3  projectM 深度整合 + Performance Memory + Visual Personality
                            → 真正的 Agentic Music Visualizer
```

## V1 採用（來自 Claude 版）
BlackHole、TD 60 fps reflex layer、Python Director sidecar、OSC、GPT 慢速決策、`on_drop`、rule fallback、gpt/rule/manual 三模式、scene history、acceptance criteria、projectM sidechain（Syphon/NDI）。

## V2：Scene Engine（來自 ChatGPT 版）
- Native TD Visual Engine 與 projectM Engine 分層
- Scene / Layer Engine：Scene Families、更完整的 Scene Composition
- AI Art Director 的高階 prompt：
  > 根據音樂的 energy、頻譜、BPM 與過去 2 分鐘視覺歷史，自主管理一場完整 Psytrance 視覺表演。不要重複相同構圖超過 60 秒。Breakdown 時降低視覺複雜度。Drop 前逐漸建立 tension。Drop 發生時切換場景，但避免每個 drop 都使用同一策略。

## V3：終局
- **projectM 深度整合**：libprojectM 作為 library 嵌入，MilkDrop render 直接成為 TD texture（V1 刻意不做：macOS TD 走 Metal，libprojectM 要 OpenGL context）
- **Performance Memory**：跨場次記住哪些決策有效
- **Visual Personality**：個人化 AI VJ identity
- 常駐 `codex app-server`，把 13 s 決策延遲壓到數秒

## ChatGPT 版終極架構圖（保留原貌）

```
                        Apple Music
                             ↓
                         BlackHole
                             ↓
                      TouchDesigner
                     ↙             ↘
                  FFT             projectM
                   ↓                 ↓
             Native TD Visual    MilkDrop
                   ↓                 ↓
                   └──────┬──────────┘
                          ↓
                       Composite
                          ↑
                       GPT-6
                          ↓
                    AI Art Director
```

projectM 當「迷幻素材生成器」，TouchDesigner 當「宇宙引擎」，GPT-6 當「導演」。
