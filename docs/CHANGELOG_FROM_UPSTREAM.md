# Changelog From Upstream

本文件记录 JourneyOps 相对 `1sdv/TripStar` 的二次开发差异，不替代 Git 历史。

## Phase 2 - 2026-08-07

- 增加 PostgreSQL、Redis、Celery 多服务运行架构。
- 增加 SQLAlchemy 2 模型和 Alembic 初始迁移。
- 将 legacy 与 V2 旅行规划任务改为数据库事实源。
- 将 legacy Planner 放入 Celery Worker 执行，未修改 Planner 文件。
- 增加 Redis Pub/Sub WebSocket、取消、重试、幂等和 Worker 恢复策略。
- 增加独立 staging volumes、基础设施 healthchecks 和真实依赖 CI。

GPL-2.0 License 和上游归属保持不变。
