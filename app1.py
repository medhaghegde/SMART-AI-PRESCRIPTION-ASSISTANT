from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import json
import io
import sqlite3
import uuid
from datetime import datetime
from werkzeug.utils import secure_filename
from PIL import Image
from google import genai
from google.genai import types
from gtts import gTTS

# =========================================================
# GEMINI SETUP
# =========================================================

client = genai.Client(
    api_key="AQ.Ab8RN6JXOoVa-1ANX_PYK1he66-vefGvvURMSlPBd_2HsZ_s5w"
)

# =========================================================
# FLASK SETUP
# =========================================================

app = Flask(__name__)
CORS(app)

# =========================================================
# CONFIGURATION
# =========================================================

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

UPLOAD_FOLDER = 'uploads'
MEDICINE_FOLDER = os.path.join(UPLOAD_FOLDER, 'medicines')
AUDIO_FOLDER = os.path.join(UPLOAD_FOLDER, 'audio')

os.makedirs(MEDICINE_FOLDER, exist_ok=True)
os.makedirs(AUDIO_FOLDER, exist_ok=True)

# =========================================================
# ROBUST IMAGE CONVERSION FUNCTION
# =========================================================

def convert_to_rgb_and_save(image_bytes, filepath, quality=85):
    """
    Convert ANY image to RGB mode and save as JPEG
    This handles RGBA, P, and other modes properly
    """
    try:
        # Open the image
        img = Image.open(io.BytesIO(image_bytes))
        
        # Print original mode for debugging
        print(f"Original image mode: {img.mode}")
        
        # Convert to RGB based on mode
        if img.mode == 'RGBA':
            # Create a white background RGB image
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            # Paste using alpha channel as mask if available
            rgb_img.paste(img, mask=img.split()[-1] if len(img.split()) == 4 else None)
            img = rgb_img
        elif img.mode == 'LA':  # Grayscale with alpha
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1] if len(img.split()) == 2 else None)
            img = rgb_img
        elif img.mode == 'P':  # Palette mode
            img = img.convert('RGB')
        elif img.mode == 'L':  # Grayscale
            img = img.convert('RGB')
        elif img.mode == '1':  # 1-bit pixels
            img = img.convert('RGB')
        elif img.mode != 'RGB':
            # For any other mode, try direct conversion
            img = img.convert('RGB')
        
        # Verify mode is now RGB
        print(f"Converted image mode: {img.mode}")
        
        # Save as JPEG
        img.save(filepath, 'JPEG', quality=quality, optimize=True)
        return True
        
    except Exception as e:
        print(f"Error converting image: {str(e)}")
        # Fallback: try to convert by creating a new RGB image
        try:
            img = Image.open(io.BytesIO(image_bytes))
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img)
            rgb_img.save(filepath, 'JPEG', quality=quality)
            return True
        except:
            raise e

# =========================================================
# DATABASE SETUP
# =========================================================

def init_db():
    conn = sqlite3.connect('prescriptions.db')
    c = conn.cursor()

    c.execute('''
    CREATE TABLE IF NOT EXISTS patients (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        local_language TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS prescriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT,
        prescription_image TEXT,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (patient_id) REFERENCES patients (id)
    )
    ''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS medicines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT,
        prescription_id INTEGER,
        name TEXT NOT NULL,
        timing_pattern TEXT,
        instruction TEXT,
        translated_instruction TEXT,
        medicine_image TEXT,
        audio_path TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (patient_id) REFERENCES patients (id),
        FOREIGN KEY (prescription_id) REFERENCES prescriptions (id)
    )
    ''')

    conn.commit()
    conn.close()

init_db()

# =========================================================
# HELPERS
# =========================================================

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# =========================================================
# TIMING PATTERN VALIDATION AND CORRECTION
# =========================================================

