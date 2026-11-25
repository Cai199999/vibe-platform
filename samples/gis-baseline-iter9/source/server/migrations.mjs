// 由 DBACoder 生成：按文件名顺序执行 server/migrations/*.sql。
// 生产环境禁止使用 Sequelize sync() 隐式建表；所有结构变更必须落成
// 可审计、可回放的 SQL 迁移，并记录在 _schema_migrations 表中。
import { createHash } from 'node:crypto'
import { readdir, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { ORM_ENABLED, STORAGE, createSequelize } from './db.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
const MIGRATIONS_DIR = process.env.MIGRATIONS_DIR || join(__dirname, 'migrations')
const MIGRATION_TABLE = '_schema_migrations'

const TRACKING_DDL =
  STORAGE === 'postgresql'
    ? `CREATE TABLE IF NOT EXISTS ${MIGRATION_TABLE} (
        id BIGSERIAL PRIMARY KEY,
        filename VARCHAR(255) NOT NULL UNIQUE,
        checksum VARCHAR(64) NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )`
    : `CREATE TABLE IF NOT EXISTS ${MIGRATION_TABLE} (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        filename VARCHAR(255) NOT NULL UNIQUE,
        checksum VARCHAR(64) NOT NULL,
        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
      ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`

// 逐字符切分 SQL，避免把字符串 / 注释里的分号误当语句结束。
function splitSqlStatements(sql) {
  const statements = []
  let current = ''
  let quote = ''
  let lineComment = false
  let blockComment = false
  for (let index = 0; index < sql.length; index += 1) {
    const char = sql[index]
    const next = sql[index + 1]
    if (lineComment) {
      if (char === '\n') lineComment = false
      current += char
      continue
    }
    if (blockComment) {
      current += char
      if (char === '*' && next === '/') {
        current += next
        index += 1
        blockComment = false
      }
      continue
    }
    if (!quote && char === '-' && next === '-') {
      lineComment = true
      current += char + next
      index += 1
      continue
    }
    if (!quote && char === '/' && next === '*') {
      blockComment = true
      current += char + next
      index += 1
      continue
    }
    if (quote) {
      current += char
      if (char === quote) {
        if (sql[index + 1] === quote) {
          current += sql[index + 1]
          index += 1
        } else {
          quote = ''
        }
      } else if (char === '\\') {
        current += next || ''
        index += 1
      }
      continue
    }
    if (char === "'" || char === '"' || char === '`') {
      quote = char
      current += char
      continue
    }
    if (char === ';') {
      if (current.trim()) statements.push(current.trim())
      current = ''
      continue
    }
    current += char
  }
  if (current.trim()) statements.push(current.trim())
  return statements
}

function checksumOf(sql) {
  return createHash('sha256').update(sql, 'utf8').digest('hex')
}

export async function runMigrations() {
  if (!ORM_ENABLED) {
    return {
      storage: 'sqlite',
      skipped: true,
      applied: [],
      reason: 'DATABASE_URL 未配置，生产迁移中止；SQLite 预览库不参与迁移',
    }
  }
  const sequelize = await createSequelize()
  const appliedNow = []
  const skipped = []
  try {
    await sequelize.authenticate()
    await sequelize.query(TRACKING_DDL)
    const files = (await readdir(MIGRATIONS_DIR))
      .filter((name) => name.endsWith('.sql'))
      .sort()
    if (files.length === 0) {
      throw new Error(`未找到迁移文件: ${MIGRATIONS_DIR}`)
    }
    const [rows] = await sequelize.query(
      `SELECT filename, checksum FROM ${MIGRATION_TABLE}`,
    )
    const applied = new Map(
      (rows || []).map((row) => [String(row.filename), String(row.checksum)]),
    )
    for (const filename of files) {
      const sql = await readFile(join(MIGRATIONS_DIR, filename), 'utf8')
      const checksum = checksumOf(sql)
      if (applied.has(filename)) {
        if (applied.get(filename) !== checksum) {
          throw new Error(
            `迁移 ${filename} 已执行但内容被修改；` +
              '生产环境禁止改写历史迁移，请新增下一版迁移文件',
          )
        }
        skipped.push(filename)
        continue
      }
      const statements = splitSqlStatements(sql)
      if (statements.length === 0) {
        throw new Error(`迁移文件为空或无法解析: ${filename}`)
      }
      await sequelize.transaction(async (transaction) => {
        for (const statement of statements) {
          await sequelize.query(statement, { transaction })
        }
        await sequelize.query(
          `INSERT INTO ${MIGRATION_TABLE} (filename, checksum) VALUES (?, ?)`,
          { transaction, replacements: [filename, checksum] },
        )
      })
      appliedNow.push(filename)
      console.log(`[fullstack-db] applied ${filename}`)
    }
    return {
      storage: STORAGE,
      skipped: false,
      applied: appliedNow,
      alreadyApplied: skipped,
    }
  } finally {
    await sequelize.close()
  }
}

export { splitSqlStatements }
