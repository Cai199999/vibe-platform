// 由 DBACoder 生成：显式执行数据库迁移。
// 生产：node server/migrate.mjs（需要 DATABASE_URL）。
// 迁移失败立刻以非 0 退出码终止发布，绝不让服务带着旧表结构上线。
import { runMigrations } from './migrations.mjs'

try {
  const result = await runMigrations()
  if (result.skipped) {
    console.error('[fullstack-db] 迁移中止：', result.reason)
    process.exit(1)
  }
  console.log('[fullstack-db] migrate', result)
  process.exit(0)
} catch (error) {
  console.error('[fullstack-db] 迁移失败：', error)
  process.exit(1)
}
