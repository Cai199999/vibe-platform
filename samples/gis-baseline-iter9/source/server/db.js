// 由 DBACoder 根据《全栈实施契约》生成。
import { QueryTypes } from 'sequelize'
// 生产路径：Sequelize ORM + DATABASE_URL（MySQL / PostgreSQL）。
// 沙盒预览：未配置 DATABASE_URL 时回退到 Node 22 内置 node:sqlite。
// 两条路径暴露同一组仓储函数，Express 路由与前端代码无需分支。
// node:sqlite 采用动态 import：生产配置 DATABASE_URL 时不会加载实验性模块。
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const TABLE = 'facilities'
const ENTITY = '登录表单'
const DB_ENGINE = (process.env.DB_ENGINE || 'mysql').toLowerCase()
const COLUMN_DEFS = [
  [
    "id",
    "INTEGER"
  ],
  [
    "name",
    "TEXT"
  ],
  [
    "type",
    "TEXT"
  ],
  [
    "status",
    "TEXT"
  ],
  [
    "lng",
    "REAL"
  ],
  [
    "lat",
    "REAL"
  ],
  [
    "location",
    "TEXT"
  ],
  [
    "created_by",
    "INTEGER"
  ],
  [
    "updated_by",
    "INTEGER"
  ],
  [
    "created_at",
    "TEXT"
  ],
  [
    "updated_at",
    "TEXT"
  ]
]
// DDL 约束元数据：NOT NULL / DEFAULT / UNIQUE / 主键。
// 只生成字段名不够——缺少 NOT NULL 列的默认值，生产 MySQL 会直接 500。
const COLUMN_META = {
  "id": {
    "type": "BIGINT",
    "sqliteType": "INTEGER",
    "nullable": false,
    "hasDefault": false,
    "primary": true,
    "unique": false,
    "autoIncrement": true
  },
  "name": {
    "type": "VARCHAR",
    "sqliteType": "TEXT",
    "nullable": false,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "type": {
    "type": "VARCHAR",
    "sqliteType": "TEXT",
    "nullable": false,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "status": {
    "type": "VARCHAR",
    "sqliteType": "TEXT",
    "nullable": false,
    "hasDefault": true,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "lng": {
    "type": "DECIMAL",
    "sqliteType": "REAL",
    "nullable": false,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "lat": {
    "type": "DECIMAL",
    "sqliteType": "REAL",
    "nullable": false,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "location": {
    "type": "POINT",
    "sqliteType": "TEXT",
    "nullable": false,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "created_by": {
    "type": "BIGINT",
    "sqliteType": "INTEGER",
    "nullable": true,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "updated_by": {
    "type": "BIGINT",
    "sqliteType": "INTEGER",
    "nullable": true,
    "hasDefault": false,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "created_at": {
    "type": "DATETIME",
    "sqliteType": "TEXT",
    "nullable": false,
    "hasDefault": true,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  },
  "updated_at": {
    "type": "DATETIME",
    "sqliteType": "TEXT",
    "nullable": false,
    "hasDefault": true,
    "primary": false,
    "unique": false,
    "autoIncrement": false
  }
}
// 主从表：RELATED_MODELS 只包含一跳关联表（已剔除鉴权敏感列），
// RELATIONS 描述 hasMany / belongsTo 的方向与外键。
const RELATED_MODELS = [
  {
    "table": "users",
    "entity": "登录表单",
    "isCore": false,
    "columns": [
      "id",
      "username",
      "role",
      "created_at",
      "updated_at"
    ],
    "columnDefs": [
      [
        "id",
        "INTEGER"
      ],
      [
        "username",
        "TEXT"
      ],
      [
        "role",
        "TEXT"
      ],
      [
        "created_at",
        "TEXT"
      ],
      [
        "updated_at",
        "TEXT"
      ]
    ],
    "columnMeta": {
      "id": {
        "type": "BIGINT",
        "sqliteType": "INTEGER",
        "nullable": false,
        "hasDefault": false,
        "primary": true,
        "unique": false,
        "autoIncrement": true
      },
      "username": {
        "type": "VARCHAR",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "role": {
        "type": "ENUM",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "created_at": {
        "type": "DATETIME",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "updated_at": {
        "type": "DATETIME",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      }
    },
    "displayKey": "username",
    "payloadKey": "username"
  },
  {
    "table": "facility_images",
    "entity": "登录表单",
    "isCore": false,
    "columns": [
      "id",
      "facility_id",
      "url",
      "filename",
      "mime_type",
      "size_bytes",
      "created_by",
      "created_at"
    ],
    "columnDefs": [
      [
        "id",
        "INTEGER"
      ],
      [
        "facility_id",
        "INTEGER"
      ],
      [
        "url",
        "TEXT"
      ],
      [
        "filename",
        "TEXT"
      ],
      [
        "mime_type",
        "TEXT"
      ],
      [
        "size_bytes",
        "INTEGER"
      ],
      [
        "created_by",
        "INTEGER"
      ],
      [
        "created_at",
        "TEXT"
      ]
    ],
    "columnMeta": {
      "id": {
        "type": "BIGINT",
        "sqliteType": "INTEGER",
        "nullable": false,
        "hasDefault": false,
        "primary": true,
        "unique": false,
        "autoIncrement": true
      },
      "facility_id": {
        "type": "BIGINT",
        "sqliteType": "INTEGER",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "url": {
        "type": "VARCHAR",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "filename": {
        "type": "VARCHAR",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "mime_type": {
        "type": "VARCHAR",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "size_bytes": {
        "type": "INT",
        "sqliteType": "INTEGER",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "created_by": {
        "type": "BIGINT",
        "sqliteType": "INTEGER",
        "nullable": true,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "created_at": {
        "type": "DATETIME",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      }
    },
    "displayKey": "facility_id",
    "payloadKey": "facility_id"
  }
]
// 与核心表无直接关系的独立集合（如 patrol_routes）：只建模，不生成 include。
const EXTRA_MODELS = [
  {
    "table": "patrol_routes",
    "entity": "登录表单",
    "isCore": false,
    "columns": [
      "id",
      "name",
      "path_geojson",
      "status",
      "created_by",
      "created_at",
      "updated_at"
    ],
    "columnDefs": [
      [
        "id",
        "INTEGER"
      ],
      [
        "name",
        "TEXT"
      ],
      [
        "path_geojson",
        "TEXT"
      ],
      [
        "status",
        "TEXT"
      ],
      [
        "created_by",
        "INTEGER"
      ],
      [
        "created_at",
        "TEXT"
      ],
      [
        "updated_at",
        "TEXT"
      ]
    ],
    "columnMeta": {
      "id": {
        "type": "BIGINT",
        "sqliteType": "INTEGER",
        "nullable": false,
        "hasDefault": false,
        "primary": true,
        "unique": false,
        "autoIncrement": true
      },
      "name": {
        "type": "VARCHAR",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "path_geojson": {
        "type": "JSON",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "status": {
        "type": "VARCHAR",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "created_by": {
        "type": "BIGINT",
        "sqliteType": "INTEGER",
        "nullable": true,
        "hasDefault": false,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "created_at": {
        "type": "DATETIME",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      },
      "updated_at": {
        "type": "DATETIME",
        "sqliteType": "TEXT",
        "nullable": false,
        "hasDefault": true,
        "primary": false,
        "unique": false,
        "autoIncrement": false
      }
    },
    "displayKey": "name",
    "payloadKey": "name"
  }
]
const RELATIONS = [
  {
    "type": "one_to_many",
    "parentTable": "users",
    "childTable": "facilities",
    "foreignKey": "created_by",
    "parentKey": "id",
    "onDelete": "CASCADE",
    "childAlias": "facilities",
    "parentAlias": "createdFacilities"
  },
  {
    "type": "one_to_many",
    "parentTable": "facilities",
    "childTable": "facility_images",
    "foreignKey": "facility_id",
    "parentKey": "id",
    "onDelete": "CASCADE",
    "childAlias": "images",
    "parentAlias": "facility"
  }
]
const MODEL_DEFS = [
  {
    table: TABLE,
    entity: ENTITY,
    columns: COLUMN_DEFS.map(([name]) => name),
    columnDefs: COLUMN_DEFS,
    columnMeta: COLUMN_META,
    displayKey: 'name',
    payloadKey: 'name',
    isCore: true,
  },
  ...RELATED_MODELS.map((model) => ({ ...model, isCore: false })),
  ...EXTRA_MODELS.map((model) => ({ ...model, isCore: false })),
]
const MODEL_BY_TABLE = Object.fromEntries(
  MODEL_DEFS.map((model) => [model.table, model]),
)
// 前端表单统一使用 name 输入框；真正落库字段由 DDL 决定。
const DISPLAY_KEY = 'name'
const PAYLOAD_KEY = 'name'
const SEED_ROWS = [
  {
    "id": 1,
    "name": "示例登录表单 1",
    "type": "facilities_type_1",
    "status": "ACTIVE",
    "lng": "facilities_lng_1",
    "lat": "facilities_lat_1",
    "location": "facilities_location_1",
    "created_by": "facilities_created_by_1",
    "updated_by": "facilities_updated_by_1",
    "created_at": "2026-11-09T10:01:00.000Z",
    "updated_at": "2026-11-09T10:01:00.000Z"
  },
  {
    "id": 2,
    "name": "示例登录表单 2",
    "type": "facilities_type_2",
    "status": "ACTIVE",
    "lng": "facilities_lng_2",
    "lat": "facilities_lat_2",
    "location": "facilities_location_2",
    "created_by": "facilities_created_by_2",
    "updated_by": "facilities_updated_by_2",
    "created_at": "2026-11-09T10:02:00.000Z",
    "updated_at": "2026-11-09T10:02:00.000Z"
  },
  {
    "id": 3,
    "name": "示例登录表单 3",
    "type": "facilities_type_3",
    "status": "PENDING",
    "lng": "facilities_lng_3",
    "lat": "facilities_lat_3",
    "location": "facilities_location_3",
    "created_by": "facilities_created_by_3",
    "updated_by": "facilities_updated_by_3",
    "created_at": "2026-11-09T10:03:00.000Z",
    "updated_at": "2026-11-09T10:03:00.000Z"
  }
]

// DATABASE_URL 是生产部署的唯一开关：存在时使用真实 ORM，缺失时走 SQLite。
const DATABASE_URL = (process.env.DATABASE_URL || '').trim()
const ORM_ENABLED = DATABASE_URL.length > 0
const STORAGE = ORM_ENABLED ? normalizeEngine(DB_ENGINE) : 'sqlite'
const RUNTIME = ORM_ENABLED
  ? `express-sequelize-${STORAGE}`
  : 'express-sqlite'

function normalizeEngine(value) {
  const engine = String(value || '').toLowerCase()
  if (engine === 'postgres' || engine === 'postgresql') return 'postgresql'
  if (engine === 'mysql' || engine === 'mariadb') return 'mysql'
  if (engine === 'sqlite') return 'sqlite'
  return engine || 'mysql'
}

// SQLite 数据库文件放在 /app/data，随容器文件系统持久化。
const DATA_DIR = process.env.FULLSTACK_DATA_DIR || resolve(__dirname, '..', 'data')
const DB_PATH = process.env.FULLSTACK_DB_PATH || resolve(DATA_DIR, 'fullstack.sqlite')

function schemaSqlFor(model) {
  const columnDdl = model.columnDefs
    .filter(([name]) => name !== 'id')
    .map(([name, type]) => `  "${name}" ${type}`)
    .join(',\n')
  return `CREATE TABLE IF NOT EXISTS "${model.table}" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT${columnDdl ? ',\n' + columnDdl : ''}
);`
}

// 预览库必须一次建好核心表与关联表；逐表执行也保证幂等。
const SCHEMA_SQL = MODEL_DEFS.map(schemaSqlFor).join('\n')
// 鉴权表必须保留 password / iam_sub 等敏感列供登录校验使用；
// 该建表语句先执行，随后 SCHEMA_SQL 的 IF NOT EXISTS 不会覆盖它。
const AUTH_ENABLED = true
const AUTH_TABLE = 'users'
const AUTH_MODEL = {
  "table": "users",
  "entity": "AuthUser",
  "isCore": false,
  "columns": [
    "id",
    "username",
    "password_hash",
    "role",
    "created_at",
    "updated_at"
  ],
  "columnDefs": [
    [
      "id",
      "INTEGER"
    ],
    [
      "username",
      "TEXT"
    ],
    [
      "password_hash",
      "TEXT"
    ],
    [
      "role",
      "TEXT"
    ],
    [
      "created_at",
      "TEXT"
    ],
    [
      "updated_at",
      "TEXT"
    ]
  ],
  "columnMeta": {
    "id": {
      "type": "BIGINT",
      "sqliteType": "INTEGER",
      "nullable": false,
      "hasDefault": false,
      "primary": true,
      "unique": false,
      "autoIncrement": true
    },
    "username": {
      "type": "VARCHAR",
      "sqliteType": "TEXT",
      "nullable": false,
      "hasDefault": false,
      "primary": false,
      "unique": false,
      "autoIncrement": false
    },
    "password_hash": {
      "type": "VARCHAR",
      "sqliteType": "TEXT",
      "nullable": false,
      "hasDefault": false,
      "primary": false,
      "unique": false,
      "autoIncrement": false
    },
    "role": {
      "type": "ENUM",
      "sqliteType": "TEXT",
      "nullable": false,
      "hasDefault": true,
      "primary": false,
      "unique": false,
      "autoIncrement": false
    },
    "created_at": {
      "type": "DATETIME",
      "sqliteType": "TEXT",
      "nullable": false,
      "hasDefault": true,
      "primary": false,
      "unique": false,
      "autoIncrement": false
    },
    "updated_at": {
      "type": "DATETIME",
      "sqliteType": "TEXT",
      "nullable": false,
      "hasDefault": true,
      "primary": false,
      "unique": false,
      "autoIncrement": false
    }
  },
  "displayKey": "username",
  "payloadKey": "username"
}
const AUTH_ID_COLUMN = 'id'
const AUTH_USERNAME_COLUMN = 'username'
const AUTH_PASSWORD_COLUMN = 'password_hash'
const AUTH_ROLE_COLUMN = 'role'
const AUTH_SCHEMA_SQL =
  AUTH_ENABLED && AUTH_MODEL
    ? `CREATE TABLE IF NOT EXISTS "${AUTH_TABLE}" (\n` +
      '  "id" INTEGER PRIMARY KEY AUTOINCREMENT' +
      AUTH_MODEL.columnDefs
        .filter(([name]) => name !== 'id')
        .map(([name, type]) => `,\n  "${name}" ${type}`)
        .join('') +
      '\n);'
    : ''
const ALL_SCHEMA_SQL = [AUTH_SCHEMA_SQL, SCHEMA_SQL].filter(Boolean).join('\n')

// ------------------------------------------------------------
// 公共写入值构造：创建 / 更新共用默认值策略
// ------------------------------------------------------------
// 为 NOT NULL 且无数据库默认值的列生成确定性默认值。
// 这是 Unknown column 之后的第二道生产 500 门禁：
// 例如 resumes.content 是 NOT NULL，旧模板只映射 name，必然写入失败。
function modelFor(table = TABLE) {
  return MODEL_BY_TABLE[table] || MODEL_DEFS[0]
}

function writableColumnsFor(model) {
  return model.columns.filter(
    (name) => !['id', 'created_at', 'updated_at', 'deleted_at'].includes(name),
  )
}

function requiredColumnsFor(model) {
  return Object.keys(model.columnMeta).filter((name) => {
    const meta = model.columnMeta[name] || {}
    return !meta.primary && !meta.autoIncrement && !meta.nullable && !meta.hasDefault
  })
}

function uniqueColumnsFor(model) {
  return Object.keys(model.columnMeta).filter(
    (name) => Boolean(model.columnMeta[name]?.unique),
  )
}

function defaultForColumn(column, model) {
  const meta = model.columnMeta[column] || {}
  if (column === 'tenant_id') return 'tenant_local'
  if (uniqueColumnsFor(model).includes(column)) {
    return `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  }
  if (/(^|_)id$/.test(column)) return 1
  if (/(amount|price|total|count|qty|size|score)$/i.test(column)) return 0
  if (/(^|_)(at|date)$/i.test(column) || /date|time/i.test(meta.type || '')) {
    return new Date().toISOString()
  }
  if (/json|payload|metadata/i.test(column)) return {}
  if (/status|state/i.test(column)) return 'ACTIVE'
  if (meta.sqliteType === 'INTEGER') return 1
  if (meta.sqliteType === 'REAL') return 0
  return `示例${model.entity || model.table}`
}

function buildValues(input = {}, { update = false, table = TABLE } = {}) {
  const model = modelFor(table)
  const body = { ...input }
  // 前端表单沿用 name/title；这里映射到 DDL 中真实的展示列。
  // 绝不把 name 当成固定列：resumes 的核心字段是 file_name。
  if (model.payloadKey && body[model.payloadKey] === undefined) {
    body[model.payloadKey] = body.name ?? body.title
  }
  const values = {}
  for (const column of writableColumnsFor(model)) {
    if (body[column] !== undefined) values[column] = body[column]
  }
  if (update) {
    if (model.columns.includes('updated_at')) {
      values.updated_at = new Date().toISOString()
    }
    return values
  }
  // NOT NULL 且无 DEFAULT 的列必须补齐，否则 MySQL 返回 500。
  for (const column of requiredColumnsFor(model)) {
    if (values[column] === undefined) {
      values[column] = defaultForColumn(column, model)
    }
  }
  if (model.columns.includes('tenant_id') && values.tenant_id === undefined) {
    values.tenant_id = 'tenant_local'
  }
  return values
}

function encodeSqlite(value) {
  if (value === undefined || value === null) return null
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 1 : 0
  return value
}

// ------------------------------------------------------------
// 生产路径：Sequelize ORM
// ------------------------------------------------------------
let ormPromise = null

function sequelizeType(name, sqliteType, DataTypes) {
  if (['created_at', 'updated_at', 'deleted_at'].includes(name)) {
    return DataTypes.DATE
  }
  if (['payload', 'metadata', 'data', 'json'].includes(name)) {
    return DataTypes.JSON
  }
  if (sqliteType === 'INTEGER') return DataTypes.INTEGER
  if (sqliteType === 'REAL') return DataTypes.FLOAT
  if (sqliteType === 'BLOB') return DataTypes.BLOB
  return DataTypes.TEXT
}

export async function createSequelize() {
  // 动态 import：沙盒未配置 DATABASE_URL 时不会加载 sequelize，
  // 因而无需在预览镜像里预装生产依赖。
  const { Sequelize } = await import('sequelize')
  const dialect = STORAGE === 'postgresql' ? 'postgres' : 'mysql'
  return new Sequelize(DATABASE_URL, {
    dialect,
    logging: false,
    // 云数据库（阿里云 RDS / Neon / Supabase）通常强制 TLS；
    // 是否启用必须由部署平台的环境变量决定，不能写死在代码里。
    dialectOptions:
      process.env.DB_SSL === 'true'
        ? { ssl: { require: true, rejectUnauthorized: false } }
        : {},
    pool: {
      max: Number(process.env.DB_POOL_MAX || 10),
      min: 0,
      acquire: 30000,
      idle: 10000,
    },
  })
}

function modelNameFor(table) {
  const normalized = String(table || 'record').replace(/[^A-Za-z0-9_]/g, '_')
  return `Record_${normalized}`
}

function attributesFor(model, DataTypes) {
  const attributes = {}
  for (const [name, sqliteType] of model.columnDefs) {
    const isId = name === 'id'
    attributes[name] = {
      type: sequelizeType(name, sqliteType, DataTypes),
      allowNull: isId ? false : true,
      primaryKey: isId,
      autoIncrement: isId,
    }
  }
  return attributes
}

async function connectOrm() {
  const { DataTypes } = await import('sequelize')
  const instance = await createSequelize()
  const models = {}
  for (const def of MODEL_DEFS) {
    models[def.table] = instance.define(
      modelNameFor(def.table),
      attributesFor(def, DataTypes),
      {
        tableName: def.table,
        timestamps: false,
        freezeTableName: true,
      },
    )
  }
  // 鉴权查询走原生 SQL，不额外 define 模型：
  // 1) 不破坏“单核心表 define”门禁；
  // 2) 完整列（含 password_hash）不会进入 include，避免敏感字段外泄。

  // 主从表关联：hasMany / belongsTo 必须成对声明。缺任何一侧，
  // Sequelize 的 include 都会报 "X is not associated to Y"。
  for (const relation of RELATIONS) {
    const parent = models[relation.parentTable]
    const child = models[relation.childTable]
    if (!parent || !child) continue
    parent.hasMany(child, {
      foreignKey: relation.foreignKey,
      sourceKey: relation.parentKey || 'id',
      as: relation.childAlias,
      onDelete: relation.onDelete || 'CASCADE',
    })
    child.belongsTo(parent, {
      foreignKey: relation.foreignKey,
      targetKey: relation.parentKey || 'id',
      as: relation.parentAlias,
      onDelete: relation.onDelete || 'CASCADE',
    })
  }

  await instance.authenticate()
  // 生产环境绝不调用 model.sync() 自动建表：表结构唯一来源是
  // server/migrations/*.sql，由 `npm run db:migrate` 显式执行。
  // 未迁移时健康检查返回 503，让部署平台在发布阶段就暴露问题。
  if (process.env.DB_SEED === 'true') {
    await seedOrm(models[TABLE])
  }
  return { instance, model: models[TABLE], models }
}

function getOrm() {
  if (!ORM_ENABLED) return null
  if (!ormPromise) ormPromise = connectOrm()
  return ormPromise
}

async function seedOrm(model) {
  const total = await model.count()
  if (Number(total) > 0) return
  for (const row of SEED_ROWS) {
    await model.create(buildValues(row))
  }
}

// ------------------------------------------------------------
// 本地预览路径：Node 22 内置 SQLite
// ------------------------------------------------------------
async function createSqliteState() {
  const { DatabaseSync } = await import('node:sqlite')
  mkdirSync(DATA_DIR, { recursive: true })
  const db = new DatabaseSync(DB_PATH)
  // 沙盒 SQLite 是单写者：锁竞争时等待更久，避免 6s 级锁直接 500。
  // 生产 MySQL/PostgreSQL 不依赖这条设置，但仍需应用层重试与幂等。
  db.exec('PRAGMA busy_timeout = 15000;')
  db.exec(ALL_SCHEMA_SQL)
  const state = { db }
  await seedSqlite(state)
  return state
}

function getSqliteState() {
  // 单例 Promise：并发首个请求不会重复建库或重复灌种子数据。
  if (!sqliteStatePromise) sqliteStatePromise = createSqliteState()
  return sqliteStatePromise
}

function sqliteAll(state, sql, params = []) {
  return state.db.prepare(sql).all(...params)
}

function sqliteGet(state, sql, params = []) {
  return state.db.prepare(sql).get(...params) ?? null
}

function sqliteRun(state, sql, params = []) {
  const result = state.db.prepare(sql).run(...params)
  return {
    lastID: Number(result.lastInsertRowid),
    changes: Number(result.changes),
  }
}

function sqliteInsert(state, values, table = TABLE) {
  const columns = Object.keys(values)
  if (columns.length === 0) throw new Error('没有可写入的字段')
  const sql =
    `INSERT INTO "${table}" (` +
    columns.map((column) => `"${column}"`).join(', ') +
    ') VALUES (' +
    columns.map(() => '?').join(', ') +
    ')'
  const result = sqliteRun(
    state,
    sql,
    columns.map((column) => encodeSqlite(values[column])),
  )
  return sqliteGetRecord(state, result.lastID, table)
}

function sqliteGetRecord(state, id, table = TABLE) {
  return sqliteGet(state, `SELECT * FROM "${table}" WHERE "id" = ?`, [Number(id)])
}

// 关联表只查询公开列（RELATED_MODELS 已剔除鉴权字段），
// 禁止 SELECT * 把 password_hash / iam_sub 带回前端。
function sqliteSelectColumns(table) {
  const model = modelFor(table)
  return model.columns.map((column) => `"${column}"`).join(', ')
}

function attachSqliteRelations(state, row) {
  if (!row) return row
  for (const relation of RELATIONS) {
    if (relation.parentTable === TABLE) {
      row[relation.childAlias] = sqliteAll(
        state,
        `SELECT ${sqliteSelectColumns(relation.childTable)} FROM "${
          relation.childTable
        }" WHERE "${relation.foreignKey}" = ? ORDER BY "id" DESC`,
        [row[relation.parentKey || 'id']],
      )
    } else if (relation.childTable === TABLE) {
      row[relation.parentAlias] = sqliteGet(
        state,
        `SELECT ${sqliteSelectColumns(relation.parentTable)} FROM "${
          relation.parentTable
        }" WHERE "${relation.parentKey || 'id'}" = ?`,
        [row[relation.foreignKey]],
      )
    }
  }
  return row
}

function seedSqlite(state) {
  const total = sqliteGet(state, `SELECT COUNT(*) AS total FROM "${TABLE}"`)
  if (Number(total?.total ?? 0) > 0) return
  for (const row of SEED_ROWS) {
    sqliteInsert(state, buildValues(row))
  }
}

let sqliteStatePromise = null

// ------------------------------------------------------------
// 对外仓储接口：ORM 与 SQLite 行为一致
// ------------------------------------------------------------
// Sequelize include 别名必须与 db.js 里的 hasMany / belongsTo 完全一致。
function buildIncludes(models) {
  const includes = []
  for (const relation of RELATIONS) {
    if (relation.parentTable === TABLE) {
      const child = models[relation.childTable]
      if (child) {
        includes.push({
          model: child,
          as: relation.childAlias,
          required: false,
          // separate 让 hasMany 走独立查询，避免 LIMIT 被 JOIN 放大。
          separate: true,
          order: [['id', 'DESC']],
        })
      }
    } else if (relation.childTable === TABLE) {
      const parent = models[relation.parentTable]
      if (parent) {
        includes.push({ model: parent, as: relation.parentAlias, required: false })
      }
    }
  }
  return includes
}

export async function listRecords({ page = 1, size = 20 } = {}) {
  if (ORM_ENABLED) {
    const { model, models } = await getOrm()
    const includes = buildIncludes(models)
    const { rows, count } = await model.findAndCountAll({
      limit: size,
      offset: (page - 1) * size,
      order: [['id', 'DESC']],
      ...(includes.length
        ? { include: includes, distinct: true }
        : { raw: true }),
    })
    return {
      items: includes.length
        ? rows.map((row) => row.get({ plain: true }))
        : rows,
      total: Number(count),
      page,
      size,
    }
  }
  const state = await getSqliteState()
  const total = sqliteGet(
    state,
    `SELECT COUNT(*) AS total FROM "${TABLE}"`,
  )
  const items = sqliteAll(
    state,
    `SELECT * FROM "${TABLE}" ORDER BY "id" DESC LIMIT ? OFFSET ?`,
    [size, (page - 1) * size],
  ).map((row) => attachSqliteRelations(state, row))
  return {
    items,
    total: Number(total?.total ?? 0),
    page,
    size,
  }
}

export async function getRecord(id) {
  if (ORM_ENABLED) {
    const { model, models } = await getOrm()
    const includes = buildIncludes(models)
    const row = await model.findByPk(Number(id), {
      ...(includes.length ? { include: includes } : { raw: true }),
    })
    if (!row) return null
    return includes.length ? row.get({ plain: true }) : row
  }
  const state = await getSqliteState()
  return attachSqliteRelations(state, sqliteGetRecord(state, id))
}

export async function createRecord(input = {}) {
  const values = buildValues(input)
  if (ORM_ENABLED) {
    const { model, models } = await getOrm()
    const row = await model.create(values)
    const plain = row.get({ plain: true })
    // 创建响应也带上关联集合，前端无需再发一次详情请求。
    if (RELATIONS.some((relation) => relation.parentTable === TABLE)) {
      return getRecord(plain.id)
    }
    return plain
  }
  const state = await getSqliteState()
  return attachSqliteRelations(state, sqliteInsert(state, values))
}

export async function updateRecord(id, input = {}) {
  const values = buildValues(input, { update: true })
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const row = await model.findByPk(Number(id))
    if (!row) return null
    if (Object.keys(values).length > 0) await row.update(values)
    return getRecord(id)
  }
  const state = await getSqliteState()
  const columns = Object.keys(values)
  if (columns.length === 0) {
    return attachSqliteRelations(state, sqliteGetRecord(state, id))
  }
  sqliteRun(
    state,
    `UPDATE "${TABLE}" SET ` +
      columns.map((column) => `"${column}" = ?`).join(', ') +
      ' WHERE "id" = ?',
    [
      ...columns.map((column) => encodeSqlite(values[column])),
      Number(id),
    ],
  )
  return attachSqliteRelations(state, sqliteGetRecord(state, id))
}

// ------------------------------------------------------------
// 主从表：子资源查询 / 创建（hasMany）
// ------------------------------------------------------------
function relationForAlias(alias) {
  return RELATIONS.find(
    (relation) =>
      relation.parentTable === TABLE && relation.childAlias === String(alias),
  )
}

export async function listRelatedRecords(
  parentId,
  alias,
  { page = 1, size = 20 } = {},
) {
  const relation = relationForAlias(alias)
  if (!relation) return null
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    const child = models[relation.childTable]
    const parent = models[relation.parentTable]
    if (!child || !parent) return null
    const parentRow = await parent.findByPk(Number(parentId), { raw: true })
    if (!parentRow) return null
    const { rows, count } = await child.findAndCountAll({
      where: { [relation.foreignKey]: Number(parentId) },
      limit: size,
      offset: (page - 1) * size,
      order: [['id', 'DESC']],
      raw: true,
    })
    return { items: rows, total: Number(count), page, size }
  }
  const state = await getSqliteState()
  const parentRow = sqliteGetRecord(state, parentId, relation.parentTable)
  if (!parentRow) return null
  const total = sqliteGet(
    state,
    `SELECT COUNT(*) AS total FROM "${relation.childTable}" WHERE "${
      relation.foreignKey
    }" = ?`,
    [Number(parentId)],
  )
  const items = sqliteAll(
    state,
    `SELECT ${sqliteSelectColumns(relation.childTable)} FROM "${
      relation.childTable
    }" WHERE "${relation.foreignKey}" = ? ORDER BY "id" DESC LIMIT ? OFFSET ?`,
    [Number(parentId), size, (page - 1) * size],
  )
  return { items, total: Number(total?.total ?? 0), page, size }
}

export async function createRelatedRecord(parentId, alias, input = {}) {
  const relation = relationForAlias(alias)
  if (!relation) return null
  const childTable = relation.childTable
  const values = buildValues(input, { table: childTable })
  // 外键由 URL 决定，禁止前端伪造把子记录挂到别的父记录下。
  values[relation.foreignKey] = Number(parentId)
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    const child = models[childTable]
    const parent = models[relation.parentTable]
    if (!child || !parent) return null
    const parentRow = await parent.findByPk(Number(parentId), { raw: true })
    if (!parentRow) return null
    const row = await child.create(values)
    return row.get({ plain: true })
  }
  const state = await getSqliteState()
  const parentRow = sqliteGetRecord(state, parentId, relation.parentTable)
  if (!parentRow) return null
  return sqliteInsert(state, values, childTable)
}

export async function deleteRecord(id) {
  if (ORM_ENABLED) {
    const { model } = await getOrm()
    const changes = await model.destroy({ where: { id: Number(id) } })
    return { deleted: changes > 0, id: Number(id) }
  }
  const result = sqliteRun(
    await getSqliteState(),
    `DELETE FROM "${TABLE}" WHERE "id" = ?`,
    [Number(id)],
  )
  return { deleted: result.changes > 0, id: Number(id) }
}

// ------------------------------------------------------------
// 多实体 CRUD：契约中除核心表外的顶层集合（例如 patrol_routes）。
// table 必须命中 MODEL_DEFS 白名单，禁止任意表名拼接。
// ------------------------------------------------------------
function resolveModelDef(table) {
  const key = String(table || '').trim().toLowerCase()
  return MODEL_DEFS.find((item) => item.table.toLowerCase() === key) || null
}

export async function listTableRecords(table, { page = 1, size = 20 } = {}) {
  const def = resolveModelDef(table)
  if (!def) throw new Error(`UNKNOWN_TABLE:${table}`)
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    const model = models[def.table]
    const { rows, count } = await model.findAndCountAll({
      limit: size,
      offset: (page - 1) * size,
      order: [['id', 'DESC']],
      raw: true,
    })
    return { items: rows, total: Number(count), page, size }
  }
  const state = await getSqliteState()
  const total = sqliteGet(state, `SELECT COUNT(*) AS total FROM "${def.table}"`)
  const items = sqliteAll(
    state,
    `SELECT * FROM "${def.table}" ORDER BY "id" DESC LIMIT ? OFFSET ?`,
    [size, (page - 1) * size],
  )
  return { items, total: Number(total?.total ?? 0), page, size }
}

export async function getTableRecord(table, id) {
  const def = resolveModelDef(table)
  if (!def) throw new Error(`UNKNOWN_TABLE:${table}`)
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    return models[def.table].findByPk(Number(id), { raw: true })
  }
  return sqliteGetRecord(await getSqliteState(), id, def.table)
}

export async function createTableRecord(table, input = {}) {
  const def = resolveModelDef(table)
  if (!def) throw new Error(`UNKNOWN_TABLE:${table}`)
  const values = buildValues(input, { table: def.table })
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    const row = await models[def.table].create(values)
    return row.get({ plain: true })
  }
  return sqliteInsert(await getSqliteState(), values, def.table)
}

export async function updateTableRecord(table, id, input = {}) {
  const def = resolveModelDef(table)
  if (!def) throw new Error(`UNKNOWN_TABLE:${table}`)
  const values = buildValues(input, { update: true, table: def.table })
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    const row = await models[def.table].findByPk(Number(id))
    if (!row) return null
    if (Object.keys(values).length > 0) await row.update(values)
    return row.get({ plain: true })
  }
  const state = await getSqliteState()
  const columns = Object.keys(values)
  if (columns.length > 0) {
    sqliteRun(
      state,
      `UPDATE "${def.table}" SET ` +
        columns.map((column) => `"${column}" = ?`).join(', ') +
        ' WHERE "id" = ?',
      [
        ...columns.map((column) => encodeSqlite(values[column])),
        Number(id),
      ],
    )
  }
  return sqliteGetRecord(state, id, def.table)
}

export async function deleteTableRecord(table, id) {
  const def = resolveModelDef(table)
  if (!def) throw new Error(`UNKNOWN_TABLE:${table}`)
  if (ORM_ENABLED) {
    const { models } = await getOrm()
    const changes = await models[def.table].destroy({
      where: { id: Number(id) },
    })
    return { deleted: changes > 0, id: Number(id) }
  }
  const result = sqliteRun(
    await getSqliteState(),
    `DELETE FROM "${def.table}" WHERE "id" = ?`,
    [Number(id)],
  )
  return { deleted: result.changes > 0, id: Number(id) }
}

// ------------------------------------------------------------
// 鉴权查询：完整列（含 password_hash）只在服务端使用。
// ------------------------------------------------------------
async function queryAuthRows(sql, replacements = [], type = QueryTypes.SELECT) {
  const { instance } = await getOrm()
  return instance.query(sql, { replacements, type })
}

export async function countAuthUsers() {
  if (!AUTH_ENABLED || !AUTH_MODEL) return 0
  if (ORM_ENABLED) {
    const rows = await queryAuthRows(
      `SELECT COUNT(*) AS total FROM ${AUTH_TABLE}`,
    )
    return Number(rows?.[0]?.total ?? 0)
  }
  const total = sqliteGet(
    await getSqliteState(),
    `SELECT COUNT(*) AS total FROM "${AUTH_TABLE}"`,
  )
  return Number(total?.total ?? 0)
}

export async function findAuthUserByUsername(username) {
  if (!AUTH_ENABLED || !AUTH_MODEL) return null
  if (ORM_ENABLED) {
    const rows = await queryAuthRows(
      `SELECT * FROM ${AUTH_TABLE} WHERE ${AUTH_USERNAME_COLUMN} = ? LIMIT 1`,
      [String(username || '')],
    )
    return rows?.[0] || null
  }
  return (
    sqliteGet(
      await getSqliteState(),
      `SELECT * FROM "${AUTH_TABLE}" WHERE "${AUTH_USERNAME_COLUMN}" = ? LIMIT 1`,
      [String(username || '')],
    ) || null
  )
}

export async function findAuthUserById(id) {
  if (!AUTH_ENABLED || !AUTH_MODEL) return null
  if (ORM_ENABLED) {
    const rows = await queryAuthRows(
      `SELECT * FROM ${AUTH_TABLE} WHERE ${AUTH_ID_COLUMN} = ? LIMIT 1`,
      [Number(id)],
    )
    return rows?.[0] || null
  }
  return (
    sqliteGet(
      await getSqliteState(),
      `SELECT * FROM "${AUTH_TABLE}" WHERE "${AUTH_ID_COLUMN}" = ? LIMIT 1`,
      [Number(id)],
    ) || null
  )
}

export async function createAuthUser(values = {}) {
  if (!AUTH_ENABLED || !AUTH_MODEL) {
    throw new Error('AUTH_NOT_ENABLED')
  }
  const payload = { ...values }
  const columns = AUTH_MODEL.columns
  if (columns.includes('created_at') && payload.created_at === undefined) {
    payload.created_at = new Date().toISOString()
  }
  if (columns.includes('updated_at') && payload.updated_at === undefined) {
    payload.updated_at = new Date().toISOString()
  }
  if (ORM_ENABLED) {
    const keys = Object.keys(payload)
    if (keys.length === 0) throw new Error('没有可写入的鉴权字段')
    await queryAuthRows(
      `INSERT INTO ${AUTH_TABLE} (${keys.join(', ')}) VALUES (` +
        keys.map(() => '?').join(', ') +
        ')',
      keys.map((key) => payload[key]),
      QueryTypes.INSERT,
    )
    return findAuthUserByUsername(payload[AUTH_USERNAME_COLUMN])
  }
  return sqliteInsert(await getSqliteState(), payload, AUTH_TABLE)
}

export async function health() {
  if (ORM_ENABLED) {
    const { instance, model } = await getOrm()
    // authenticate 验证连接与账号权限；失败时直接抛出，由 /api/health
    // 转为 HTTP 503，让负载均衡 / 部署平台停止摘流。
    await instance.authenticate()
    let tableReady = true
    let records = 0
    let reason = ''
    try {
      records = Number(await model.count())
    } catch (error) {
      // 连接正常但表不存在：通常是迁移未执行，返回 degraded 而不是 ok。
      tableReady = false
      reason = String(error?.message || error)
    }
    return {
      status: tableReady ? 'ok' : 'degraded',
      runtime: RUNTIME,
      storage: STORAGE,
      orm: 'sequelize',
      databaseUrlConfigured: true,
      table: TABLE,
      entity: ENTITY,
      tableReady,
      records,
      ...(reason ? { reason } : {}),
    }
  }
  const total = sqliteGet(
    await getSqliteState(),
    `SELECT COUNT(*) AS total FROM "${TABLE}"`,
  )
  return {
    status: 'ok',
    runtime: 'express-sqlite',
    storage: 'sqlite',
    orm: 'sqlite-fallback',
    databaseUrlConfigured: false,
    table: TABLE,
    entity: ENTITY,
    tableReady: true,
    records: Number(total?.total ?? 0),
    dbPath: DB_PATH,
  }
}

export {
  DATABASE_URL,
  DB_ENGINE,
  DB_PATH,
  ORM_ENABLED,
  RUNTIME,
  SCHEMA_SQL,
  STORAGE,
}
