---
name: gitlab-target-watch
description: 为已绑定主人监控指定 GitLab Issue 或 MR 的新评论与讨论回复，按间隔静默检查，命中后通知并可自动结束。
---

# GitLab 目标监控

主人要求“盯住这个 Issue/MR 的回复”“每 10 分钟检查，有新回复再告诉我”等时，将监控提议写入输出 JSON 的 `target_watch` 数组。每项为 `{"url":"https://gitlab.example.com/group/project/-/issues/75","event":"new_comment","interval_minutes":10,"mode":"once"}`；MR 链接使用 `/-/merge_requests/<编号>`。多目标分别占一项；不创建时输出 `[]`。链接必须是当前已配置 GitLab 主机的完整目标链接。缺少唯一链接时先根据当前话题核实，仍无法确定才请主人补充。

`mode` 为 `once` 或 `continuous`。“首次有回复后通知并取消”使用 `once`；要求一直监控使用 `continuous`。未指定间隔时用 10 分钟；未指定结束条件时按语境选择，单次“有回复就告诉我”默认为 `once`。只监控主人提出请求后的新人工评论，包括 MR 代码行讨论回复；系统自动记录不算回复。服务在创建时核实目标并保存请求开始时的评论基线，之后静默轮询；查询失败保留监控并重试。创建结果由服务确认，在此之前不要声称已启动。

监控由后台服务执行，模型不自行 sleep、轮询或伪装成固定提醒。`/schedule` 查看生效任务及编号，`/schedule <编号>` 看最近状态，`/schedule cancel <编号>` 取消。目标监控只授权向已绑定主人发送通知，不授权修改 GitLab 目标或向其他人发消息。流水线成功后向他人发送已审阅通知仍按 issue-assistant 和 schedule-assistant 的流程处理。

最近检查、下次检查和通知时间按本轮 `TIMEZONE` 展示，默认 `Asia/Shanghai`；存储的时间戳不因展示时区变化重新解释。通知使用当前任务确认的范围，不猜测个人身份或其他接收人。
