# 外部資料來源

`guard_intent.jsonl` 裡 id 是 `x-` 開頭、有 `source` 欄的句子，取自下面幾個公開資料集。
用 `eval/make_laya_external.py` 抽樣、OpenCC `s2twp` 轉繁體、人工檢查後加入（開發日誌 2026-10-15-01、03）。
其他句子（`t-` 開頭）是專題組自己寫的。

| 來源 | 授權 | 用到的部分 | `source` 欄的寫法 |
|---|---|---|---|
| [thu-coai/Safety-Prompts](https://github.com/thu-coai/Safety-Prompts) | [Apache License 2.0](https://github.com/thu-coai/Safety-Prompts/blob/main/LICENSE) | `instruction_attack_scenarios.json` 的 Goal_Hijacking、Prompt_Leaking、Role_Play_Instruction、Unsafe_Instruction_Topic、Inquiry_With_Unsafe_Opinion | `Safety-Prompts/<類別>#<在該類別的索引>` |
| [STAIR-BUPT/JailBench](https://github.com/STAIR-BUPT/JailBench) | [MIT](https://github.com/STAIR-BUPT/JailBench/blob/main/LICENSE)（Copyright (c) 2024 gitkolento） | `JailBench.csv` 的 `query` | `JailBench#<列號>` |
| LCCC（[thu-coai/CDial-GPT](https://github.com/thu-coai/CDial-GPT)，檔案取自 Hugging Face 鏡像 `silver/lccc`） | MIT（Hugging Face 載入腳本 `thu-coai/lccc` 標示） | `lccc_base_test.jsonl.gz` 每段對話的第一句 | `LCCC-base-test#<行號>` |

相對原始資料的修改：簡體轉繁體（含台灣用詞）、合併多餘空白；LCCC 去掉斷字空格。少數句子的 label 依 `shared/agent.yaml` 的 `intent_classes` 重新判定，和來源的分類不同。

這些句子有一部分含有害、冒犯性的內容（越獄提示、違法請求、粗話），只用來訓練、評估智慧助理第 2 段的防護判斷。
