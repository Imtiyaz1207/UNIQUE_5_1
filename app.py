# ------------------- IMPORTS -------------------
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory  # Flask helpers
from datetime import datetime  # for timestamps in logs
import csv  # for local CSV logging
import os  # filesystem utilities
from werkzeug.utils import secure_filename  # sanitize uploaded filenames
import cloudinary  # Cloudinary core config
import cloudinary.uploader  # Cloudinary uploader functions
import requests  # to optionally send metadata to Google Apps Script (Sheets)
from dotenv import load_dotenv  # read .env secrets

# ------------------- APP SETUP -------------------
app = Flask(__name__)  # create Flask app instance
load_dotenv()  # load environment variables from .env file

# ------------------- CONFIGURATION -------------------
# folder to store temporary local copies of uploads; will be served by Flask static if needed
STORY_FOLDER = os.path.join(app.root_path, 'static', 'uploads')  # static uploads path
os.makedirs(STORY_FOLDER, exist_ok=True)  # ensure directory exists

# allowed video file extensions
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'webm', 'ogg', 'mkv'}  # permitted video types

# log file path (CSV)
LOG_FILE = os.path.join(app.root_path, 'logs.csv')  # path for local CSV log

# create log file with header if it doesn't exist already
if not os.path.exists(LOG_FILE):  # if file missing
    with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:  # create and write header
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'ip', 'event', 'password', 'chat', 'story_url'])

# load Cloudinary credentials from environment variables
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME', ''),  # CLOUDINARY_CLOUD_NAME in .env
    api_key=os.getenv('CLOUDINARY_API_KEY', ''),  # CLOUDINARY_API_KEY in .env
    api_secret=os.getenv('CLOUDINARY_API_SECRET', '')  # CLOUDINARY_API_SECRET in .env
)

# Google Apps Script URL to push metadata to Google Sheet (optional)
GOOGLE_SCRIPT_URL = os.getenv('GOOGLE_SCRIPT_URL', '')  # set in .env if you use a Sheet

# admin password (change in .env for production)
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'mrshaik')  # default password 'mrshaik'

# ------------------- HELPERS -------------------
def allowed_file(filename):
    """Return True if file has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def log_event(ip, event, password='', chat='', story_url=''):
    """
    Append an event to local CSV log and attempt to send to Google Sheet (best-effort).
    Fields: timestamp, ip, event, password, chat, story_url
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')  # format timestamp
    try:
        with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:  # open file append mode
            writer = csv.writer(f)
            writer.writerow([timestamp, ip, event, password, chat, story_url])  # write row
    except Exception as e:
        print('Failed to write to CSV log:', e)  # print if CSV write fails

    # try to POST metadata to Google Sheet via web app (non-blocking, best-effort)
    if GOOGLE_SCRIPT_URL:
        try:
            payload = {
                'event': event,
                'password': password,
                'chat': chat,
                'story_url': story_url,
                'ip': ip
            }
            requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=5)  # short timeout
        except Exception as e:
            print('Failed to send to Google Sheet:', e)

# ------------------- ROUTES -------------------
@app.route('/')
def index():
    """Render password entry page."""
    return render_template('index.html')

@app.route('/save_password', methods=['POST'])
def save_password():
    """Validate posted password (JSON) and log the attempt; return redirect or error message."""
    data = request.get_json() or {}  # parse json payload
    password = data.get('password', '')  # get password
    ip = request.remote_addr or 'unknown'  # client IP (best-effort)

    if password == ADMIN_PASSWORD:  # check password
        log_event(ip, 'password_attempt', password=password)  # log successful attempt
        return jsonify({'redirect': url_for('main')})  # instruct client to redirect
    else:
        log_event(ip, 'password_attempt_failed', password=password)  # log failed attempt
        return jsonify({'message': 'Wrong password ❌'})  # send error

@app.route('/main')
def main():
    """Render the main dashboard (Admin/User/Chat)."""
    return render_template('main.html')

@app.route('/upload_story_video', methods=['POST'])
def upload_story_video():
    """
    Handle video file upload from form:
    - Validate file extension
    - Save a local copy (static/uploads) as fallback
    - Upload to Cloudinary (video) and capture secure URL
    - Log the event (CSV + optional Google Sheet)
    - Redirect back to /main
    """
    if 'video' not in request.files:
        return "No file part", 400  # bad request if no file field

    file = request.files['video']  # file storage object
    if file.filename == '':
        return "No selected file", 400  # no file selected

    # get uploader type (hidden form field 'uploader' -> 'admin' or 'user')
    uploader = request.form.get('uploader', 'user')
    ip = request.remote_addr or 'unknown'

    # validate extension
    if not allowed_file(file.filename):
        return "Unsupported file type", 400

    # secure filename and save local copy to static/uploads
    filename = secure_filename(file.filename)
    local_path = os.path.join(STORY_FOLDER, filename)
    try:
        file.stream.seek(0)  # ensure stream at start
        file.save(local_path)  # save local fallback copy
    except Exception as e:
        print('Local save failed:', e)

    # upload to Cloudinary (video); use upload_large for robust large uploads
    video_url = ''
    try:
        with open(local_path, 'rb') as fobj:
            upload_result = cloudinary.uploader.upload_large(
                fobj,
                resource_type='video',  # indicate video resource
                folder='stories'  # store in 'stories' folder in your cloud
            )
        video_url = upload_result.get('secure_url', '')  # secure https url if available
        print('Cloudinary upload success:', video_url)
    except Exception as e:
        print('Cloudinary upload failed:', e)
        # fallback to serving local static file if Cloudinary fails
        video_url = url_for('uploaded_file', filename=filename, _external=True)

    # log upload event
    event_name = 'admin_story_upload' if uploader == 'admin' else 'user_story_upload'
    log_event(ip, event_name, story_url=video_url)

    # redirect back to main UI
    return redirect(url_for('main'))

@app.route('/last_admin_story')
def last_admin_story():
    """
    Read logs.csv and return the latest admin story URL.
    This is a best-effort function that reads the CSV in reverse.
    """
    url = ''
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        for row in reversed(rows):
            if row.get('event') == 'admin_story_upload' and row.get('story_url'):
                url = row.get('story_url')
                break
    except Exception as e:
        print('Failed to read last_admin_story:', e)
    return jsonify({'url': url})

@app.route('/last_user_story')
def last_user_story():
    """
    Read logs.csv and return the latest user story URL.
    """
    url = ''
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        for row in reversed(rows):
            if row.get('event') == 'user_story_upload' and row.get('story_url'):
                url = row.get('story_url')
                break
    except Exception as e:
        print('Failed to read last_user_story:', e)
    return jsonify({'url': url})

@app.route('/log_chat', methods=['POST'])
def log_chat():
    """
    Endpoint to receive chat messages (JSON) and log them.
    Returns a JSON acknowledgement.
    """
    data = request.get_json() or {}
    chat_text = data.get('chat', '')
    ip = request.remote_addr or 'unknown'
    log_event(ip, 'chat_message', chat=chat_text)
    return jsonify({'status': 'ok'})

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve a local uploaded file from static/uploads (fallback)."""
    return send_from_directory(STORY_FOLDER, filename)

# ------------------- RUN APP -------------------
if __name__ == '__main__':
    # Run on 0.0.0.0 for access from other devices on network; debug=True for dev
    app.run(host='0.0.0.0', port=5000, debug=True)
