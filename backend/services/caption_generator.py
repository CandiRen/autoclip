"""
Automatic Caption and Hashtag Generator Service for AutoClip.
Generates engaging, relevant, and high-quality captions and hashtags for social platforms
(Threads, Instagram Reels, TikTok, YouTube Shorts, Facebook, Shopee, etc.).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_POST_PATH = Path("/home/cr/project/post/.env")

STYLE_DEFINITIONS = {
    "viral": "Viral & Catchy: Hook kuat dan bikin penasaran di 3 detik pertama, bahasa gaul/santai, ajakan interaksi/komentar.",
    "storytelling": "Storytelling & Relatable: Gaya bercerita mengalir seperti pengalaman nyata, emosional atau lucu, menyentuh sisi manusiawi pembaca.",
    "educational": "Edukasi & Poin Penting: To-the-point, menjelaskan ringkasan isi dan manfaat utama, terstruktur dengan bullet points atau emoji.",
    "punchy": "Short & Punchy: Singkat, padat, estetik, langsung pada inti pesan, sangat cocok untuk video cepat.",
}

PLATFORM_PROMPTS = {
    "threads": "Threads: Gaya tulisan santai, mengundang obrolan/diskusi, kalimat pembuka memancing rasa ingin tahu, format naratif tanpa basa-basi.",
    "instagram": "Instagram Reels & Feed: Hook visual, deskripsi estetik, spacing rapi, call-to-action untuk save & share.",
    "tiktok": "TikTok: Cepat, hook punchy, bahasa tren/slang yang wajar, ajakan follow/duet/komentar.",
    "youtube": "YouTube Shorts: Judul searchable, deskripsi informatif dan padat yang menjelaskan highlight klip.",
    "facebook": "Facebook: Deskripsi ramah, memancing sharing ke teman/komunitas, format paragraf teratur.",
}


@dataclass
class CaptionVariation:
    style: str
    style_label: str
    caption: str
    hashtags: list[str] = field(default_factory=list)
    topic: str = ""


@dataclass
class CaptionGenerationResult:
    title: str
    topic: str
    hashtags: list[str]
    variations: list[CaptionVariation]
    best_caption: str


def clean_text(text: Any) -> str:
    """Sanitize and clean text, removing excessive whitespace and html tags."""
    if not text:
        return ""
    val = html.unescape(str(text))
    val = re.sub(r"<[^>]+>", "", val)
    val = re.sub(r"\s+", " ", val).strip()
    return val


def clean_product_links(text: str) -> str:
    """Strip affiliate or promotional raw links if undesirable."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\b(link di bio|check link|cek tautan|link produk)\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_hashtags_from_text(text: str) -> list[str]:
    """Find all hashtags in a text block."""
    matches = re.findall(r"#([a-zA-Z0-9_\u0080-\uffff]+)", text)
    return [f"#{m}" for m in dict.fromkeys(matches)]


