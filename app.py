from flask import Flask, render_template, request, jsonify, send_file, session
from flask_socketio import SocketIO, emit
import yt_dlp
import uuid
import os
import shutil
import threading
import time
import json
from pathlib import Path
from datetime import datetime, timedelta
import subprocess

app = Flask(__name__)
app.config['SECRET_KEY'] = 'video-downloader-secret-key-2024'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# إعدادات التحميل
DOWNLOAD_FOLDER = 'downloads'
TEMP_FOLDER = 'temp'
HISTORY_FILE = 'download_history.json'
MAX_CONCURRENT_DOWNLOADS = 3
FILE_EXPIRY_HOURS = 2

# إنشاء المجلدات
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# إدارة العمليات النشطة
active_downloads = {}
download_queue = []

class DownloadProgress:
    def __init__(self, task_id, url, options=None):
        self.task_id = task_id
        self.url = url
        self.status = 'pending'
        self.progress = 0
        self.speed = ''
        self.eta = ''
        self.filename = ''
        self.error = None
        self.percent = 0
        self.video_title = ''
        self.format_type = options.get('format_type', 'video') if options else 'video'
        self.playlist_title = ''
        self.current_item = 0
        self.total_items = 0
        self.created_at = datetime.now()

def load_download_history():
    """تحميل سجل التحميلات"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return []

def save_download_history(history):
    """حفظ سجل التحميلات"""
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def get_video_info(url, download=False):
    """استخراج معلومات الفيديو"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    
    if not download:
        ydl_opts['format'] = 'best'
    else:
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['merge_output_format'] = 'mp4'
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=download)
            return info
    except Exception as e:
        return None

