import { describe, expect, it } from 'vitest'
import {
  PROJECT_MODE,
  projectEntrySource,
  resolveProjectMode,
} from '../projectMode.js'

const blankProject = () => ({
  id: 'proj_blank',
  template: 'vibe-react',
  files: {},
  bootstrap: 'pending',
  generated: false,
})

const importedProject = () => ({
  id: 'proj_imported',
  template: 'imported-react',
  imported: true,
  files: { 'src/Users.jsx': 'export default function Users() {}' },
  bootstrap: 'ready',
  generated: false,
})

describe('ProjectModeResolver', () => {
  it('空白新建项目走 NEW_PROJECT', () => {
    expect(resolveProjectMode(blankProject())).toBe(PROJECT_MODE.NEW_PROJECT)
  })

  it('内置演示模板不被误判为存量源码', () => {
    const demo = {
      ...blankProject(),
      files: { 'src/App.jsx': 'export default function App() { return null }' },
    }
    expect(resolveProjectMode(demo)).toBe(PROJECT_MODE.NEW_PROJECT)
  })

  it('导入项目 / 已有 sandbox / 已生成项目均为 EXISTING_SOURCE', () => {
    expect(resolveProjectMode(importedProject())).toBe(
      PROJECT_MODE.EXISTING_SOURCE,
    )
    expect(
      resolveProjectMode(blankProject(), { sandboxId: 'sbx_1' }),
    ).toBe(PROJECT_MODE.EXISTING_SOURCE)
    expect(
      resolveProjectMode({ ...blankProject(), generated: true }),
    ).toBe(PROJECT_MODE.EXISTING_SOURCE)
  })

  it('非内置模板的非空 files 视为存量源码', () => {
    const manual = {
      ...blankProject(),
      template: 'unknown',
      files: { 'src/App.jsx': 'export default function App() { return null }' },
    }
    expect(resolveProjectMode(manual)).toBe(PROJECT_MODE.EXISTING_SOURCE)
  })

  it('可以取出导入项目的入口源码', () => {
    expect(projectEntrySource(importedProject())).toEqual({
      fileName: 'src/Users.jsx',
      source: 'export default function Users() {}',
    })
  })
})