class CaptionGenerator:
    """Service to automatically generate relevant, high-quality captions and hashtags."""

    def __init__(self):
        self._gemini_api_key = self._find_gemini_key()

    def _find_gemini_key(self) -> str | None:
        key = os.getenv("GEMINI_API_KEY")
        if key:
            return key
        if ENV_POST_PATH.exists():
            try:
                for line in ENV_POST_PATH.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"\'')
                        if val:
                            return val
            except Exception:
                pass
        return None

    def _call_gemini_direct(self, prompt: str) -> dict[str, Any] | None:
        """Call Gemini API directly if key is available."""
        if not self._gemini_api_key:
            return None
        import requests

        models = [
            "gemini-flash-lite-latest",
            "gemini-3.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-flash-latest",
            "gemini-pro-latest",
        ]
        for m in models:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={self._gemini_api_key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.75},
                }
                resp = requests.post(url, json=payload, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_out = data["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(raw_out)
            except Exception as e:
                logger.debug("Gemini direct call model %s skipped: %s", m, e)
        return None

    def _call_autoclip_llm(self, prompt: str, user_context: dict) -> dict[str, Any] | None:
        """Call AutoClip's configured LLM (OpenAI/Gemini/Dashscope/Ollama/etc)."""
        try:
            from backend.utils.llm_client import LLMClient

            client = LLMClient()
            raw = client.call_with_retry(prompt, user_context)
            if raw:
                return client.parse_json_response(raw)
        except Exception as e:
            logger.debug("AutoClip LLMClient call failed: %s", e)
        return None

    def generate(
        self,
        *,
        title: str = "",
        transcript: str = "",
        platform: str = "threads",
        language: str = "id",
        custom_prompt: str = "",
        clip_outline: str = "",
    ) -> CaptionGenerationResult:
        """Generate multiple styles of captions, relevant hashtags, and topic."""
        clean_title = clean_text(title)
        clean_transcript = clean_text(transcript)[:4000]
        clean_outline = clean_text(clip_outline)
        platform_key = platform.lower()
        platform_info = PLATFORM_PROMPTS.get(platform_key, PLATFORM_PROMPTS["threads"])

        lang_instruction = (
            "Gunakan Bahasa Indonesia yang alami, luwes, dan kekinian (bukan kaku seperti terjemahan mesin)."
            if language == "id"
            else f"Gunakan bahasa {language}."
        )

        custom_block = f'\n⚠️ INSTRUKSI KHUSUS PENGGUNA:\n"{custom_prompt.strip()}"\n' if custom_prompt.strip() else ""

        system_prompt = f"""
Kamu adalah Social Media Specialist dan Copywriter Viral profesional.
Tugasmu adalah membuat caption media sosial dan kumpulan hashtag yang sangat relevan, berbobot, dan menarik berdasarkan video/klip berikut.

Platform Target: {platform_info}
Bahasa: {lang_instruction}
{custom_block}

Informasi Konten:
- Judul / Topik: {clean_title}
- Garis Besar (Outline): {clean_outline}
- Transkrip Percakapan / Isi Video:
\"\"\"{clean_transcript}\"\"\"

Instruksi:
1. Pahami pesan inti, emosi, dan momen paling menarik dari konten.
2. Tentukan satu 'topic' (topik payung pendek, 2-4 kata, misal: 'Karir & Finansial', 'Overthinking Malam', 'Tips Produktivitas').
3. Buat kumpulan 6-10 'hashtags' yang SANGAT RELEVAN (berkaitan langsung dengan topik, industri, dan kata kunci viral). Hashtags harus diawali tanda pagar (#).
4. Buat 4 variasi caption:
   - "viral": Gaya Viral & Hook kuat di kalimat pertama.
   - "storytelling": Gaya Bercerita / Relatable.
   - "educational": Gaya Tips & Insight penting.
   - "punchy": Gaya Singkat, Padat, dan Estetik.
5. Pada akhir setiap variasi caption, sertakan hashtag yang relevan secara natural.

Format JSON Wajib:
{{
  "topic": "Topik Singkat",
  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5", "#tag6"],
  "variations": [
    {{
      "style": "viral",
      "style_label": "Viral & Catchy",
      "caption": "Teks lengkap caption variasi viral termasuk hashtag..."
    }},
    {{
      "style": "storytelling",
      "style_label": "Storytelling & Relatable",
      "caption": "Teks lengkap caption variasi storytelling..."
    }},
    {{
      "style": "educational",
      "style_label": "Tips & Insight",
      "caption": "Teks lengkap caption variasi edukatif..."
    }},
    {{
      "style": "punchy",
      "style_label": "Short & Punchy",
      "caption": "Teks lengkap caption singkat..."
    }}
  ]
}}
"""

        # Try Gemini direct first if key exists, otherwise use AutoClip LLMClient
        parsed_result = None
        user_payload = {
            "title": clean_title,
            "transcript": clean_transcript,
            "platform": platform,
            "language": language,
        }

        if self._gemini_api_key:
            parsed_result = self._call_gemini_direct(system_prompt)

        if not parsed_result or not isinstance(parsed_result, dict) or "variations" not in parsed_result:
            try:
                parsed_result = self._call_autoclip_llm(system_prompt, user_payload)
            except Exception:
                parsed_result = None

        # Fallback heuristic generator if LLM offline
        if not parsed_result or not isinstance(parsed_result, dict) or "variations" not in parsed_result:
            parsed_result = self._heuristic_fallback(clean_title, clean_transcript, platform_key)

        # Parse variations
        variations = []
        raw_vars = parsed_result.get("variations", [])
        if isinstance(raw_vars, list):
            for v in raw_vars:
                if isinstance(v, dict) and "caption" in v:
                    style_key = v.get("style", "viral")
                    s_label = v.get("style_label", STYLE_DEFINITIONS.get(style_key, style_key.title()))
                    cap = clean_product_links(str(v["caption"])).strip()
                    extracted_tags = extract_hashtags_from_text(cap)
                    variations.append(
                        CaptionVariation(
                            style=style_key,
                            style_label=s_label,
                            caption=cap,
                            hashtags=extracted_tags,
                        )
                    )

        if not variations:
            # Minimal guarantee
            sample_caption = f"🔥 {clean_title or 'Highlight menarik hari ini!'}\n\nSimak video lengkapnya untuk insight selengkapnya! ✨\n\n#autoclip #shorts #trending #viral"
            variations.append(CaptionVariation(style="viral", style_label="Viral & Catchy", caption=sample_caption, hashtags=["#autoclip", "#shorts", "#trending", "#viral"]))

        raw_tags = parsed_result.get("hashtags", [])
        clean_tags = []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                tag = str(t).strip()
                if not tag.startswith("#"):
                    tag = f"#{tag}"
                if tag and tag not in clean_tags:
                    clean_tags.append(tag)

        if not clean_tags and variations:
            clean_tags = variations[0].hashtags

        topic = clean_text(parsed_result.get("topic") or "Trending Clip")
        best = variations[0].caption if variations else ""

        return CaptionGenerationResult(
            title=clean_title,
            topic=topic,
            hashtags=clean_tags,
            variations=variations,
            best_caption=best,
        )

    def _heuristic_fallback(self, title: str, transcript: str, platform: str) -> dict[str, Any]:
        """Offline fallback if no LLM endpoint responded."""
        base_title = title.strip() or "Highlight Menarik Hari Ini"
        words = re.findall(r"\b[A-Za-z0-9_]{4,}\b", transcript)
        suggested_tags = [f"#{w.lower()}" for w in list(dict.fromkeys(words))[:6]]
        if not suggested_tags:
            suggested_tags = ["#trending", "#viral", "#foryou", "#video", "#insight"]

        tags_str = " ".join(suggested_tags)
        return {
            "topic": "Highlight & Insight",
            "hashtags": suggested_tags,
            "variations": [
                {
                    "style": "viral",
                    "style_label": "Viral & Catchy",
                    "caption": f"Gak nyangka bagian ini relate banget! 😱\n\n\"{base_title}\"\n\nMenurut kalian gimana? Coba tulis pendapat kalian di bawah! 👇\n\n{tags_str}",
                },
                {
                    "style": "storytelling",
                    "style_label": "Storytelling & Relatable",
                    "caption": f"Pernah gak sih kalian ngerasa momen kayak gini?\n\n{base_title}.\n\nKadang hal-hal kayak gini baru kita sadari setelah nonton sampai habis. Semoga bermanfaat ya! 🤍\n\n{tags_str}",
                },
                {
                    "style": "educational",
                    "style_label": "Tips & Insight",
                    "caption": f"📌 Poin penting dari pembahasan ini:\n• {base_title}\n\nSimpan postingan ini biar gak lupa! 🔖\n\n{tags_str}",
                },
                {
                    "style": "punchy",
                    "style_label": "Short & Punchy",
                    "caption": f"{base_title} ✨ Simak selengkapnya di video!\n\n{tags_str}",
                },
            ],
        }


# Singleton instance
caption_generator = CaptionGenerator()
