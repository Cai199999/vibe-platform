import { describe, expect, it } from 'vitest'
import {
  SOURCE_PROJECTION_PRINCIPLE,
  SOURCE_PROJECTION_TIP,
  SourceProjectionError,
  buildSourceProjection,
  flattenSourceProjection,
  projectionHierarchyText,
} from '../sourceProjection.js'
import { sampleSourceAnalysis } from './sourceAnalysisFixture.js'

describe('存量源码只读投影', () => {
  it('把分析报告转换为只读 VisualNode 投影', () => {
    const projection = buildSourceProjection(sampleSourceAnalysis())

    expect(projection.analysisOnly).toBe(true)
    expect(projection.writable).toBe(false)
    expect(projection.entryComponent).toBe('Card')
    expect(projection.root.type).toBe('container')
    expect(projection.root.projection.path).toEqual(['Card', 'div'])
    expect(projection.tip).toBe(SOURCE_PROJECTION_TIP)
    expect(projection.principle).toBe(SOURCE_PROJECTION_PRINCIPLE)
    expect(projection.root.locked).toBe(true)
    expect(projection.componentHierarchies).toContain('Card -> Header -> Text')
  })

  it('展开同文件组件并保留层级与 className', () => {
    const projection = buildSourceProjection(sampleSourceAnalysis())
    const nodes = flattenSourceProjection(projection.root)
    const header = nodes.find((node) => node.projection?.tag === 'Header')
    const headerElement = nodes.find((node) => node.projection?.tag === 'header')
    const textNode = nodes.find((node) => node.props?.text === '卡片标题')

    expect(header?.projection.inlined).toBe(true)
    expect(headerElement).toBeTruthy()
    expect(headerElement?.projection.path).toEqual([
      'Card',
      'div',
      'Header',
      'header',
    ])
    expect(textNode?.projection.path).toContain('Text')
    expect(headerElement?.projection.classes).toContain('items-center')
    expect(projectionHierarchyText(headerElement)).toContain('Card › div › Header')
  })

  it('拒绝未声明只读的分析报告', () => {
    const analysis = sampleSourceAnalysis()
    analysis.writable = true
    expect(() => buildSourceProjection(analysis)).toThrow(SourceProjectionError)
  })

  it('缺少入口根节点时拒绝生成投影', () => {
    const analysis = sampleSourceAnalysis()
    analysis.root = null
    expect(() => buildSourceProjection(analysis)).toThrow(SourceProjectionError)
  })
})
