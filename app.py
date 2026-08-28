from flask import Flask, render_template, request, jsonify
from google import genai
import os
from dotenv import load_dotenv
from PyPDF2 import PdfReader
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import inch
from io import BytesIO
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import tempfile
import time
import uuid
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import ipaddress
import socket


# Carica variabili d'ambiente
load_dotenv()

app = Flask(__name__)

# Rate Limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["5 per day"],
    storage_uri="memory://",
    headers_enabled=True  # Invia header con info limite
)

# Configurazione
UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Limite dimensione richiesta (upload) per prevenire DoS: 5 MB
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
# Tetto di caratteri inviati al modello, per contenere costi/latenza
MAX_TEXT_CHARS = 200_000
DEFAULT_SUMMARY_WORDS = 150
MIN_SUMMARY_WORDS = 50
MAX_SUMMARY_WORDS = 300

# Configura Gemini
print("Configurazione Google Gemini API...")
# Usa la key dedicata a DocDigest
api_key = os.getenv('DOCDIGEST_GEMINI_KEY') or os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=api_key)

def _gemini_generate(prompt, retries=3):
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            is_transient = '503' in str(e) or 'UNAVAILABLE' in str(e)
            if is_transient and attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s
                continue
            raise

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_max_words(raw_value):
    """Converte in intero e limita la lunghezza richiesta per il riassunto."""
    try:
        max_words = int(raw_value)
    except (TypeError, ValueError):
        max_words = DEFAULT_SUMMARY_WORDS
    return max(MIN_SUMMARY_WORDS, min(max_words, MAX_SUMMARY_WORDS))

def normalize_summary_format(raw_format):
    """Restituisce un formato di riassunto valido, con fallback a 'paragraph'."""
    return raw_format if raw_format in ('paragraph', 'bullet') else 'paragraph'

def read_txt_file(filepath):
    """Legge file TXT"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        return "Errore nella lettura del TXT: il file non è codificato in UTF-8 valido"

def read_pdf_file(filepath):
    """Legge file PDF"""
    try:
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text.strip()
    except Exception as e:
        return f"Errore nella lettura del PDF: {str(e)}"

def read_docx_file(filepath):
    """Legge file DOCX"""
    try:
        doc = Document(filepath)
        text = ""
        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"
        return text.strip()
    except Exception as e:
        return f"Errore nella lettura del DOCX: {str(e)}"

def read_file(filepath):
    """Legge file in base all'estensione"""
    ext = filepath.rsplit('.', 1)[1].lower()
    
    if ext == 'txt':
        return read_txt_file(filepath)
    elif ext == 'pdf':
        return read_pdf_file(filepath)
    elif ext == 'docx':
        return read_docx_file(filepath)
    else:
        return "Formato file non supportato"

def _is_public_ip(host):
    """Verifica che l'host non risolva a un IP privato/loopback/link-local (protezione SSRF)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True

def is_safe_url(url):
    """Valida schema http/https e blocca URL che puntano a IP privati/interni."""
    try:
        result = urlparse(url)
    except Exception:
        return False

    if result.scheme not in ('http', 'https') or not result.netloc:
        return False

    host = result.hostname
    if not host:
        return False

    return _is_public_ip(host)

def extract_text_from_url(url):
    """Scarica una pagina web ed estrae il testo leggibile dell'articolo."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=8, allow_redirects=False)

        # Segui manualmente al massimo un redirect, ri-validando la destinazione
        # per evitare che un URL "sicuro" reindirizzi verso un host interno (SSRF).
        redirect_hops = 0
        while response.is_redirect and redirect_hops < 3:
            location = response.headers.get('Location')
            if not location or not is_safe_url(location):
                return None
            response = requests.get(location, headers=headers, timeout=8, allow_redirects=False)
            redirect_hops += 1

        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        for tag in soup(['script', 'style', 'nav', 'footer',
                          'header', 'aside', 'form', 'iframe']):
            tag.decompose()

        article = soup.find('article')
        container = article if article else soup.body

        if not container:
            return None

        paragraphs = container.find_all('p')
        text = '\n'.join(
            p.get_text(strip=True) for p in paragraphs
            if len(p.get_text(strip=True)) > 40
        )

        return text.strip() if text.strip() else None

    except requests.exceptions.RequestException:
        return None
    except Exception:
        return None

