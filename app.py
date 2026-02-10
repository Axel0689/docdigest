from flask import Flask, render_template, request, jsonify
from google import genai
from google.genai import types
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
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Configura Gemini
print("Configurazione Google Gemini API...")
# Usa la key dedicata a DocDigest
api_key = os.getenv('DOCDIGEST_GEMINI_KEY') or os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=api_key)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def read_txt_file(filepath):
    """Legge file TXT"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

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

def generate_summary(text, max_words=150, language="auto"):
    """Genera riassunto usando Gemini"""
    
    lang_instruction = ""
    if language == "it":
        lang_instruction = "Rispondi in italiano."
    elif language == "en":
        lang_instruction = "Respond in English."
    
    prompt = f"""{lang_instruction}
Riassumi il seguente testo in modo chiaro e conciso in circa {max_words} parole.
Mantieni i punti chiave e le informazioni più importanti.
Non aggiungere commenti o introduzioni, fornisci solo il riassunto.

TESTO DA RIASSUMERE:
{text}

RIASSUNTO:"""
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return response.text.strip()
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
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"Errore nella traduzione: {str(e)}"
    
# Route per la pagina principale
@app.route('/')
def index():
    return render_template('index.html')

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
        filename = file.filename
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Ottieni parametri
        max_words = int(request.form.get('length', 150))
        ui_language = request.form.get('ui_language', 'it')
        
        print(f"DEBUG: Lunghezza richiesta = {max_words}")
        print(f"DEBUG: File caricato = {filename}")
        print(f"DEBUG: Lingua UI = {ui_language}")
        
        # Leggi file
        text = read_file(filepath)
        
        if text.startswith("Errore"):
            os.remove(filepath)
            return jsonify({'error': text}), 400
        
        original_word_count = len(text.split())
        print(f"DEBUG: Testo estratto - {original_word_count} parole")
        
        # Genera riassunto nella lingua dell'UI
        summary = generate_summary(text, max_words=max_words, language=ui_language)
        
        actual_summary_word_count = len(summary.split())
        
        print(f"DEBUG: Parole originali = {original_word_count}")
        print(f"DEBUG: Parole riassunto = {actual_summary_word_count}")
        
        # Rimuovi il file dopo l'elaborazione
        os.remove(filepath)
        
        result = {
            'original_length': original_word_count,
            'summary': summary,
            'summary_length': actual_summary_word_count
        }
        
        return jsonify(result)
    
    return jsonify({'error': 'Formato file non supportato'}), 400

@app.route('/translate', methods=['POST'])
@limiter.limit("15 per day")
@limiter.limit("5 per hour")
def translate():
    """Traduce il riassunto in un'altra lingua"""
    data = request.get_json()
    text = data.get('text', '')
    target_language = data.get('target_language', 'en')
    
    if not text:
        return jsonify({'error': 'Nessun testo da tradurre'}), 400
    
    translated = translate_text(text, target_language)
    
    return jsonify({
        'translated_text': translated,
        'target_language': target_language
    })

@app.route('/download-pdf', methods=['POST'])
def download_pdf():
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    
    data = request.get_json()
    summary = data.get('summary', '')
    custom_title = data.get('custom_title', '').strip()
    
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

if __name__ == '__main__':
    app.run(debug=True)