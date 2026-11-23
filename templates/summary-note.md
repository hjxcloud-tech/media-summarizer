---
title: "{{title}}"
source: "{{url}}"
uploader: "{{uploader}}"
publish_date: {{publish_date}}
duration: "{{duration}}"
date_summarized: {{date}}
transcript_type: "{{transcript_type}}"
tags:
  - bilibili
  - video-summary
  - {{category}}
---

# {{title}}

> **来源**: [B站视频]({{url}})
> **上传者**: {{uploader}}
> **发布日期**: {{publish_date}}
> **时长**: {{duration}}
> **总结日期**: {{date}}
> **转录方式**: {{transcript_type}}

## 核心摘要

{{summary}}

## 关键客观数据

视频中的硬数据汇总为表格（指标/数值/口径备注），不含观点判断；存在口径差异时（如归母 vs 扣非、整体 vs 分部毛利率、静态/TTM/前瞻 PE）必须并列标注。

| 类别 | 指标 | 数值 | 口径/备注 |
|------|------|------|-----------|
| {{data_row_1}} |
| {{data_row_2}} |

## 关键观点

{{key_points}}

## 作者分析过程

作者的分析框架与推理链：按什么顺序拆解（如"业绩→护城河→现金流→资产负债表→估值→资本动作"）、每步的论证逻辑（"因为 A 所以 B"）、多方/空方如何交锋与让步。纯口播观点类视频若无明确框架可标注"无明确分析框架"。

{{analysis_process}}

## 内容时间线

| 时间段 | 主题 | 关键信息 |
|--------|------|----------|
{{timeline}}

## 金句摘录

{{quotes}}

## 💬 社区讨论

### 核心议题

{{discussion_topics}}

### 共识与分歧

{{consensus_divergence}}

### UP 主互动

{{uploader_interaction}}

### 情绪基调

{{community_sentiment}}

## 个人备注

{{notes}}

## 📄 字幕原文（校对整理版）

> 原文经校对、整理（合并零碎句、规范标点分段、修正明显误识别）；不单独生成原稿文件。
> 画面关键帧（若有，来自管线 `keyframes`）插入本节**时间上对应的时间段**：`![[<视频ID>_<HHMMSS>.png]]` + `*画面 @mm:ss：…*`（图片原件在 vault `attachments/`）。
> 基于 STT 时注明：基于语音转录（Whisper xx 模型），术语可能存在误差。

{{proofread_transcript}}
