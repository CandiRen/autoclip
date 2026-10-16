import api from '../services/api'

export interface ReplizConfigView {
  configured: boolean
  source: string
  access_key_masked: string
  secret_key_masked: string
  default_account_id?: string | null
  note?: string
}

export interface ReplizAccount {
  id?: string
  _id?: string
  name: string
  username: string
  type: string
  picture?: string
  isConnected: boolean
}

export interface ReplizJobView {
  job_id: string
  status: 'queued' | 'running' | 'submitted' | 'failed' | string
  percent?: number
  schedule_id?: string | null
  error?: string
  result?: any
}

export interface ReplizPublishBody {
  account_id: string
  post_type: 'video' | 'text' | 'image' | 'reel' | 'album' | 'link' | string
  title?: string
  description?: string
  topic?: string
  scheduled_date?: string
  media_url?: string
  reply_text?: string
  replies?: Array<{ type: string; description: string }>
  subtitles?: boolean
  title_card?: boolean
}

export interface ReplizRecord {
  schedule_id: string
  job_id?: string
  project_id: string
  clip_id: string
  account_id: string
  type: string
  title?: string
  description?: string
  topic?: string
  schedule_at?: string
  status?: string
  submitted_at?: string
}

export const replizApi = {
  getConfig: (): Promise<ReplizConfigView> => api.get('/publish/repliz/config'),
  saveConfig: (body: {
    access_key?: string
    secret_key?: string
    default_account_id?: string
  }): Promise<ReplizConfigView & { ok: boolean; account_info?: any; note?: string }> =>
    api.put('/publish/repliz/config', body),
  clearConfig: (): Promise<{ ok: boolean; configured: boolean }> => api.delete('/publish/repliz/config'),
  getAccounts: (): Promise<{ accounts: ReplizAccount[] }> => api.get('/publish/repliz/accounts'),
  getAccountStats: (accountId: string): Promise<any> => api.get(`/publish/repliz/accounts/${accountId}/stats`),
  getSchedules: (accountId?: string, page = 1, limit = 50): Promise<any> => {
    const params = new URLSearchParams()
    if (accountId) params.set('account_id', accountId)
    params.set('page', String(page))
    params.set('limit', String(limit))
    return api.get(`/publish/repliz/schedules?${params.toString()}`)
  },
  start: (projectId: string, clipId: string, body: ReplizPublishBody): Promise<{ ok: boolean; job_id: string; status: string }> =>
    api.post(`/publish/repliz/${projectId}/clips/${clipId}`, body),
  job: (jobId: string): Promise<ReplizJobView> => api.get(`/publish/repliz/jobs/${jobId}`),
  records: (projectId: string): Promise<{ records: ReplizRecord[] }> => api.get(`/publish/repliz/${projectId}/records`),
  cancel: (projectId: string, scheduleId: string): Promise<{ ok: boolean; status: string }> =>
    api.delete(`/publish/repliz/${projectId}/records/${scheduleId}`),
  retry: (scheduleId: string): Promise<any> => api.post(`/publish/repliz/schedules/${scheduleId}/retry`),
  deleteSchedule: (scheduleId: string): Promise<any> => api.delete(`/publish/repliz/schedules/${scheduleId}`),
  searchThreads: (accountId: string, query: string): Promise<any> =>
    api.get(`/publish/repliz/threads/search?account_id=${encodeURIComponent(accountId)}&query=${encodeURIComponent(query)}`),
  getTiktokTrends: (accountId: string, genre?: string, country?: string): Promise<any> => {
    const params = new URLSearchParams({ account_id: accountId })
    if (genre) params.set('genre', genre)
    if (country) params.set('country', country)
    return api.get(`/publish/repliz/trends/tiktok?${params.toString()}`)
  },
}
