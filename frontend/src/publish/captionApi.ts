import api from '../services/api'

export interface CaptionVariation {
  style: string
  style_label: string
  caption: string
  hashtags: string[]
}

export interface CaptionGenerationResult {
  ok: boolean
  title: string
  topic: string
  hashtags: string[]
  best_caption: string
  variations: CaptionVariation[]
}

export interface GenerateCaptionParams {
  project_id?: string
  clip_id?: string
  title?: string
  transcript?: string
  platform?: string
  language?: string
  style?: string
  custom_prompt?: string
}

export const captionApi = {
  generate: (params: GenerateCaptionParams): Promise<CaptionGenerationResult> =>
    api.post('/publish/generate-caption', params),
}