def validate_and_correct_timing(pattern, medicine_name=""):
    """
    Validate and correct common OCR/extraction mistakes
    Specifically handles 1-1-1 being misread as 1-0-0
    """
    if not pattern or pattern.count('-') != 2:
        return "1-0-1"  # Default safe value
    
    parts = pattern.split('-')
    
    # Case 1: All numbers are valid
    try:
        morning = int(parts[0])
        afternoon = int(parts[1])
        night = int(parts[2])
    except ValueError:
        return "1-0-1"
    
    # Case 2: Suspicious pattern - only morning but medicine likely thrice daily
    if morning > 0 and afternoon == 0 and night == 0:
        # Common thrice-daily or frequent medications
        common_frequent_meds = [
            'dolo', 'paracetamol', 'antibiotic', 'amoxicillin', 
            'azithromycin', 'cefixime', 'metformin', 'amlodipine',
            'atenolol', 'losartan', 'omeprazole', 'pantoprazole'
        ]
        # Also check if the pattern looks like it was misread from "1-1-1"
        if any(med in medicine_name.lower() for med in common_frequent_meds):
            print(f"🔧 Auto-correcting {medicine_name}: {pattern} → 1-1-1 (common thrice-daily med)")
            return "1-1-1"
    
    # Case 3: Pattern is reasonable as-is
    return pattern

# =========================================================
# GENERATE TIMING TEXT FROM PATTERN
# =========================================================

def generate_instruction(pattern, food_relation="after food"):
    try:
        parts = [p.strip() for p in pattern.split('-')]
        if len(parts) != 3:
            return f"as directed by your doctor {food_relation}"

        morning = int(parts[0]) if parts[0].isdigit() else 0
        afternoon = int(parts[1]) if parts[1].isdigit() else 0
        night = int(parts[2]) if parts[2].isdigit() else 0

        timings = []
        if morning > 0: timings.append("morning")
        if afternoon > 0: timings.append("afternoon")
        if night > 0: timings.append("night")

        if not timings:
            return f"as directed by your doctor {food_relation}"

        if len(timings) == 1:
            timing_phrase = timings[0]
        elif len(timings) == 2:
            timing_phrase = f"{timings[0]} and {timings[1]}"
        else:
            timing_phrase = f"{timings[0]}, {timings[1]} and {timings[2]}"

        return f"{timing_phrase} {food_relation}"
    except Exception:
        return f"as directed by your doctor {food_relation}"

# =========================================================
# LOCAL LANGUAGE ENGINE
# =========================================================

def translate_text(timing_and_food, language):
    if not language:
        return "This medicine should be taken as directed."
        
    language = language.lower().strip()

    translations = {
        "kannada": {
            "base_format": "ಈ ಔಷಧಿಯನ್ನು {text} ತೆಗೆದುಕೊಳ್ಳಬೇಕು",
            "morning": "ಬೆಳಿಗ್ಗೆ",
            "afternoon": "ಮಧ್ಯಾಹ್ನ",
            "night": "ರಾತ್ರಿ",
            "and": "ಮತ್ತು",
            "after food": "ಊಟದ ನಂತರ",
            "before food": "ಊಟಕ್ಕೆ ಮೊದಲು",
            "as directed by your doctor after food": "ವೈದ್ಯರ ಸೂಚನೆಯಂತೆ ಊಟದ ನಂತರ",
            "as directed by your doctor before food": "ವೈದ್ಯರ ಸೂಚನೆಯಂತೆ ಊಟಕ್ಕೆ ಮೊದಲು"
        },
        "hindi": {
            "base_format": "यह दवा {text} ली जानी चाहिए",
            "morning": "सुबह",
            "afternoon": "दोपहर",
            "night": "रात",
            "and": "और",
            "after food": "भोजन के बाद",
            "before food": "भोजन से पहले",
            "as directed by your doctor after food": "डॉक्टर के निर्देशानुसार भोजन के बाद",
            "as directed by your doctor before food": "डॉक्टर के निर्देशानुसार भोजन से पहले"
        },
        "tamil": {
            "base_format": "இந்த மருந்தை {text} எடுத்துக்கொள்ள வேண்டும்",
            "morning": "காலை",
            "afternoon": "மதியம்",
            "night": "இரவு",
            "and": "மற்றும்",
            "after food": "உணவுக்குப் பிறகு",
            "before food": "உணவுக்கு முன்",
            "as directed by your doctor after food": "மருத்துவர் அறிவுறுத்தியபடி உணவுக்குப் பிறகு",
            "as directed by your doctor before food": "மருத்துவர் அறிவுறுத்தியபடி உணவுக்கு முன்"
        },
        "telugu": {
            "base_format": "ఈ మందును {text} తీసుకోవాలి",
            "morning": "ఉదయం",
            "afternoon": "మధ్యాహ్నం",
            "night": "రాత్రి",
            "and": "మరియు",
            "after food": "భోజనం తర్వాత",
            "before food": "భోజనం ముందు",
            "as directed by your doctor after food": "వైద్యులు సూచించిన విధంగా భోజనం తర్వాత",
            "as directed by your doctor before food": "వైద్యులు సూచించిన విధంగా భోజనం ముందు"
        }
    }

    if language not in translations:
        return f"This medicine should be taken {timing_and_food}"

    lang_dict = translations[language]
    processed_text = timing_and_food
    for eng, local in lang_dict.items():
        if eng != "base_format":
            processed_text = processed_text.replace(eng, local)

    return lang_dict["base_format"].format(text=processed_text)

