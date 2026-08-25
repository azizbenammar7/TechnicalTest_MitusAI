import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class MockXmlHttpRequest {
  static instances: MockXmlHttpRequest[] = []
  method = ''
  url = ''
  status = 201
  responseText = ''
  headers: Record<string, string> = {}
  body: Document | XMLHttpRequestBodyInit | null = null
  upload: XMLHttpRequestUpload = { onprogress: null } as XMLHttpRequestUpload
  onload: ((this: XMLHttpRequest, ev: ProgressEvent) => unknown) | null = null
  onerror: ((this: XMLHttpRequest, ev: ProgressEvent) => unknown) | null = null

  constructor() { MockXmlHttpRequest.instances.push(this) }
  open(method: string, url: string) { this.method = method; this.url = url }
  setRequestHeader(name: string, value: string) { this.headers[name] = value }
  send(body: Document | XMLHttpRequestBodyInit | null) {
    this.body = body
    this.upload.onprogress?.call(
      this as unknown as XMLHttpRequest,
      new ProgressEvent('progress', { lengthComputable: true, loaded: 4, total: 4 }) as ProgressEvent<XMLHttpRequestEventTarget>,
    )
    this.onload?.call(this as unknown as XMLHttpRequest, new ProgressEvent('load'))
  }
}

beforeEach(() => {
  vi.resetModules()
  MockXmlHttpRequest.instances = []
  window.__FOOTBALLAI_CONFIG__ = { apiBase: '', uploadMode: 'direct' }
  vi.stubGlobal('XMLHttpRequest', MockXmlHttpRequest)
})

afterEach(() => {
  delete window.__FOOTBALLAI_CONFIG__
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('cloud direct upload', () => {
  it('authorizes, PUTs bytes to object storage, then finalizes metadata', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        run_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        upload: {
          method: 'PUT',
          url: 'https://example.blob.core.windows.net/container/object?redacted',
          headers: { 'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'video/mp4' },
          max_bytes: 1024,
          required_content_type: 'video/mp4',
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ run_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      }))
    vi.stubGlobal('fetch', fetchMock)
    const { uploadAnalysis } = await import('./api')
    const form = new FormData()
    form.set('video', new File(['tiny'], 'tiny.mp4', { type: 'video/mp4' }))
    form.set('match_name', 'Azure demo')
    form.set('data_origin', 'evaluation')
    form.set('pipeline_profile', 'demo_fast')
    const progress = vi.fn()

    const result = await uploadAnalysis(form, progress)

    expect(result.run_id).toBe('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/uploads/authorize')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/uploads/finalize')
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
      match_name: 'Azure demo', data_origin: 'evaluation', pipeline_profile: 'demo_fast',
    })
    expect(MockXmlHttpRequest.instances).toHaveLength(1)
    expect(MockXmlHttpRequest.instances[0]).toMatchObject({
      method: 'PUT',
      headers: { 'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'video/mp4' },
    })
    expect(MockXmlHttpRequest.instances[0].body).toBeInstanceOf(File)
    expect(progress).toHaveBeenLastCalledWith(100)
  })
})
