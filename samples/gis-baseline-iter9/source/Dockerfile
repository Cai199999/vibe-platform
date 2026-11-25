# syntax=docker/dockerfile:1
# 由 InfraCoder 生成：后端 API 生产镜像（多阶段构建）。
# 构建上下文为项目根目录：docker build -t vibe-facilities-api .
#
# 阶段 1 只负责安装生产依赖；阶段 2 仅复制 node_modules 与源码，
# 不携带 npm 缓存、构建工具和 devDependencies，镜像体积更小、攻击面更低。

FROM node:22-alpine AS deps
WORKDIR /app
# 先复制依赖清单，命中 Docker 层缓存；生产依赖包含 Sequelize 与驱动。
COPY server/package.json ./server/package.json
RUN cd server && npm install --omit=dev --no-audit --no-fund

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000
# 保留 server/ 目录层级：db.js 默认把 SQLite 放在 /app/data，
# 与沙盒目录结构一致，未配置 DATABASE_URL 时也能直接写库。
COPY --from=deps --chown=node:node /app/server/node_modules ./server/node_modules
COPY --chown=node:node server/ ./server
# COPY 以 root 写入时必须显式 --chown 或在切换用户前再次移交属主；
# 否则 node 用户无法创建 server/uploads（multer 落盘）与 data/（SQLite），
# 上传会直接 500 EACCES。
RUN mkdir -p /app/server/uploads /app/data \
    && chown -R node:node /app
USER node

EXPOSE 3000
# 生产发布流程：先 node server/migrate.mjs，再启动本镜像。
CMD ["node", "server/index.js"]