# =========================================================
# NATIVE TEXT-TO-SPEECH GENERATOR
# =========================================================

def generate_voice_file(text, language, filename_prefix):
    lang_map = {
        "kannada": "kn",
        "hindi": "hi",
        "tamil": "ta",
        "telugu": "te"
    }
    lang_code = lang_map.get(language.lower().strip(), "en")
    
    try:
        tts = gTTS(text=text, lang=lang_code, slow=False)
        filename = f"{filename_prefix}_{int(datetime.now().timestamp())}.mp3"
        filepath = os.path.join(AUDIO_FOLDER, filename)
        tts.save(filepath)
        return f"/uploads/audio/{filename}"
    except Exception as e:
        print("TTS Generation Error:", e)
        return ""

# =========================================================
# IMPROVED GEMINI MULTIMODAL EXTRACTION
# =========================================================

def extract_medicines_from_bytes(image_bytes):
    try:
        # Convert to RGB JPEG first for better OCR
        temp_img_path = "temp_prescription.jpg"
        convert_to_rgb_and_save(image_bytes, temp_img_path)
        
        with open(temp_img_path, 'rb') as img_file:
            optimized_bytes = img_file.read()
        
        # Clean up temp file
        if os.path.exists(temp_img_path):
            os.remove(temp_img_path)
        
        # FIX: Explicitly bundle raw bytes into a type-validated Part block object
        image_part = types.Part.from_bytes(
            data=optimized_bytes,
            mime_type="image/jpeg"
        )

        # IMPROVED PROMPT with explicit timing instructions
        prompt = """
        You are an expert medical prescription analyzer. Extract medicine details and STRUCTURE THEM STRICTLY.

        CRITICAL TIMING RULES:
        - Look for patterns like "1-1-1", "1-0-1", "0-0-1", "1-0-0", "0-1-0"
        - Format is always "MORNING-AFTERNOON-NIGHT" where each number is 0 or 1+
        - If you see "OD" or "1 daily" with no time specified → use "1-0-0"
        - If you see "BD" or "twice daily" → check context: if morning+night → "1-0-1", if morning+afternoon → "1-1-0"
        - If you see "TID" or "thrice daily" → use "1-1-1"
        - If you see "1-1-1" explicitly → MUST output "1-1-1" (NEVER change to "1-0-0")
        - If dosage mentions "morning, afternoon, night" → "1-1-1"
        
        IMPORTANT: Pay close attention to handwritten or printed numbers. If you see three numbers separated by dashes or spaces (like "1 1 1" or "1-1-1"), preserve ALL three numbers.
        
        DO NOT assume missing doses are zero unless explicitly indicated.
        DO NOT convert "1-1-1" to "1-0-0" under any circumstances.
        
        Special cases:
        - Missing afternoon dose but has morning & night → "1-0-1"
        - Only morning dose → "1-0-0"
        - Only night dose → "0-0-1"

        Output ONLY valid JSON. No explanations, no markdown formatting outside the JSON structure:
        {
          "medicines": [
            {
              "name": "exact medicine name as written",
              "timing_pattern": "X-Y-Z",
              "food_relation": "before food" or "after food"
            }
          ]
        }
        """

        response = client.models.generate_content(
            model="gemini-2.5-flash",  # ✅ FIXED: correct model name
            contents=[image_part, prompt]
        )

        text = response.text.strip()
        print(f"Gemini raw response: {text}")  # Debug logging
        
        # Clean markdown formatting
        if "```" in text:
            if "```json" in text:
                text = text.split("```json")[-1].split("```")[0].strip()
            else:
                text = text.split("```")[1].split("```")[0].strip()

        data = json.loads(text)
        medicines = data.get("medicines", [])
        
        # ✅ NEW: Post-process to validate and fix timing patterns
        for med in medicines:
            pattern = med.get("timing_pattern", "")
            med_name = med.get("name", "")
            print(f"Extracted: {med_name} -> {pattern}")
            
            if pattern and pattern.count('-') == 2:
                parts = pattern.split('-')
                # Check if all parts are digits
                if all(p.isdigit() for p in parts):
                    # Log suspicious patterns
                    if parts[0].isdigit() and int(parts[0]) > 0 and parts[1] == '0' and parts[2] == '0':
                        print(f"⚠️ Warning: {med_name} extracted as {pattern} - possible misread of 1-1-1")
        
        print(f"✅ Extracted {len(medicines)} medicines total")
        return medicines

    except Exception as e:
        print("Gemini Extraction Error:", e)
        import traceback
        traceback.print_exc()
        return []

