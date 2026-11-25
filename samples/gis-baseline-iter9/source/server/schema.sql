-- 由 DBA Agent 根据《全栈实施契约》生成。
-- contract: ctr_83e7700b9b5344f5a175
-- table: facilities
-- 目标数据库: mysql（生产通过 DATABASE_URL 连接）
-- 说明：本文件是沙盒 SQLite 的预览建表语句；
-- 生产结构变更必须使用 server/migrations/*.sql，并由
-- `node server/migrate.mjs` 显式执行，禁止 Sequelize sync() 自动建表。

CREATE TABLE IF NOT EXISTS "facilities" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "name" TEXT,
  "type" TEXT,
  "status" TEXT,
  "lng" REAL,
  "lat" REAL,
  "location" TEXT,
  "created_by" INTEGER,
  "updated_by" INTEGER,
  "created_at" TEXT,
  "updated_at" TEXT
);
CREATE TABLE IF NOT EXISTS "users" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "username" TEXT,
  "password_hash" TEXT,
  "role" TEXT,
  "created_at" TEXT,
  "updated_at" TEXT
);
CREATE TABLE IF NOT EXISTS "facility_images" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "facility_id" INTEGER,
  "url" TEXT,
  "filename" TEXT,
  "mime_type" TEXT,
  "size_bytes" INTEGER,
  "created_by" INTEGER,
  "created_at" TEXT
);
