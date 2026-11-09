export default function App() {
  // 最外层占满 iframe 视口，避免预览四周出现黑边或内容被垂直裁切。
  return (
    <main
      style={{
        width: '100%',
        minHeight: '100vh',
        padding: 32,
        boxSizing: 'border-box',
      }}
    >
      <h1>Vibe Sandbox</h1>
      <p>Sandbox is ready.</p>
    </main>
  )
}
