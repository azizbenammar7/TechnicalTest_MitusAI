import { useEffect, useState } from 'react'

declare global {
  interface Window {
    __FOOTBALLAI_CONFIG__?: { apiBase?: string | null }
  }
}

const API_BASE = window.__FOOTBALLAI_CONFIG__?.apiBase ?? import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

export class ApiError extends Error {
  constructor(message: string, public status?: number) {
    super(message)
  }
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) message = payload.detail
    } catch {
      // A bounded public message is sufficient when the server did not return JSON.
    }
    throw new ApiError(message, response.status)
  }
  return response.json() as Promise<T>
}

export async function apiPost<T>(path: string, body?: BodyInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { method: 'POST', body, headers: body instanceof FormData ? undefined : { Accept: 'application/json' } })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string | { message?: string } }
    const detail = typeof payload.detail === 'string' ? payload.detail : payload.detail?.message
    throw new ApiError(detail ?? `Request failed (${response.status})`, response.status)
  }
  return response.json() as Promise<T>
}

export function uploadAnalysis(form: FormData, onProgress: (percent: number) => void): Promise<{ run_id: string }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open('POST', `${API_BASE}/api/v1/analyses`)
    request.setRequestHeader('Accept', 'application/json')
    request.upload.onprogress = (event) => event.lengthComputable && onProgress(Math.round(event.loaded / event.total * 100))
    request.onload = () => {
      let payload: { run_id?: string; detail?: string | { message?: string } } = {}
      try { payload = JSON.parse(request.responseText) as typeof payload } catch { /* safe fallback below */ }
      if (request.status >= 200 && request.status < 300 && payload.run_id) resolve({ run_id: payload.run_id })
      else reject(new ApiError(typeof payload.detail === 'string' ? payload.detail : payload.detail?.message ?? `Upload failed (${request.status})`, request.status))
    }
    request.onerror = () => reject(new ApiError('The local API could not be reached.'))
    request.send(form)
  })
}

export function useApi<T>(path: string | null) {
  const [state, setState] = useState<{ path: string | null; data: T | null; error: string | null }>({
    path: null,
    data: null,
    error: null,
  })

  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    apiGet<T>(path, controller.signal)
      .then((data) => setState({ path, data, error: null }))
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setState({
          path,
          data: null,
          error: reason instanceof Error ? reason.message : 'The local API could not be reached.',
        })
      })
    return () => controller.abort()
  }, [path])

  const current = state.path === path
  return {
    data: current ? state.data : null,
    loading: Boolean(path) && !current,
    error: current ? state.error : null,
  }
}