def generate_summary(text, max_words=150, language="auto", summary_format="paragraph"):
    """Genera riassunto usando Gemini"""

    lang_instruction = ""
    if language == "it":
        lang_instruction = "Rispondi in italiano."
    elif language == "en":
        lang_instruction = "Respond in English."

    if summary_format == "bullet":
        prompt = f"""{lang_instruction}
Estrai i concetti chiave dal seguente testo come lista di punti chiave.
Genera tra 5 e 10 punti chiave. Ogni punto deve essere conciso (massimo 20 parole).
Non aggiungere introduzioni o conclusioni, fornisci solo la lista di punti chiave.
Usa il formato: - punto chiave

TESTO:
{text}

PUNTI CHIAVE:"""
    else:
        prompt = f"""{lang_instruction}
Riassumi il seguente testo in modo chiaro e conciso in circa {max_words} parole.
Mantieni i punti chiave e le informazioni più importanti.
Non aggiungere commenti o introduzioni, fornisci solo il riassunto.

TESTO DA RIASSUMERE:
{text}

RIASSUNTO:"""

    try:
        return _gemini_generate(prompt)
    except Exception as e:
        return f"Errore nella generazione: {str(e)}"

def translate_text(text, target_language):
    """Traduce testo usando Gemini"""
    
    languages = {
        'en': 'inglese',
        'it': 'italiano',
        'es': 'spagnolo',
        'fr': 'francese',
        'de': 'tedesco'
    }
    
    target_lang_name = languages.get(target_language, 'inglese')
    
    prompt = f"""Traduci il seguente testo in {target_lang_name}.
Mantieni il tono, lo stile e la formattazione del testo originale.
Non aggiungere commenti, fornisci solo la traduzione.

TESTO DA TRADURRE:
{text}

TRADUZIONE:"""
    
    try:
        return _gemini_generate(prompt)
    except Exception as e:
        return f"Errore nella traduzione: {str(e)}"
    
# Route per la pagina principale
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/legal')
def legal():
    return render_template('legal.html')

@app.route('/summarize', methods=['POST'])
@limiter.limit("5 per day")
@limiter.limit("3 per hour")
def summarize():
    if 'file' not in request.files:
        return jsonify({'error': 'Nessun file caricato'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'Nome file vuoto'}), 400
    
    if file and allowed_file(file.filename):
        # Sanifica il nome file e genera un nome univoco lato server:
        # non ci si fida mai del filename fornito dal client (rischio path traversal).
        safe_original = secure_filename(file.filename)
        ext = safe_original.rsplit('.', 1)[1].lower() if '.' in safe_original else ''
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'error': 'Formato file non supportato'}), 400

        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        # Difesa in profondità: verifica che il path risolto resti dentro UPLOAD_FOLDER
        upload_root = os.path.realpath(app.config['UPLOAD_FOLDER'])
        resolved_path = os.path.realpath(filepath)
        if os.path.commonpath([upload_root, resolved_path]) != upload_root:
            return jsonify({'error': 'Nome file non valido'}), 400

        max_words = parse_max_words(request.form.get('length', DEFAULT_SUMMARY_WORDS))
        ui_language = request.form.get('ui_language', 'it')
        summary_format = normalize_summary_format(request.form.get('format', 'paragraph'))

        try:
            file.save(filepath)

            # Leggi file
            text = read_file(filepath)

            if text.startswith("Errore"):
                return jsonify({'error': text}), 400

            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]

            original_word_count = len(text.split())

            # Genera riassunto nella lingua dell'UI
            summary = generate_summary(text, max_words=max_words, language=ui_language, summary_format=summary_format)

            actual_summary_word_count = len(summary.split())

            result = {
                'original_length': original_word_count,
                'summary': summary,
                'summary_length': actual_summary_word_count,
                'format': summary_format
            }

            return jsonify(result)
        finally:
            # Rimuovi sempre il file temporaneo, anche in caso di eccezione
            if os.path.exists(filepath):
                os.remove(filepath)

    return jsonify({'error': 'Formato file non supportato'}), 400