# =========================================================
# ROUTES
# =========================================================

@app.route('/')
def home():
    return render_template('staff.html')

@app.route('/staff')
def staff():
    return render_template('staff.html')

@app.route('/api/create_patient', methods=['POST'])
def create_patient():
    try:
        data = request.json
        patient_name = data.get('name', '').strip()
        local_language = data.get('local_language', '').strip()

        if not patient_name or not local_language:
            return jsonify({'success': False, 'error': 'Name and language required'})

        patient_id = str(uuid.uuid4())[:8]

        conn = sqlite3.connect('prescriptions.db')
        c = conn.cursor()
        c.execute('INSERT INTO patients (id, name, local_language) VALUES (?, ?, ?)', 
                  (patient_id, patient_name, local_language))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'patient_id': patient_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload_prescription', methods=['POST'])
def upload_prescription():
    try:
        if 'prescription' not in request.files:
            return jsonify({'success': False, 'error': 'No prescription uploaded'})

        patient_id = request.form.get('patient_id')
        if not patient_id:
            return jsonify({'success': False, 'error': 'Patient ID missing'})

        file = request.files['prescription']
        if file.filename == '' or not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file selection'})

        image_bytes = file.read()
        medicines = extract_medicines_from_bytes(image_bytes)

        if len(medicines) == 0:
            return jsonify({'success': False, 'error': 'No clear medicines detected'})

        conn = sqlite3.connect('prescriptions.db')
        c = conn.cursor()

        # Pull explicitly from database to guarantee name/language alignment
        c.execute('SELECT name, local_language FROM patients WHERE id = ?', (patient_id,))
        p_row = c.fetchone()
        if not p_row:
            conn.close()
            return jsonify({'success': False, 'error': 'Patient record profile not found'})
            
        patient_name, patient_language = p_row[0], p_row[1]

        # Save main image asset - USING ROBUST CONVERSION FUNCTION
        prescription_filename = f"prescription_{patient_id}_{int(datetime.now().timestamp())}.jpg"
        prescription_path = os.path.join(UPLOAD_FOLDER, prescription_filename)
        
        # Use the robust conversion function
        convert_to_rgb_and_save(image_bytes, prescription_path)

        c.execute('INSERT INTO prescriptions (patient_id, prescription_image) VALUES (?, ?)', 
                  (patient_id, f'/uploads/{prescription_filename}'))
        prescription_id = c.lastrowid
        stored_medicines = []

        for med in medicines:
            pattern = str(med.get("timing_pattern", "1-0-1")).strip()
            medicine_name = med.get("name", "")
            
            # ✅ ADD VALIDATION AND CORRECTION
            original_pattern = pattern
            pattern = validate_and_correct_timing(pattern, medicine_name)
            
            if original_pattern != pattern:
                print(f"🔧 Corrected {medicine_name}: {original_pattern} → {pattern}")
            
            food_rel = str(med.get("food_relation", "after food")).strip().lower()
            
            timing_and_food = generate_instruction(pattern, food_rel)
            full_english_instruction = f"This medicine should be taken {timing_and_food}"
            translated_instruction = translate_text(timing_and_food, patient_language)

            audio_url = generate_voice_file(translated_instruction, patient_language, f"audio_{patient_id}")

            c.execute('''
            INSERT INTO medicines (
                patient_id, prescription_id, name, timing_pattern, 
                instruction, translated_instruction, audio_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                patient_id, prescription_id, medicine_name, pattern,
                full_english_instruction, translated_instruction, audio_url
            ))

            stored_medicines.append({
                "id": c.lastrowid,
                "name": medicine_name,
                "timing_pattern": pattern,
                "instruction": full_english_instruction,
                "translated_instruction": translated_instruction,
                "audio_url": audio_url
            })

        conn.commit()
        conn.close()

        return jsonify({
            'success': True, 
            'medicines': stored_medicines,
            'patient_name': patient_name,
            'language': patient_language
        })

    except Exception as e:
        print(f"Upload error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload_medicine_image', methods=['POST'])
def upload_medicine_image():
    try:
        if 'medicine_image' not in request.files:
            return jsonify({'success': False, 'error': 'No image file uploaded'})
        
        medicine_id = request.form.get('medicine_id')
        file = request.files['medicine_image']
        
        if not medicine_id or file.filename == '' or not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid parameters'})
        
        filename = secure_filename(f"medicine_{medicine_id}_{datetime.now().timestamp()}.jpg")
        filepath = os.path.join(MEDICINE_FOLDER, filename)
        
        # Read and convert image using robust function
        image_bytes = file.read()
        convert_to_rgb_and_save(image_bytes, filepath)
        
        image_url = f'/uploads/medicines/{filename}'
        conn = sqlite3.connect('prescriptions.db')
        c = conn.cursor()
        c.execute('UPDATE medicines SET medicine_image = ? WHERE id = ?', (image_url, medicine_id))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'image_path': image_url})
    except Exception as e:
        print(f"Medicine image upload error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get_patient_medicines/<patient_id>')
def get_patient_medicines(patient_id):
    try:
        conn = sqlite3.connect('prescriptions.db')
        c = conn.cursor()
        
        # Pull Patient Info profile metadata explicitly alongside the call
        c.execute('SELECT name, local_language FROM patients WHERE id = ?', (patient_id,))
        p_row = c.fetchone()
        patient_name = p_row[0] if p_row else "Unknown"
        patient_lang = p_row[1] if p_row else "English"

        c.execute('''
        SELECT id, name, timing_pattern, instruction, translated_instruction, medicine_image, audio_path
        FROM medicines WHERE patient_id = ? ORDER BY created_at DESC
        ''', (patient_id,))
        
        medicines = [{
            "id": r[0], "name": r[1], "timing_pattern": r[2],
            "instruction": r[3], "translated_instruction": r[4],
            "medicine_image": r[5], "audio_url": r[6]
        } for r in c.fetchall()]
        
        conn.close()
        return jsonify({
            'success': True, 
            'medicines': medicines,
            'patient_name': patient_name,
            'local_language': patient_lang
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/patient/<patient_id>')
def patient_page(patient_id):
    return render_template('patient.html', patient_id=patient_id)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# =========================================================
# DEBUG ENDPOINT FOR TESTING EXTRACTION
# =========================================================

@app.route('/api/debug_extract', methods=['POST'])
def debug_extract():
    """Debug endpoint to test extraction without saving to database"""
    try:
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image uploaded'})
        
        file = request.files['image']
        image_bytes = file.read()
        medicines = extract_medicines_from_bytes(image_bytes)
        
        return jsonify({
            'success': True, 
            'extracted_medicines': medicines,
            'count': len(medicines)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)