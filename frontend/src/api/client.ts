/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  // 上传文件时交给浏览器自动带 multipart boundary，不能手写 Content-Type。
  const isFormData = typeof FormData !== 'undefined' && init?.body instanceof FormData
  return fetch(url, {
    ...(isFormData ? {} : { headers: { 'Content-Type': 'application/json' } }),
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export function downloadUrl(path: string): string {
  return path.startsWith('http') ? path : `${API_BASE}${path}`
}
