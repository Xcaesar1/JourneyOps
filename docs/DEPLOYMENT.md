# Deployment And Rollback

本文件描述阶段 2 多服务架构。生产环境在明确批准前不得执行本阶段部署；当前部署目标是
Oracle staging，使用独立端口和独立 named volumes。

## Staging Deploy

```bash
cd /opt/tripstar/JourneyOps-staging
git status --short --branch
git pull --ff-only origin staging
cp -n .env.staging.example .env.staging
chmod 0600 .env.staging
```

只在未跟踪的 `.env.staging` 中填写 Secret。`POSTGRES_PASSWORD` 使用随机、URL-safe 字符；
不得打印该文件。

```bash
docker compose \
  --env-file .env.staging \
  -f docker-compose.yaml \
  -f docker-compose.staging.yaml \
  config --quiet
docker compose \
  --env-file .env.staging \
  -f docker-compose.yaml \
  -f docker-compose.staging.yaml \
  up -d --build
docker compose \
  --env-file .env.staging \
  -f docker-compose.yaml \
  -f docker-compose.staging.yaml \
  ps
curl --fail --silent http://127.0.0.1:17861/health/ready
```

`migrate` 必须以 `0` 退出，`postgres`、`redis`、`worker` 和 `trip-planner` 必须为 healthy。

## Database Backup

PostgreSQL 是任务事实源。部署迁移和回滚前创建逻辑备份：

```bash
set -euo pipefail
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/var/backups/tripstar/$STAMP
sudo install -d -m 0700 "$BACKUP"
docker exec journeyops-postgres-staging \
  pg_dump -U journeyops -d journeyops -Fc \
  | sudo tee "$BACKUP/journeyops-staging.pgdump" >/dev/null
sudo chmod 0600 "$BACKUP/journeyops-staging.pgdump"
sudo sha256sum "$BACKUP/journeyops-staging.pgdump" \
  | sudo tee "$BACKUP/journeyops-staging.pgdump.sha256" >/dev/null
sudo chmod 0600 "$BACKUP/journeyops-staging.pgdump.sha256"
sudo sha256sum -c "$BACKUP/journeyops-staging.pgdump.sha256"
```

Redis 不保存任务真相。通常不恢复 Redis volume；Worker 启动扫描负责处理未投递或陈旧任务。

## Restore Drill

恢复会覆盖目标数据库，只能在新的 staging 演练栈中执行，并需先确认目标容器名和卷名。

```bash
set -euo pipefail
test "$TARGET_POSTGRES_CONTAINER" = journeyops-postgres-restore-drill
docker exec "$TARGET_POSTGRES_CONTAINER" \
  dropdb -U journeyops --if-exists journeyops
docker exec "$TARGET_POSTGRES_CONTAINER" \
  createdb -U journeyops journeyops
sudo cat /var/backups/tripstar/<STAMP>/journeyops-staging.pgdump \
  | docker exec -i "$TARGET_POSTGRES_CONTAINER" \
  pg_restore -U journeyops -d journeyops --clean --if-exists \
    --no-owner --no-privileges
```

恢复后执行 `alembic current`、三表计数、`/health/ready` 和一个脱敏测试任务。禁止把恢复
演练指向生产容器或生产 volume。

## Code Rollback

阶段 2 使用独立 Git commit。优先创建审计可见的 revert，不改写历史：

```bash
git status --short --branch
git revert <PHASE_2_COMMIT>
docker compose \
  --env-file .env.staging \
  -f docker-compose.yaml \
  -f docker-compose.staging.yaml \
  up -d --build
```

代码回滚不会自动删除 PostgreSQL/Redis volumes。只回滚应用时保留数据卷；确认备份可恢复且
明确不再需要阶段 2 数据后，才可人工执行 Alembic downgrade。不得把 `docker compose down -v`
作为常规回滚命令。

## Migration Rollback

首次迁移只创建三张新表，对旧 JSON 数据无写入。若必须回退 schema：

```bash
docker compose \
  --env-file .env.staging \
  -f docker-compose.yaml \
  -f docker-compose.staging.yaml \
  stop trip-planner worker
docker compose \
  --env-file .env.staging \
  -f docker-compose.yaml \
  -f docker-compose.staging.yaml \
  run --rm migrate alembic -c backend/alembic.ini downgrade base
```

该命令会删除 `trips`、`trip_tasks`、`trip_versions`，属于破坏性操作；执行前必须有已校验
`pg_dump`、维护窗口和人工批准。

## Production Promotion Gate

阶段 2 不读取或迁移 `backend/data/trip_tasks/*.json`。正式切换生产前必须先盘点旧 JSON，制定
可重复执行且已在 staging 验证的数据导入方案，并核对任务数、终态数和历史结果。该迁移未完成前，
不得将阶段 2 栈提升为 production，也不得删除旧 JSON volume。

## Executed Backup Evidence

- 2026-08-07 Oracle staging PostgreSQL backup: `/var/backups/tripstar/20260807T115742Z`
- Format: PostgreSQL custom dump plus SHA-256 manifest
- Permissions: backup directory `0700`, files `0600`
- Verification: `sha256sum -c` passed
