import { describe, expect, it } from 'vitest'

import { responseError as fullstackError } from '../fullstackStream.js'
import { responseError as architectError } from '../architectStream.js'

function fakeResponse({ status = 500, statusText = '', body = null }) {
  return {
    status,
    statusText,
    json: async () => {
      if (body === null) throw new Error('not json')
      return body
    },
  }
}

describe('fullstackStream.responseError', () => {
  it('把 RUN_NOT_FOUND 归类为已过期，并给出可操作提示', async () => {
    const error = await fullstackError(
      fakeResponse({
        status: 404,
        body: { detail: { code: 'FULLSTACK_RUN_NOT_FOUND', message: 'x' } },
      }),
    )
    expect(error.code).toBe('FULLSTACK_RUN_NOT_FOUND')
    expect(error.runExpired).toBe(true)
    expect(error.message).toContain('已过期')
    expect(error.message).toContain('重新点击')
  })

  it('非 JSON 的 404 也按已过期处理', async () => {
    const error = await fullstackError(fakeResponse({ status: 404, body: null }))
    expect(error.runExpired).toBe(true)
  })

  it('普通 500 错误不标记为过期，保留原始信息', async () => {
    const error = await fullstackError(
      fakeResponse({
        status: 500,
        body: { detail: { code: 'FULLSTACK_INTERNAL_ERROR', message: 'boom' } },
      }),
    )
    expect(error.code).toBe('FULLSTACK_INTERNAL_ERROR')
    expect(error.runExpired).toBe(false)
    expect(error.message).toContain('boom')
  })

  it('启动新 Run 的 404 不误判为“记录过期”', async () => {
    const error = await fullstackError(
      fakeResponse({
        status: 404,
        body: { detail: { code: 'NOT_FOUND', message: '路由不存在' } },
      }),
      { allowExpired: false },
    )
    expect(error.runExpired).toBe(false)
    expect(error.httpStatus).toBe(404)
    expect(error.message).toContain('路由不存在')
  })
})

describe('architectStream.responseError', () => {
  it('把 ARCHITECT_RUN_NOT_FOUND 归类为已过期', async () => {
    const error = await architectError(
      fakeResponse({
        status: 404,
        body: { detail: { code: 'ARCHITECT_RUN_NOT_FOUND', message: 'x' } },
      }),
    )
    expect(error.code).toBe('ARCHITECT_RUN_NOT_FOUND')
    expect(error.runExpired).toBe(true)
    expect(error.message).toContain('架构会话已过期')
  })

  it('错误体是字符串时仍保留原文', async () => {
    const error = await architectError(
      fakeResponse({ status: 400, body: { detail: 'bad request' } }),
    )
    expect(error.message).toBe('bad request')
    expect(error.runExpired).toBe(false)
  })
})