def convert_to_mp3(input_path, output_path):
    """تحويل الفيديو إلى MP3"""
    try:
        command = [
            'ffmpeg', '-i', input_path,
            '-vn', '-acodec', 'libmp3lame',
            '-q:a', '2', output_path
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        print(f"MP3 conversion error: {e}")
        return False

def download_video(task_id, url, options):
    """تنزيل الفيديو في خيط منفصل"""
    download = active_downloads.get(task_id)
    if not download:
        return
    
    try:
        download.status = 'downloading'
        format_type = options.get('format_type', 'video')
        
        # إعدادات yt-dlp
        if format_type == 'audio':
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(TEMP_FOLDER, f'{task_id}.%(ext)s'),
                'quiet': True,
                'no_warnings': False,
                'progress_hooks': [lambda d: progress_callback(d, task_id)],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
        else:
            ydl_opts = {
                'format': options.get('format_id', 'best'),
                'outtmpl': os.path.join(TEMP_FOLDER, f'{task_id}.%(ext)s'),
                'quiet': True,
                'no_warnings': False,
                'progress_hooks': [lambda d: progress_callback(d, task_id)],
                'merge_output_format': 'mp4',
            }
        
        emit_progress(task_id, 'جاري تحميل الفيديو...', 0)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            download.video_title = info.get('title', 'فيديو')
            
            # تحديد اسم الملف النهائي
            if format_type == 'audio':
                base_path = os.path.join(TEMP_FOLDER, f'{task_id}.%(ext)s')
                for ext in ['.mp3', '.m4a', '.webm']:
                    if os.path.exists(base_path + ext):
                        download.filename = base_path + ext
                        break
            else:
                download.filename = ydl.prepare_filename(info)
            
            # نقل الملف النهائي
            if download.filename and os.path.exists(download.filename):
                ext = os.path.splitext(download.filename)[1]
                final_name = f"{download.video_title[:50]}{ext}"
                # تنظيف اسم الملف
                final_name = "".join(c if c.isalnum() or c in ' .-_()' else '_' for c in final_name)
                final_path = os.path.join(DOWNLOAD_FOLDER, final_name)
                shutil.move(download.filename, final_path)
                download.filename = final_path
            
            download.progress = 100
            download.status = 'completed'
            emit_progress(task_id, 'اكتمل التحميل!', 100)
            
            # حفظ في السجل
            add_to_history({
                'task_id': task_id,
                'url': url,
                'title': download.video_title,
                'type': format_type,
                'filename': os.path.basename(download.filename) if download.filename else '',
                'completed_at': datetime.now().isoformat(),
                'size': os.path.getsize(download.filename) if download.filename else 0
            })
            
    except Exception as e:
        download.status = 'failed'
        download.error = str(e)
        emit_progress(task_id, f'خطأ: {str(e)}', 0, error=True)

def download_playlist(task_id, url, options):
    """تنزيل قائمة تشغيل كاملة"""
    download = active_downloads.get(task_id)
    if not download:
        return
    
    try:
        download.status = 'downloading'
        format_type = options.get('format_type', 'video')
        
        emit_progress(task_id, 'جاري تحميل قائمة التشغيل...', 0)
        
        playlist_info = get_video_info(url, download=False)
        if playlist_info:
            download.playlist_title = playlist_info.get('title', 'قائمة التشغيل')
            download.total_items = playlist_info.get('playlist_count', 0)
        
        def progress_hook(d):
            progress_callback(d, task_id)
            if d['status'] == 'downloading':
                if d.get('info_dict', {}).get('playlist_title'):
                    download.playlist_title = d['info_dict']['playlist_title']
                if d.get('info_dict', {}).get('playlist_index'):
                    download.current_item = d['info_dict']['playlist_index']
                    emit_progress(task_id, f'جاري تحميل الفيديو {download.current_item} من {download.total_items}', 
                                 download.percent)
        
        ydl_opts = {
            'format': options.get('format_id', 'best'),
            'outtmpl': os.path.join(TEMP_FOLDER, f'{task_id}/%(playlist)s/%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': False,
            'progress_hooks': [progress_hook],
            'merge_output_format': 'mp4',
            'writethumbnail': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            download.video_title = download.playlist_title
            download.status = 'completed'
            download.progress = 100
            emit_progress(task_id, 'اكتمل تحميل قائمة التشغيل!', 100)
            
            add_to_history({
                'task_id': task_id,
                'url': url,
                'title': download.playlist_title,
                'type': 'playlist',
                'filename': f'{download.playlist_title}',
                'completed_at': datetime.now().isoformat(),
                'items': download.total_items
            })
            
    except Exception as e:
        download.status = 'failed'
        download.error = str(e)
        emit_progress(task_id, f'خطأ: {str(e)}', 0, error=True)

def progress_callback(d, task_id):
    """استدعاء تحديث التقدم"""
    download = active_downloads.get(task_id)
    if not download:
        return
    
    if d['status'] == 'downloading':
        percent = d.get('percent', 0)
        speed = d.get('speed', 0)
        eta = d.get('eta', 0)
        
        if speed:
            download.speed = format_speed(speed)
        if eta:
            mins, secs = divmod(int(eta), 60)
            download.eta = f"{mins}:{secs:02d}"
        
        download.percent = percent
        status_text = download.playlist_title if download.playlist_title else 'جاري التحميل...'
        emit_progress(task_id, status_text, percent, speed=download.speed, eta=download.eta)

def format_speed(speed):
    """تنسيق سرعة التحميل"""
    if speed >= 1024 * 1024:
        return f"{speed / 1024 / 1024:.1f} MB/s"
    elif speed >= 1024:
        return f"{speed / 1024:.1f} KB/s"
    return f"{speed:.0f} B/s"

def emit_progress(task_id, status, progress, speed='', eta='', error=False):
    """إرسال تحديث التقدم عبر WebSocket"""
    socketio.emit('download_progress', {
        'task_id': task_id,
        'status': status,
        'progress': progress,
        'speed': speed,
        'eta': eta,
        'error': error
    })

def add_to_history(item):
    """إضافة عنصر إلى السجل"""
    history = load_download_history()
    history.insert(0, item)
    # الاحتفاظ بآخر 50 عنصر فقط
    history = history[:50]
    save_download_history(history)

def cleanup_old_files():
    """حذف الملفات القديمة"""
    while True:
        time.sleep(600)  # كل 10 دقائق
        try:
            current_time = time.time()
            for folder in [TEMP_FOLDER, DOWNLOAD_FOLDER]:
                if os.path.exists(folder):
                    for item in os.listdir(folder):
                        item_path = os.path.join(folder, item)
                        item_age = current_time - os.path.getmtime(item_path)
                        if item_age > FILE_EXPIRY_HOURS * 3600:
                            try:
                                if os.path.isdir(item_path):
                                    shutil.rmtree(item_path)
                                else:
                                    os.remove(item_path)
                            except:
                                pass
        except:
            pass

# بدء عملية التنظيف في الخلفية
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/info', methods=['POST'])
def get_info():
    """جلب معلومات الفيديو"""
    data = request.json
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'success': False, 'error': 'الرجاء إدخال رابط الفيديو'})
    
    try:
        info = get_video_info(url, download=False)
        if not info:
            return jsonify({'success': False, 'error': 'فشل جلب معلومات الفيديو'})
        
        # التحقق من نوع المحتوى
        is_playlist = 'playlist' in url or info.get('playlist_type') or info.get('entries')
        
        if is_playlist:
            return jsonify({
                'success': True,
                'type': 'playlist',
                'title': info.get('title', 'قائمة تشغيل'),
                'thumbnail': info.get('thumbnails', [{}])[-1].get('url') if info.get('thumbnails') else '',
                'video_count': info.get('playlist_count', len(info.get('entries', []))),
                'uploader': info.get('uploader', ''),
                'url': url
            })
        
        # معلومات فيديو عادي
        formats = []
        for f in info.get('formats', [])[:20]:
            if f.get('url') and f.get('ext') in ['mp4', 'webm', 'm4a', 'mp3']:
                res = f.get('resolution', '')
                if not res or res == 'audio only':
                    res = 'صوت فقط' if f.get('vcodec') == 'none' else f.get('format_note', '')
                
                formats.append({
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'resolution': res,
                    'filesize': f.get('filesize'),
                    'format_note': f.get('format_note', ''),
                })
        
        # حساب المدة
        duration = info.get('duration', 0)
        duration_str = f"{int(duration//60)}:{int(duration%60):02d}" if duration else ''
        
        return jsonify({
            'success': True,
            'type': 'video',
            'title': info.get('title'),
            'thumbnail': info.get('thumbnail'),
            'duration': duration_str,
            'uploader': info.get('uploader'),
            'view_count': info.get('view_count'),
            'formats': formats,
            'url': url
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/download', methods=['POST'])
def start_download():
    """بدء التحميل"""
    data = request.json
    url = data.get('url', '').strip()
    format_id = data.get('format_id', 'best')
    format_type = data.get('format_type', 'video')
    is_playlist = data.get('is_playlist', False)
    
    if not url:
        return jsonify({'success': False, 'error': 'الرجاء إدخال رابط الفيديو'})
    
    task_id = str(uuid.uuid4())
    download = DownloadProgress(task_id, url, {'format_id': format_id, 'format_type': format_type})
    active_downloads[task_id] = download
    
    options = {'format_id': format_id, 'format_type': format_type}
    
    # بدء التحميل في خيط منفصل
    if is_playlist:
        thread = threading.Thread(target=download_playlist, args=(task_id, url, options))
    else:
        thread = threading.Thread(target=download_video, args=(task_id, url, options))
    thread.start()
    
    return jsonify({
        'success': True,
        'task_id': task_id,
        'message': 'تم بدء التحميل'
    })

@app.route('/api/status/<task_id>')
def get_status(task_id):
    """الحصول على حالة التحميل"""
    download = active_downloads.get(task_id)
    if not download:
        return jsonify({'success': False, 'error': 'لم يتم العثور على العملية'})
    
    return jsonify({
        'success': True,
        'status': download.status,
        'progress': download.percent,
        'speed': download.speed,
        'eta': download.eta,
        'filename': download.filename,
        'video_title': download.video_title,
        'current_item': download.current_item,
        'total_items': download.total_items,
        'error': download.error
    })

@app.route('/download/<task_id>')
def download_file(task_id):
    """تنزيل الملف المكتمل"""
    download = active_downloads.get(task_id)
    if not download or download.status != 'completed':
        return jsonify({'success': False, 'error': 'الملف غير متاح'}), 404
    
    if not download.filename or not os.path.exists(download.filename):
        return jsonify({'success': False, 'error': 'الملف غير موجود'}), 404
    
    return send_file(
        download.filename,
        as_attachment=True,
        download_name=os.path.basename(download.filename)
    )

@app.route('/api/cancel/<task_id>')
def cancel_download(task_id):
    """إلغاء التحميل"""
    download = active_downloads.get(task_id)
    if download:
        download.status = 'cancelled'
    return jsonify({'success': True})

@app.route('/api/history', methods=['GET', 'DELETE'])
def handle_history():
    """جلب أو حذف سجل التحميلات"""
    if request.method == 'DELETE':
        save_download_history([])
        return jsonify({'success': True, 'message': 'تم حذف السجل'})
    
    history = load_download_history()
    return jsonify({'success': True, 'history': history})

@app.route('/api/queue')
def get_queue():
    """الحصول على قائمة الانتظار"""
    queue_info = {
        'active': [],
        'pending': []
    }
    
    for task_id, download in active_downloads.items():
        item = {
            'task_id': task_id,
            'title': download.video_title or download.playlist_title or 'جاري التحميل...',
            'status': download.status,
            'progress': download.percent
        }
        if download.status == 'downloading':
            queue_info['active'].append(item)
        else:
            queue_info['pending'].append(item)
    
    return jsonify({'success': True, 'queue': queue_info})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=False, host='0.0.0.0', port=port)
