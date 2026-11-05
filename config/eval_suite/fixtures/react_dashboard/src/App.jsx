export default function App() {
  return (
    <main className="app-shell">
      <section className="hero">
        <p className="eyebrow" data-source="hero-kicker">
          VIBE PLATFORM
        </p>
        <h1 data-source="hero-title">Build faster</h1>
        <p data-source="hero-copy">把想法变成可运行的全栈应用。</p>
        <button data-source="primary-action" type="button">
          开始使用
        </button>
      </section>
      <section className="pricing-card" data-source="pricing-card">
        <h2>Pro</h2>
        <p>适合成长型团队。</p>
      </section>
    </main>
  )
}
