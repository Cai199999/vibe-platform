import { useEffect } from 'react'

/**
 * 【部署上线】Mock 指引。
 *
 * 当前阶段不真实调用云厂商 API：全栈生成器已经在导出包中产出
 * Dockerfile / render.yaml / .env.example / DEPLOY.md，本弹窗负责把
 * “下一步怎么上线”讲清楚，避免生成物停留在沙盒玩具。
 */
export default function DeployGuideDialog({ open, onClose, sandboxId = '' }) {
  useEffect(() => {
    if (!open) return undefined
    const handleKey = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="deploy-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="deploy-dialog-title"
        data-deploy-dialog
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <span className="eyebrow">DEPLOY · MOCK</span>
            <h2 id="deploy-dialog-title">部署上线</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="关闭部署指引"
          >
            ×
          </button>
        </div>

        <p className="deploy-dialog-lead">
          已生成生产级 Dockerfile 与云端部署配置。导出代码包后，可在 Render /
          Railway / Vercel 上一键部署；当前按钮为指引 Mock，不会真实调用云厂商。
        </p>

        {sandboxId && (
          <p className="deploy-dialog-sandbox">
            当前沙盒 <code>{sandboxId}</code>
          </p>
        )}

        <section className="deploy-dialog-section">
          <h3>已包含的部署产物</h3>
          <ul className="deploy-file-list">
            <li>
              <code>Dockerfile</code>
              <span>后端 API 生产镜像（Express + Sequelize）</span>
            </li>
            <li>
              <code>render.yaml</code>
              <span>Render Blueprint：后端 Docker + 前端静态站</span>
            </li>
            <li>
              <code>.env.example</code>
              <span>DATABASE_URL、端口、CORS 等环境变量模板</span>
            </li>
            <li>
              <code>DEPLOY.md</code>
              <span>Render / Vercel / Railway / Docker 分步指引</span>
            </li>
          </ul>
        </section>

        <section className="deploy-dialog-section">
          <h3>上线三步</h3>
          <ol className="deploy-steps">
            <li>
              <strong>导出代码包</strong>
              <span>从工作区导出项目，推送到 Git 仓库。</span>
            </li>
            <li>
              <strong>准备云数据库</strong>
              <span>
                创建 MySQL / PostgreSQL 实例，把连接串写入部署平台的
                <code>DATABASE_URL</code>。
              </span>
            </li>
            <li>
              <strong>一键部署</strong>
              <span>
                Render 选择 Blueprint 读取 <code>render.yaml</code>；或 Railway
                直接识别 <code>Dockerfile</code>；Vercel 部署前端静态站并设置
                <code>VITE_API_BASE_URL</code>。
              </span>
            </li>
          </ol>
        </section>

        <div className="deploy-dialog-note">
          部署完成后访问 <code>/api/health</code>，确认 <code>status=ok</code>{' '}
          且 <code>storage=mysql</code> 或 <code>storage=postgresql</code>。
        </div>

        <div className="dialog-actions">
          <button className="primary-button" type="button" onClick={onClose}>
            我知道了
          </button>
        </div>
      </div>
    </div>
  )
}
