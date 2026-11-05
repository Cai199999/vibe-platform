# 单前端 React 沙盒基础镜像。
# 依赖在镜像构建期安装，创建沙盒时只需写入项目文件并启动 Vite，秒级就绪。
#
# 构建加速：
#   - 基础镜像走宿主已配置的 Registry Mirror；
#   - npm 依赖走 npmmirror 国内镜像；
#   - package.json 单独成层，改源码不会重新下载依赖。
FROM node:20-alpine

# git 用于应用 Agent 输出的 unified diff；unzip 供导入 zip 时容器内排查使用。
# 真正的上传解压通过 Docker put_archive 完成，不依赖容器内 unzip。
RUN apk add --no-cache git unzip \
    && npm config set registry https://registry.npmmirror.com

# 纯 HTML 导入项目的静态服务器（无 npm 依赖）。
COPY static_server.mjs /opt/vibe-static-server.mjs

WORKDIR /app

# 依赖层：package.json / lock 不变时命中 Docker 缓存。
COPY package*.json ./
RUN npm install --no-audit --no-fund

# 源码层：只复制运行所需文件；改代码只会让本层失效。
# .dockerignore 已排除 node_modules / dist，不会覆盖刚安装的依赖。
COPY . .

EXPOSE 5173

# 实际端口 / HMR 客户端端口由 LocalDockerSandbox 通过环境变量与端口映射控制。
CMD ["npm", "run", "dev"]
