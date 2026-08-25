# TEXTRA — Text Translation & Speech App

A Flask web app that translates text between 12 languages and generates
audio playback of the translated text.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in your browser.

## What changed from the original version

- **Translation engine**: switched from `googletrans` (unofficial, frequently
  broken) to `deep-translator`, which is actively maintained.
- **Text-to-speech**: removed `pyttsx3` (server-side audio playback, which
  does nothing useful once deployed) and standardized on `gTTS`. Audio is
  now saved to `static/audio/` with a unique filename per request and played
  back in-browser via an `<audio>` element.
- **Concurrency-safe audio**: each translation generates a uniquely named
  mp3 instead of overwriting a single shared `output.mp3`, so multiple users
  won't collide.
- **Automatic cleanup**: audio files older than 30 minutes are deleted on
  each request so the `static/audio/` folder doesn't grow unbounded.
- **Input validation**: empty text, overly long text (2000 char cap), and
  identical source/target languages are now caught with clear error
  messages instead of silently failing.
- **Deployment-ready config**: debug mode and port are now controlled via
  environment variables (`FLASK_DEBUG`, `PORT`) instead of being hardcoded.
- **UI refresh**: cleaner layout, character counter, inline error messages,
  and a working audio player tied to the actual backend-generated speech
  (previously the "Speak" button used the browser's built-in
  SpeechSynthesis and ignored the backend entirely).

## New input methods

TEXTRA now supports two ways to get text into the translator besides typing:

- **File upload** — `.txt`, `.docx`, `.pdf`. Text is extracted server-side and
  fills the textarea automatically.
- **Voice input** — click "Start Recording" and speak; uses the browser's
  built-in Web Speech API (Chrome/Edge recommended) to transcribe live,
  entirely client-side, no server round-trip needed.

Max upload size for files is 5 MB. Translation input is capped at
10,000 characters — under the hood, anything over ~4,500 characters is
automatically split into sentence-aware chunks, translated separately, and
rejoined, since Google Translate's backend has its own per-request limit.

## Translation reliability

Translation uses Google Translate's unofficial page-scraping method under
the hood (via `deep-translator`), which can occasionally fail for specific
languages or return an error page instead of a real translation. To handle
this:

- Failed Google requests are retried automatically before giving up.
- If Google's engine keeps failing, the app automatically falls back to
  **MyMemory**, a separate free translation service, so most translations
  still succeed even when Google's scraper has an off moment.
- If both engines fail, you'll see a clear error message (never raw HTML
  error text) suggesting you pick an explicit source language or try again.

## Environment variables

| Variable      | Default | Description                        |
|---------------|---------|-------------------------------------|
| `FLASK_DEBUG` | `0`     | Set to `1` to enable debug mode     |
| `PORT`        | `5000`  | Port to run the server on           |

## Deploying

This app is now stateless-friendly and safe to deploy on platforms like
Render, Railway, or PythonAnywhere. Just make sure the `static/audio`
directory is writable, and consider adding a scheduled task/cron to purge
that folder if you expect heavy traffic (the built-in cleanup only runs on
request).
