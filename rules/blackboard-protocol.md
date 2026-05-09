# Blackboard 协同系统 v4 — 注册中心

> MCP是注册中心。每个session启动时**必须**先注册，否则其他session看不到你。

## ⚠️ 启动时必须执行（不可跳过）

1. `bb_register(name, task, session_id)` — 注册当前session，**task必须写清楚你在做什么**（如"修复登录失败bug"），不能留空。**session_id必须用hook注入的UUID**，否则会创建重复session。
2. `bb_get_recent_knowledge(48)` — 获取最近知识

**不注册 = 不存在。** 其他session无法感知你，文件冲突检测、编译锁都不会保护你。

## 编辑文件前

- `bb_check_conflicts(file_paths)` — 检查冲突
- 冲突时换文件或等对方释放
- 编辑后 `bb_claim_file(session_id, file_path)` — 声明占用

## 编译纪律

- 编译前 `bb_acquire_build_lock(session_id, project_dir)` — 获取锁
- 编译后 `bb_release_build_lock(session_id, project_dir)` — 释放锁
- 同一项目只允许1个session编译

## 知识闭环

- 修bug踩坑后：`bb_report_bug_pattern(session_id, pattern, root_cause, fix)`
- 做关键决策：`bb_share_decision(session_id, decision, rationale)`
- 发现经验：`bb_share_knowledge(session_id, fingerprint, category, text)`
- 开始任务前：`bb_search_knowledge(query)` — 先搜再做

## MCP工具速查

| 工具 | 用途 |
|------|------|
| `bb_register` | 注册session（启动时必须调用） |
| `bb_deregister` | 注销session |
| `bb_heartbeat` | 刷新心跳 |
| `bb_claim_file` | 声明文件占用 |
| `bb_release_file` | 释放文件 |
| `bb_check_conflicts` | 查询文件冲突 |
| `bb_acquire_build_lock` | 获取编译锁 |
| `bb_release_build_lock` | 释放编译锁 |
| `bb_status` | 查看完整状态 |
| `bb_session_files` | 列出占用文件 |
| `bb_share_knowledge` | 共享知识 |
| `bb_search_knowledge` | 搜索知识 |
| `bb_validate_knowledge` | 验证知识 |
| `bb_get_recent_knowledge` | 获取最近知识 |
| `bb_share_decision` | 记录决策 |
| `bb_report_bug_pattern` | 报告Bug模式 |
| `bb_event` | 记录事件 |