@app.route('/summarize-url', methods=['POST'])
@limiter.limit("5 per day")
@limiter.limit("3 per hour")
def summarize_url():
    data = request.get_json(silent=True) or {}
    url = (data.get('url', '') or '').strip()

    if not url:
        return jsonify({'error': 'URL mancante'}), 400

    if not is_safe_url(url):
        return jsonify({'error': 'URL non valido'}), 400

    max_words = parse_max_words(data.get('length', DEFAULT_SUMMARY_WORDS))
    ui_language = data.get('ui_language', 'it')
    summary_format = normalize_summary_format(data.get('format', 'paragraph'))

    text = extract_text_from_url(url)

    if not text or len(text.split()) < 30:
        return jsonify({'error': 'Impossibile estrarre contenuto sufficiente da questa pagina'}), 400

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    original_word_count = len(text.split())

    summary = generate_summary(text, max_words=max_words, language=ui_language, summary_format=summary_format)

    actual_summary_word_count = len(summary.split())

    return jsonify({
        'original_length': original_word_count,
        'summary': summary,
        'summary_length': actual_summary_word_count,
        'format': summary_format,
        'source_url': url
    })

@app.route('/translate', methods=['POST'])
@limiter.limit("15 per day")
@limiter.limit("5 per hour")
def translate():
    """Traduce il riassunto in un'altra lingua"""
    data = request.get_json(silent=True) or {}
    text = data.get('text', '') or ''
    target_language = data.get('target_language', 'en')

    if not text:
        return jsonify({'error': 'Nessun testo da tradurre'}), 400

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    translated = translate_text(text, target_language)
    
    return jsonify({
        'translated_text': translated,
        'target_language': target_language
    })

@app.route('/download-pdf', methods=['POST'])
@limiter.limit("15 per day")
@limiter.limit("5 per hour")
def download_pdf():
    from reportlab.lib import colors

    data = request.get_json(silent=True) or {}
    summary = (data.get('summary', '') or '')[:MAX_TEXT_CHARS]
    custom_title = (data.get('custom_title', '') or '').strip()[:200]
    summary_format = normalize_summary_format(data.get('format', 'paragraph'))
    
    # Crea PDF in memoria
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=50, bottomMargin=50)
    story = []
    
    # Stili
    styles = getSampleStyleSheet()
    
    # Stile titolo personalizzato
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#2563eb'),
        spaceAfter=20,
        alignment=1,  # Centrato
        fontName='Helvetica-Bold'
    )
    
    # Stile sottotitolo
    subtitle_style = ParagraphStyle(
        'Subtitle',
        parent=styles['Normal'],
        fontSize=12,
        textColor=colors.HexColor('#6b7280'),
        spaceAfter=30,
        alignment=1,  # Centrato
    )
    
    # Stile corpo
    body_style = ParagraphStyle(
        'BodyText',
        parent=styles['BodyText'],
        fontSize=11,
        leading=18,
        spaceAfter=20,
    )
    
    # Stile firma
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.HexColor('#9ca3af'),
        alignment=1,  # Centrato
        spaceBefore=30,
    )
    
    # Aggiungi titolo personalizzato se presente
    if custom_title:
        title = Paragraph(custom_title, title_style)
        story.append(title)
    else:
        title = Paragraph("Riassunto Documento", title_style)
        story.append(title)
    
    # Aggiungi sottotitolo
    subtitle = Paragraph("Generato con DocDigest AI", subtitle_style)
    story.append(subtitle)
    story.append(Spacer(1, 0.3*inch))
    
    # Aggiungi riassunto
    if summary_format == 'bullet':
        from reportlab.platypus import ListFlowable, ListItem
        import re
        lines = [re.sub(r'^[-•*▸]\s*', '', line).strip()
                 for line in summary.split('\n') if line.strip()]
        bullet_items = [ListItem(Paragraph(line, body_style)) for line in lines if line]
        body = ListFlowable(bullet_items, bulletType='bullet', start='•', leftIndent=20)
    else:
        body = Paragraph(summary.replace('\n', '<br/>'), body_style)
    story.append(body)
    
    # Aggiungi spazio prima della firma
    story.append(Spacer(1, 0.5*inch))
    
    # Aggiungi linea separatore
    from reportlab.platypus import HRFlowable
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e5e7eb')))
    
    # Aggiungi firma
    footer_text = """
    <b>Riassunto generato con DocDigest</b><br/>
    Powered by AI
    """
    footer = Paragraph(footer_text, footer_style)
    story.append(footer)
    
    # Genera PDF
    doc.build(story)
    buffer.seek(0)
    
    return buffer.getvalue(), 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': 'attachment; filename=docdigest_summary.pdf'
    }

@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer-when-downgrade'
    return response


if __name__ == '__main__':
    # Il debugger interattivo di Werkzeug espone RCE se raggiungibile: mai in produzione.
    debug_mode = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug_mode)