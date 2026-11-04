// M1 演示项目的初始源码。
// 与画布中的 Mock 页面保持一致；提交 Run 时作为沙盒初始文件。
// 版式刻意紧凑，保证右侧较窄的预览 iframe 里也能完整看到按钮。
export const INITIAL_APP = `export default function App() {
  return (
    <main style={{ minHeight: '100vh', margin: 0, fontFamily: '"Inter", "Segoe UI", sans-serif', background: '#0f172a', color: '#e2e8f0', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '24px', boxSizing: 'border-box' }}>
      <style>{"html,body{margin:0;background:#0f172a;overflow:hidden}.vibe-card{width:min(720px,100%);padding:28px;border-radius:22px;background:rgba(15,23,42,.82);border:1px solid rgba(148,163,184,.22);box-shadow:0 24px 64px rgba(0,0,0,.45);box-sizing:border-box}.vibe-kicker{margin:0;letter-spacing:.3em;text-transform:uppercase;font-size:11px;color:#38bdf8}.vibe-title{font-size:32px;line-height:1.16;margin:14px 0 12px}.vibe-copy{margin:0;color:#94a3b8;font-size:13.5px;line-height:1.7}.vibe-cta{margin-top:22px;padding:12px 24px;border:0;border-radius:999px;background:#2563eb;color:#fff;font-size:14px;font-weight:600;cursor:pointer}.vibe-cta:hover{background:#1d4ed8}"}</style>
      <section className="vibe-card" data-vibe-target="card">
        <p className="vibe-kicker" data-vibe-target="kicker">Vibe Studio</p>
        <h1 className="vibe-title" data-vibe-target="title">把想法画出来。<br />剩下的交给 Agent。</h1>
        <p className="vibe-copy" data-vibe-target="copy">在画布上圈选任意组件，写下修改意图。Agent 会生成代码、运行构建，并自动修复错误。</p>
        <button className="vibe-cta" data-vibe-target="cta-button">开始体验</button>
      </section>
    </main>
  )
}
`
