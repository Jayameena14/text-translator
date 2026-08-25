import os
import io
import uuid
import time
from flask import Flask, render_template, request, url_for, jsonify
from deep_translator import GoogleTranslator, MyMemoryTranslator
from gtts import gTTS
from werkzeug.utils import secure_filename

import docx
from pypdf import PdfReader

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

MAX_TEXT_LENGTH = 10000         # prevent abuse / huge translation requests (characters)
CHUNK_SIZE = 4500                # Google Translate's backend caps ~5000 chars/request
AUDIO_FILE_TTL_SECONDS = 60 * 30  # delete audio files older than 30 minutes

ALLOWED_DOC_EXTENSIONS = {'txt', 'docx', 'pdf'}
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE

# gTTS supports a slightly different set of language codes than Google
# Translate for a couple of entries (e.g. Chinese). Map any mismatches here.
GTTS_LANG_OVERRIDES = {
    "zh-CN": "zh-CN",
}

languages = {
    'af': 'Afrikaans',
    'am': 'Amharic',
    'ar': 'Arabic',
    'bg': 'Bulgarian',
    'bn': 'Bengali',
    'bs': 'Bosnian',
    'ca': 'Catalan',
    'cs': 'Czech',
    'cy': 'Welsh',
    'da': 'Danish',
    'de': 'German',
    'el': 'Greek',
    'en': 'English',
    'es': 'Spanish',
    'et': 'Estonian',
    'eu': 'Basque',
    'fi': 'Finnish',
    'fr': 'French',
    'fr-CA': 'French (Canada)',
    'gl': 'Galician',
    'gu': 'Gujarati',
    'ha': 'Hausa',
    'hi': 'Hindi',
    'hr': 'Croatian',
    'hu': 'Hungarian',
    'id': 'Indonesian',
    'is': 'Icelandic',
    'it': 'Italian',
    'iw': 'Hebrew',
    'ja': 'Japanese',
    'jw': 'Javanese',
    'km': 'Khmer',
    'kn': 'Kannada',
    'ko': 'Korean',
    'la': 'Latin',
    'lt': 'Lithuanian',
    'lv': 'Latvian',
    'ml': 'Malayalam',
    'mr': 'Marathi',
    'ms': 'Malay',
    'my': 'Myanmar (Burmese)',
    'ne': 'Nepali',
    'nl': 'Dutch',
    'no': 'Norwegian',
    'pa': 'Punjabi',
    'pl': 'Polish',
    'pt': 'Portuguese (Brazil)',
    'pt-PT': 'Portuguese (Portugal)',
    'ro': 'Romanian',
    'ru': 'Russian',
    'si': 'Sinhala',
    'sk': 'Slovak',
    'sq': 'Albanian',
    'sr': 'Serbian',
    'su': 'Sundanese',
    'sv': 'Swedish',
    'sw': 'Swahili',
    'ta': 'Tamil',
    'te': 'Telugu',
    'th': 'Thai',
    'tl': 'Filipino',
    'tr': 'Turkish',
    'uk': 'Ukrainian',
    'ur': 'Urdu',
    'vi': 'Vietnamese',
    'zh-CN': 'Chinese (Simplified)',
    'zh-TW': 'Chinese (Traditional)',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def cleanup_old_audio_files():
    """Delete audio files older than AUDIO_FILE_TTL_SECONDS so the
    static/audio folder doesn't grow forever on a long-running server."""
    now = time.time()
    try:
        for fname in os.listdir(AUDIO_DIR):
            fpath = os.path.join(AUDIO_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > AUDIO_FILE_TTL_SECONDS:
                os.remove(fpath)
    except OSError:
        pass  # best-effort cleanup, never fail the request over this


def detect_language(text):
    """Detect source language using deep_translator's single_detection
    (falls back gracefully if detection fails)."""
    try:
        return GoogleTranslator(source='auto', target='en').translate(text)
    except Exception:
        return None


def split_into_chunks(text, max_chunk_size):
    """Split long text into chunks under max_chunk_size, breaking on sentence
    boundaries (or paragraph/newline breaks) where possible so translation
    quality isn't hurt by cutting mid-sentence."""
    if len(text) <= max_chunk_size:
        return [text]

    import re
    # Split on sentence-ending punctuation followed by whitespace, keeping the punctuation.
    sentences = re.split(r'(?<=[.!?])\s+', text)

    chunks = []
    current = ""
    for sentence in sentences:
        # A single sentence longer than max_chunk_size: hard-split it.
        if len(sentence) > max_chunk_size:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(sentence), max_chunk_size):
                chunks.append(sentence[i:i + max_chunk_size])
            continue

        if len(current) + len(sentence) + 1 <= max_chunk_size:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)

    return chunks


# Signatures that show up when Google's backend returns an error/interstitial
# page instead of a real translation. deep-translator doesn't always catch
# this itself, so we detect it ourselves and treat it as a failure.
GOOGLE_ERROR_SIGNATURES = [
    "that's an error",
    "that’s an error",
    "please try again later",
    "that's all we know",
    "that’s all we know",
    "error 500",
    "error 404",
]


def looks_like_error_page(text):
    if not text:
        return False
    lowered = text.lower()
    return any(sig in lowered for sig in GOOGLE_ERROR_SIGNATURES)


