# M1 开发模式：Vite dev server 直接运行，保留 HMR 与 /api 代理。
# 生产构建可改为多阶段：npm run build + Nginx 托管静态资源。
FROM node:20-alpine

# 国内加速 npm 安装；仅影响构建层，不改业务代码。
RUN npm config set registry https://registry.npmmirror.com

WORKDIR /app

# 先复制锁文件/依赖清单，最大化 Docker 层缓存。
COPY package*.json ./
RUN npm install --no-audit --no-fund

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
