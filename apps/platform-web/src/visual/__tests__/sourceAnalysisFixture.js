/** 测试用 Babel 分析报告；形状与 analyze_existing_react.mjs 输出一致。 */
export function sampleSourceAnalysis() {
  const textRoot = {
    id: 'el_text_1',
    kind: 'element',
    tag: 'p',
    className: 'm-0 text-sm text-slate-700',
    classes: ['m-0', 'text-sm', 'text-slate-700'],
    attributes: {},
    source: { file: 'Card.jsx', line: 2, column: 10 },
    children: [
      {
        id: 'tx_text_1',
        kind: 'text',
        tag: '#text',
        text: '卡片标题',
        source: { file: 'Card.jsx', line: 2, column: 48 },
        children: [],
      },
    ],
  }
  const headerRoot = {
    id: 'el_header_1',
    kind: 'element',
    tag: 'header',
    className: 'flex items-center border-b px-4 py-3',
    classes: ['flex', 'items-center', 'border-b', 'px-4', 'py-3'],
    attributes: {},
    source: { file: 'Card.jsx', line: 6, column: 5 },
    children: [
      {
        id: 'cp_text_2',
        kind: 'component',
        tag: 'Text',
        className: '',
        classes: [],
        attributes: {},
        source: { file: 'Card.jsx', line: 7, column: 7 },
        children: [
          {
            id: 'tx_text_2',
            kind: 'text',
            tag: '#text',
            text: '卡片标题',
            source: { file: 'Card.jsx', line: 7, column: 13 },
            children: [],
          },
        ],
      },
    ],
  }
  const cardRoot = {
    id: 'el_card_1',
    kind: 'element',
    tag: 'div',
    className: 'rounded-xl border bg-white p-4 shadow',
    classes: ['rounded-xl', 'border', 'bg-white', 'p-4', 'shadow'],
    attributes: {},
    source: { file: 'Card.jsx', line: 13, column: 5 },
    children: [
      {
        id: 'cp_header_1',
        kind: 'component',
        tag: 'Header',
        className: '',
        classes: [],
        attributes: {},
        source: { file: 'Card.jsx', line: 14, column: 7 },
        children: [],
      },
    ],
  }
  return {
    schemaVersion: '0.1-analysis',
    kind: 'ReactSourceAnalysis',
    analysisOnly: true,
    writable: false,
    file: 'Card.jsx',
    framework: 'react',
    parser: { engine: 'babel', requireRoot: '/sandbox_template/package.json' },
    entryComponent: 'Card',
    root: cardRoot,
    components: [
      { name: 'Text', exported: 'named', roots: [textRoot], root: textRoot },
      {
        name: 'Header',
        exported: 'named',
        roots: [headerRoot],
        root: headerRoot,
      },
      { name: 'Card', exported: 'default', roots: [cardRoot], root: cardRoot },
    ],
    componentHierarchies: ['Card -> Header -> Text'],
    stats: { components: 3, elements: 3, texts: 3, maxDepth: 4 },
    warnings: [],
  }
}