def translate_one(google_translator, fallback_translator, chunk, retries=1):
    """Translate a single chunk using Google's engine first (with a couple
    of retries, since it's an unofficial scraping method and can be flaky
    for specific languages). If it keeps failing, fall back to MyMemory,
    a separate free translation service, before giving up entirely."""
    for attempt in range(retries + 1):
        try:
            result = google_translator.translate(chunk)
        except Exception:
            result = None
        if result and not looks_like_error_page(result):
            return result, None
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))  # brief backoff before retry

    # Google's engine failed after retries — try the fallback engine.
    if fallback_translator is not None:
        try:
            result = fallback_translator.translate(chunk)
            if result and not looks_like_error_page(result):
                return result, None
        except Exception:
            pass

    return None, (
        "Translation failed for this language pair after retrying with a "
        "backup translation service. This can happen with very short text "
        "under Auto Detect, or temporary issues with the translation "
        "provider. Try selecting the source language explicitly, or try "
        "again in a moment."
    )


def translate_text(text, source_language, target_language):
    """Translate text using deep-translator, with Google Translate as the
    primary engine and MyMemory as an automatic fallback if Google's engine
    fails for a given language. Automatically splits long text into chunks
    to stay under provider request limits, then rejoins the results.
    Returns (translated_text, error)."""
    try:
        src = source_language if source_language else 'auto'
        google_translator = GoogleTranslator(source=src, target=target_language)

        try:
            fallback_translator = MyMemoryTranslator(source=src, target=target_language)
        except Exception:
            # If MyMemory doesn't support this exact language pair/code,
            # just proceed without a fallback rather than failing outright.
            fallback_translator = None

        chunks = split_into_chunks(text, CHUNK_SIZE)

        translated_chunks = []
        for chunk in chunks:
            result, err = translate_one(google_translator, fallback_translator, chunk)
            if err:
                return None, err
            translated_chunks.append(result)

        return " ".join(translated_chunks), None
    except Exception as e:
        return None, f"Translation failed: {e}"


def generate_speech(text, lang_code):
    """Generate an mp3 for the given text/language using gTTS.
    Returns (filename, error). filename is relative to static/audio."""
    try:
        gtts_lang = GTTS_LANG_OVERRIDES.get(lang_code, lang_code)
        tts = gTTS(text=text, lang=gtts_lang)
        filename = f"{uuid.uuid4().hex}.mp3"
        filepath = os.path.join(AUDIO_DIR, filename)
        tts.save(filepath)
        return filename, None
    except Exception as e:
        return None, f"Audio generation failed: {e}"


def allowed_file(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set


def extract_text_from_txt(file_storage):
    raw = file_storage.read()
    try:
        return raw.decode('utf-8'), None
    except UnicodeDecodeError:
        try:
            return raw.decode('latin-1'), None
        except Exception as e:
            return None, f"Could not read text file: {e}"


def extract_text_from_docx(file_storage):
    try:
        document = docx.Document(file_storage)
        paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
        return "\n".join(paragraphs), None
    except Exception as e:
        return None, f"Could not read .docx file: {e}"


def extract_text_from_pdf(file_storage):
    try:
        reader = PdfReader(file_storage)
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        combined = "\n".join(pages_text).strip()
        if not combined:
            return None, "No selectable text found in this PDF (it may be a scanned/image-only PDF)."
        return combined, None
    except Exception as e:
        return None, f"Could not read PDF file: {e}"


def extract_text_from_document(file_storage):
    """Route a document upload to the right extractor based on extension."""
    filename = secure_filename(file_storage.filename or '')
    if not filename or not allowed_file(filename, ALLOWED_DOC_EXTENSIONS):
        return None, "Unsupported file type. Please upload a .txt, .docx, or .pdf file."

    ext = filename.rsplit('.', 1)[1].lower()
    if ext == 'txt':
        return extract_text_from_txt(file_storage)
    elif ext == 'docx':
        return extract_text_from_docx(file_storage)
    elif ext == 'pdf':
        return extract_text_from_pdf(file_storage)
    return None, "Unsupported file type."


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def translate():
    translated_text = ''
    original_language = ''
    target_language = ''
    original_text = ''
    source_language = ''
    audio_url = None
    error = None

    if request.method == 'POST':
        original_text = request.form.get('text', '').strip()
        source_language = request.form.get('source_language', '')
        target_language = request.form.get('target_language', '')

        cleanup_old_audio_files()

        # ---- Validation ----
        if not original_text:
            error = "Please enter some text to translate."
        elif len(original_text) > MAX_TEXT_LENGTH:
            error = f"Text is too long. Please limit input to {MAX_TEXT_LENGTH} characters."
        elif not target_language:
            error = "Please select a target language."
        elif source_language and source_language == target_language:
            error = "Source and target languages can't be the same."

        if not error:
            translated_text, translate_err = translate_text(
                original_text, source_language, target_language
            )

            if translate_err:
                error = translate_err
            else:
                original_language = languages.get(source_language, 'Auto-detected')

                audio_filename, audio_err = generate_speech(translated_text, target_language)
                if audio_filename:
                    audio_url = url_for('static', filename=f'audio/{audio_filename}')
                elif audio_err:
                    # Translation still succeeded; just surface the audio issue softly
                    error = audio_err

    return render_template(
        'index.html',
        languages=languages,
        translated_text=translated_text,
        original_language=original_language,
        original_text=original_text,
        source_language=source_language,
        target_language=target_language,
        audio_url=audio_url,
        error=error,
    )


@app.route('/extract-file', methods=['POST'])
def extract_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file received.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected.'}), 400

    text, error = extract_text_from_document(file)
    if error:
        return jsonify({'error': error}), 400

    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]

    return jsonify({'text': text})


@app.errorhandler(413)
def file_too_large(e):
    return jsonify({'error': 'File is too large. Maximum upload size is 5 MB.'}), 413


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=debug_mode, host='0.0.0.0', port=port)
