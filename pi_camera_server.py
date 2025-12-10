#!/usr/bin/env python3
"""
Raspberry Pi Zero Camera Server for FTC Robot
Install: pip3 install picamera2 flask opencv-python
Run: python3 camera_server.py
"""

from flask import Flask, Response, render_template_string, request, jsonify, session, redirect, url_for
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput
import time
import threading
from datetime import datetime
import os
import cv2
import secrets
import hashlib

app = Flask(__name__)

# Set secure session secret key (change this to a strong random string in production)
app.secret_key = secrets.token_hex(32)

# Global variables
camera = None
recording = False
recording_lock = threading.Lock()
stream_active = False

# Latest frame storage for streaming
latest_frame = None
latest_frame_lock = threading.Lock()
frame_grabber_thread = None
frame_grabber_running = False

# Default settings
stream_config = {
    'width': 320,
    'height': 240,
    'fps': 30
}

record_config = {
    'width': 1920,
    'height': 1080,
    'fps': 30
}

# Default login credentials - CHANGE THESE!
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "gary2026"  # Hash this in production

def hash_password(password):
    """Simple password hashing - use werkzeug.security in production"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(stored_hash, password):
    """Verify password against hash"""
    return stored_hash == hash_password(password)

def require_login(f):
    """Decorator to require login for routes"""
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def init_camera():
    global camera, stream_active
    try:
        # Always stop and release previous camera if exists
        if camera is not None:
            try:
                camera.stop()
                camera.close()
            except Exception as e:
                print(f"Camera stop/close error during re-init: {e}")
            camera = None
            stream_active = False
            time.sleep(1)  # Give hardware time to fully reset

        print("Initializing camera...")
        camera = Picamera2()
        
        # Use video configuration with framerate control
        config = camera.create_video_configuration(
            main={"size": (stream_config['width'], stream_config['height']), 
                  "format": "RGB888"},
            controls={"FrameRate": stream_config['fps']}
        )
        
        print(f"Camera config: {stream_config['width']}x{stream_config['height']} @ {stream_config['fps']}fps")
        camera.configure(config)
        
        # Set auto exposure and auto white balance
        camera.set_controls({
            "AeEnable": True,
            "AwbEnable": True
        })
        
        camera.start()
        time.sleep(2)
        
        stream_active = True
        print("Camera initialized successfully")
        start_frame_grabber()  # <-- Start frame grabber after camera is ready
    except Exception as e:
        print(f"Error initializing camera: {e}")
        stream_active = False
        camera = None

def frame_grabber():
    """Continuously grab the latest frame from the camera."""
    global latest_frame, frame_grabber_running, camera, stream_active
    frame_delay = 1.0 / stream_config['fps']
    while frame_grabber_running:
        if not stream_active or camera is None:
            time.sleep(0.2)
            continue
        try:
            frame = camera.capture_array()
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ret:
                with latest_frame_lock:
                    latest_frame = buffer.tobytes()
        except Exception as e:
            print(f"Frame grabber error: {e}")
            time.sleep(0.1)
        time.sleep(frame_delay)

def start_frame_grabber():
    global frame_grabber_thread, frame_grabber_running
    if frame_grabber_thread is None or not frame_grabber_thread.is_alive():
        frame_grabber_running = True
        frame_grabber_thread = threading.Thread(target=frame_grabber, daemon=True)
        frame_grabber_thread.start()

def stop_frame_grabber():
    global frame_grabber_running, frame_grabber_thread
    frame_grabber_running = False
    if frame_grabber_thread is not None:
        frame_grabber_thread.join(timeout=1)
        frame_grabber_thread = None

def generate_frames():
    """Serve the most recent MJPEG frame for streaming, always as up-to-date as possible."""
    global stream_active, camera, latest_frame
    if not stream_active or camera is None:
        print("Camera not active, initializing...")
        init_camera()
    if not stream_active or camera is None:
        print("Camera not available for streaming")
        blank = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18l\xf6\x00\x00\x00\x00IEND\xaeB`\x82'
        yield (b'--frame\r\n'
               b'Content-Type: image/png\r\n\r\n' + blank + b'\r\n')
        time.sleep(1)
        return

    frame_delay = 1.0 / stream_config['fps']
    try:
        while True:
            with latest_frame_lock:
                frame_bytes = latest_frame
            if frame_bytes is not None:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            else:
                # If no frame yet, send blank
                blank = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18l\xf6\x00\x00\x00\x00IEND\xaeB`\x82'
                yield (b'--frame\r\n'
                       b'Content-Type: image/png\r\n\r\n' + blank + b'\r\n')
            time.sleep(frame_delay)
    except GeneratorExit:
        print("Stream client disconnected")
    except Exception as e:
        print(f"Streaming error: {e}")
        stream_active = False

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '')
    password = data.get('password', '')
    
    # Check credentials
    if username == DEFAULT_USERNAME and password == DEFAULT_PASSWORD:
        session['user'] = username
        return jsonify({'status': 'success', 'message': 'Logged in'})
    else:
        return jsonify({'status': 'error', 'message': 'Invalid credentials'}), 401

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'success', 'message': 'Logged out'})

@app.route('/')
def index():
    if 'user' not in session:
        return render_template_string(LOGIN_INTERFACE)
    return render_template_string(WEB_INTERFACE)

@app.route('/video_feed')
@require_login
def video_feed():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/start_recording', methods=['POST'])
@require_login
def start_recording():
    global recording, camera
    
    with recording_lock:
        if recording:
            return jsonify({'status': 'error', 'message': 'Already recording'})
        
        if camera is None:
            return jsonify({'status': 'error', 'message': 'Camera not initialized'})
        
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"/home/pi/videos/video_{timestamp}.h264"
            os.makedirs("/home/pi/videos", exist_ok=True)
            
            print(f"Starting recording to {filename}")
            print(f"Recording settings: {record_config['width']}x{record_config['height']} @ {record_config['fps']}fps")
            
            # Stop current camera and reconfigure for recording
            try:
                camera.stop()
            except Exception as e:
                print(f"Error stopping camera before reconfigure: {e}")
            
            time.sleep(1)  # Longer pause for high-res switching
            
            try:
                # Configure camera with recording settings
                video_config = camera.create_video_configuration(
                    main={"size": (record_config['width'], record_config['height']), 
                          "format": "RGB888"},
                    controls={"FrameRate": record_config['fps']}
                )
                camera.configure(video_config)
            except Exception as e:
                print(f"Error configuring camera: {e}")
                raise
            
            # Set auto exposure and white balance
            try:
                camera.set_controls({
                    "AeEnable": True,
                    "AwbEnable": True
                })
            except Exception as e:
                print(f"Error setting controls: {e}")
            
            try:
                camera.start()
            except Exception as e:
                print(f"Error starting camera: {e}")
                raise
            
            time.sleep(2)  # Let camera stabilize - important for high resolution
            
            # Create encoder with appropriate bitrate
            # Higher resolution needs higher bitrate
            if record_config['width'] >= 1920:
                bitrate = 20000000  # 20Mbps for Full HD+
            elif record_config['width'] >= 1280:
                bitrate = 15000000  # 15Mbps for HD
            else:
                bitrate = 10000000  # 10Mbps for SD
            
            try:
                encoder = H264Encoder(bitrate=bitrate)
                output = FileOutput(filename)
                camera.start_recording(encoder, output)
            except Exception as e:
                print(f"Error starting encoder: {e}")
                raise
            
            recording = True
            
            print("Recording started successfully")
            return jsonify({
                'status': 'success',
                'filename': filename,
                'settings': record_config
            })
        except Exception as e:
            print(f"Recording start error: {e}")
            recording = False
            # Try to recover camera for streaming
            try:
                if camera:
                    try:
                        camera.stop_recording()
                    except:
                        pass
                    try:
                        camera.stop()
                    except:
                        pass
                    try:
                        camera.close()
                    except:
                        pass
                camera = None
            except:
                pass
            time.sleep(1)
            try:
                init_camera()
            except Exception as init_err:
                print(f"Recovery init error: {init_err}")
            return jsonify({'status': 'error', 'message': str(e)})

@app.route('/stop_recording', methods=['POST'])
@require_login
def stop_recording():
    global recording, camera, stream_active
    with recording_lock:
        if not recording:
            return jsonify({'status': 'error', 'message': 'Not recording'})
        try:
            print("Stopping recording")
            try:
                camera.stop_recording()
            except Exception as e:
                print(f"Error stopping recording: {e}")
            try:
                camera.stop()
            except Exception as e:
                print(f"Error stopping camera: {e}")
            recording = False
            time.sleep(0.5)
            try:
                camera.close()
            except Exception as e:
                print(f"Camera close error: {e}")
            camera = None
            stream_active = False
            stop_frame_grabber()  # <-- Stop frame grabber before re-init
            time.sleep(1)
            init_camera()
            print("Recording stopped successfully")
            return jsonify({'status': 'success'})
        except Exception as e:
            print(f"Recording stop error: {e}")
            recording = False
            # Try to recover camera
            try:
                if camera:
                    try:
                        camera.stop_recording()
                    except:
                        pass
                    try:
                        camera.stop()
                    except:
                        pass
                    try:
                        camera.close()
                    except:
                        pass
            except Exception as e2:
                print(f"Camera stop error during recovery: {e2}")
            camera = None
            stream_active = False
            time.sleep(1)
            try:
                init_camera()
            except Exception as e3:
                print(f"Camera re-init error during recovery: {e3}")
            return jsonify({'status': 'error', 'message': str(e)})

@app.route('/status', methods=['GET'])
@require_login
def status():
    return jsonify({
        'recording': recording,
        'stream_active': stream_active,
        'camera_ready': camera is not None,
        'stream_config': stream_config,
        'record_config': record_config
    })

@app.route('/list_recordings', methods=['GET'])
@require_login
def list_recordings():
    try:
        video_dir = "/home/pi/videos"
        if not os.path.exists(video_dir):
            return jsonify({'status': 'success', 'recordings': []})
        
        files = []
        for filename in os.listdir(video_dir):
            if filename.endswith('.h264'):
                filepath = os.path.join(video_dir, filename)
                size = os.path.getsize(filepath)
                mtime = os.path.getmtime(filepath)
                files.append({
                    'name': filename,
                    'size': size,
                    'size_mb': round(size / (1024 * 1024), 2),
                    'date': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                })
        
        # Sort by date, newest first
        files.sort(key=lambda x: x['date'], reverse=True)
        
        return jsonify({'status': 'success', 'recordings': files})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/download/<filename>')
@require_login
def download_file(filename):
    try:
        if recording:
            return "Cannot download while recording", 400
        
        video_dir = "/home/pi/videos"
        filepath = os.path.join(video_dir, filename)
        
        # Security check - ensure filename doesn't contain path traversal
        if '..' in filename or '/' in filename:
            return "Invalid filename", 400
        
        if not os.path.exists(filepath):
            return "File not found", 404

        # Only allow .h264 files
        if not filename.endswith('.h264'):
            return "Invalid file type", 400

        # Convert to mp4 using ffmpeg
        mp4_filename = filename.replace('.h264', '.mp4')
        mp4_filepath = os.path.join(video_dir, mp4_filename)

        # Only convert if mp4 doesn't exist or is older than h264
        convert_needed = (
            not os.path.exists(mp4_filepath) or
            os.path.getmtime(mp4_filepath) < os.path.getmtime(filepath)
        )

        if convert_needed:
            import subprocess
            ffmpeg_cmd = [
                'ffmpeg',
                '-y',  # Overwrite output file if exists
                '-framerate', '30',  # Default framerate, could be improved by reading metadata
                '-i', filepath,
                '-c:v', 'copy',
                mp4_filepath
            ]
            try:
                subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception as e:
                return f"ffmpeg conversion error: {e}", 500

        from flask import send_file
        # Send mp4 file as attachment
        response = send_file(mp4_filepath, as_attachment=True, download_name=mp4_filename)
        # Optionally, clean up mp4 after sending (comment out if you want to keep mp4s)
        def cleanup_file(path):
            time.sleep(10)
            try:
                os.remove(path)
            except Exception as e:
                print(f"Cleanup error: {e}")
        threading.Thread(target=cleanup_file, args=(mp4_filepath,), daemon=True).start()
        return response
    except Exception as e:
        return str(e), 500

@app.route('/delete/<filename>', methods=['POST'])
@require_login
def delete_file(filename):
    try:
        if recording:
            return jsonify({'status': 'error', 'message': 'Cannot delete while recording'})
        
        video_dir = "/home/pi/videos"
        filepath = os.path.join(video_dir, filename)
        
        # Security check
        if '..' in filename or '/' in filename:
            return jsonify({'status': 'error', 'message': 'Invalid filename'})
        
        if not os.path.exists(filepath):
            return jsonify({'status': 'error', 'message': 'File not found'})
        
        os.remove(filepath)
        return jsonify({'status': 'success', 'message': 'File deleted'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/update_stream_settings', methods=['POST'])
@require_login
def update_stream_settings():
    global stream_config, camera, stream_active
    data = request.json
    
    if 'width' in data:
        stream_config['width'] = int(data['width'])
    if 'height' in data:
        stream_config['height'] = int(data['height'])
    if 'fps' in data:
        stream_config['fps'] = int(data['fps'])
    
    print(f"Stream settings updated: {stream_config['width']}x{stream_config['height']} @ {stream_config['fps']}fps")
    print("Restarting camera to apply new stream settings...")
    # Force camera re-init to apply new settings immediately
    try:
        if camera:
            try:
                camera.stop()
            except Exception as e:
                print(f"Error stopping camera before re-init: {e}")
            try:
                camera.close()
            except Exception as e:
                print(f"Error closing camera before re-init: {e}")
        camera = None
        stream_active = False
        stop_frame_grabber()  # <-- Stop frame grabber before re-init
        time.sleep(1)
        init_camera()
    except Exception as e:
        print(f"Error re-initializing camera after stream settings update: {e}")

    return jsonify({'status': 'success', 'settings': stream_config})

@app.route('/update_record_settings', methods=['POST'])
@require_login
def update_record_settings():
    global record_config
    data = request.json
    
    if 'width' in data:
        record_config['width'] = int(data['width'])
    if 'height' in data:
        record_config['height'] = int(data['height'])
    if 'fps' in data:
        record_config['fps'] = int(data['fps'])
    
    return jsonify({'status': 'success', 'settings': record_config})

@app.route('/reboot', methods=['POST'])
@require_login
def reboot():
    try:
        # Optionally, prevent reboot while recording
        if recording:
            return jsonify({'status': 'error', 'message': 'Cannot reboot while recording'})
        # Respond before rebooting to avoid client disconnect
        threading.Thread(target=lambda: (time.sleep(1), os.system('sudo reboot')), daemon=True).start()
        return jsonify({'status': 'success', 'message': 'Rebooting Raspberry Pi...'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

LOGIN_INTERFACE = '''
<!DOCTYPE html>
<html>
<head>
    <title>FTC Robot Camera - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
            color: #fff;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-container {
            background: #1a1a1a;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
            width: 100%;
            max-width: 360px;
            border: 1px solid #333;
        }
        .login-header {
            text-align: center;
            margin-bottom: 30px;
        }
        .login-header h1 {
            font-size: 28px;
            margin-bottom: 8px;
            color: #4CAF50;
        }
        .login-header p {
            color: #888;
            font-size: 14px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #aaa;
            font-size: 14px;
            font-weight: 600;
        }
        .form-group input {
            width: 100%;
            padding: 12px;
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 8px;
            color: #fff;
            font-size: 16px;
            transition: border-color 0.2s;
        }
        .form-group input:focus {
            outline: none;
            border-color: #4CAF50;
        }
        .login-btn {
            width: 100%;
            padding: 12px;
            background: #4CAF50;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .login-btn:hover {
            background: #45a049;
        }
        .login-btn:active {
            background: #3d8b40;
        }
        .login-btn:disabled {
            background: #666;
            cursor: not-allowed;
        }
        .error-message {
            display: none;
            background: #dc3545;
            color: white;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .error-message.show {
            display: block;
        }
        .loading {
            display: none;
            text-align: center;
            color: #888;
            font-size: 14px;
        }
        .loading.show {
            display: block;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="login-header">
            <h1>🎥 Camera</h1>
            <p>FTC Robot Camera Server</p>
        </div>

        <div class="error-message" id="errorMsg"></div>

        <form id="loginForm" onsubmit="handleLogin(event)">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autofocus>
            </div>

            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required>
            </div>

            <button type="submit" class="login-btn" id="loginBtn">
                Sign In
            </button>
        </form>

        <div class="loading" id="loading">
            Authenticating...
        </div>
    </div>

    <script>
        function handleLogin(event) {
            event.preventDefault();

            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const btn = document.getElementById('loginBtn');
            const errorMsg = document.getElementById('errorMsg');
            const loading = document.getElementById('loading');

            btn.disabled = true;
            loading.classList.add('show');
            errorMsg.classList.remove('show');

            fetch('/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    window.location.href = '/';
                } else {
                    errorMsg.textContent = data.message || 'Login failed';
                    errorMsg.classList.add('show');
                    btn.disabled = false;
                    loading.classList.remove('show');
                    document.getElementById('password').value = '';
                }
            })
            .catch(err => {
                errorMsg.textContent = 'Connection error: ' + err;
                errorMsg.classList.add('show');
                btn.disabled = false;
                loading.classList.remove('show');
            });
        }

        // Focus password field when username is filled
        document.getElementById('username').addEventListener('change', function() {
            if (this.value) {
                document.getElementById('password').focus();
            }
        });
    </script>
</body>
</html>
'''

WEB_INTERFACE = '''
<!DOCTYPE html>
<html>
<head>
    <title>FTC Robot Camera</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: #000;
            color: #fff;
            overflow-x: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .container {
            max-width: 720px;
            padding: 10px;
            position: relative;
        }
        #stream {
            height: auto;
            display: block;
            border: 2px solid #333;
            border-radius: 8px;
            background: #1a1a1a;
            min-height: 240px;
            object-fit: contain;
            width: 100%;
        }
        @media (min-width: 750px) {
            #stream {
                width: 640px;
            }
        }
        .controls {
            margin-top: 15px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        button {
            padding: 15px 20px;
            font-size: 16px;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .record-btn {
            background: #dc3545;
            color: white;
        }
        .record-btn:active { background: #c82333; }
        .record-btn.recording {
            background: #28a745;
            animation: pulse 1.5s infinite;
        }
        .record-btn:disabled {
            background: #555;
            cursor: not-allowed;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        .settings-btn {
            background: #6c757d;
            color: white;
        }
        .settings-btn:active { background: #5a6268; }
        .status {
            background: #1a1a1a;
            padding: 12px;
            border-radius: 8px;
            margin-top: 10px;
            font-size: 14px;
        }
        .status.error {
            background: #dc3545;
        }
        .settings-panel {
            display: none;
            background: #1a1a1a;
            padding: 15px;
            border-radius: 8px;
            margin-top: 10px;
        }
        .settings-panel.active { display: block; }
        .setting-group {
            margin-bottom: 15px;
        }
        .setting-group label {
            display: block;
            margin-bottom: 5px;
            font-size: 14px;
            color: #aaa;
        }
        .setting-group select {
            width: 100%;
            padding: 10px;
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 5px;
            color: #fff;
            font-size: 14px;
        }
        h3 {
            color: #4CAF50;
            margin-bottom: 15px;
            font-size: 16px;
        }
        .save-btn {
            background: #007bff;
            color: white;
            width: 100%;
        }
        .save-btn:active { background: #0056b3; }
        .info-text {
            color: #aaa;
            font-size: 12px;
            margin-top: 5px;
        }
        .recordings-panel {
            display: none;
            background: #1a1a1a;
            padding: 15px;
            border-radius: 8px;
            margin-top: 10px;
            max-height: 400px;
            overflow-y: auto;
        }
        .recordings-panel.active { display: block; }
        .recording-item {
            background: #2a2a2a;
            padding: 12px;
            border-radius: 5px;
            margin-bottom: 10px;
        }
        .recording-name {
            font-weight: 600;
            color: #fff;
            margin-bottom: 5px;
        }
        .recording-info {
            font-size: 12px;
            color: #aaa;
            margin-bottom: 8px;
        }
        .recording-actions {
            display: flex;
            gap: 8px;
        }
        .download-btn {
            background: #28a745;
            color: white;
            padding: 8px 12px;
            font-size: 14px;
            flex: 1;
        }
        .download-btn:active { background: #218838; }
        .download-btn:disabled {
            background: #555;
            cursor: not-allowed;
        }
        .delete-btn {
            background: #dc3545;
            color: white;
            padding: 8px 12px;
            font-size: 14px;
            flex: 1;
        }
        .delete-btn:active { background: #c82333; }
        .delete-btn:disabled {
            background: #555;
            cursor: not-allowed;
        }
        .empty-message {
            text-align: center;
            color: #aaa;
            padding: 20px;
        }
        .recordings-btn {
            background: #17a2b8;
            color: white;
        }
        .recordings-btn:active { background: #138496; }
        .logout-btn {
            background: #dc3545;
            color: white;
            font-size: 14px;
            padding: 10px 15px;
            position: absolute;
            top: 10px;
            right: 10px;
        }
        .logout-btn:active { background: #c82333; }
        .user-info {
            position: absolute;
            top: 10px;
            left: 10px;
            color: #aaa;
            font-size: 12px;
        }
        .reboot-btn {
            background: #ff9800;
            color: white;
            font-size: 14px;
            padding: 10px 15px;
            position: absolute;
            top: 10px;
            right: 110px;
        }
        .reboot-btn:active { background: #e68900; }
    </style>
</head>
<body>
    <div class="container">
        <div class="user-info">
            Logged in as <strong id="username">User</strong>
        </div>
        <button class="logout-btn" onclick="logout()">LOGOUT</button>
        <button class="reboot-btn" onclick="rebootPi()">REBOOT</button>

        <img id="stream" src="{{ url_for('video_feed') }}" alt="Camera Stream" onerror="handleStreamError()">
        
        <div class="controls">
            <button id="recordBtn" class="record-btn" onclick="toggleRecording()">
                START RECORDING
            </button>
            <button class="settings-btn" onclick="toggleSettings()">
                SETTINGS
            </button>
            <button class="recordings-btn" onclick="toggleRecordings()">
                RECORDINGS
            </button>
        </div>

        <div class="status" id="status">
            Status: Connecting...
        </div>

        <div class="settings-panel" id="settingsPanel">
            <h3>Stream Settings</h3>
            <div class="setting-group">
                <label>Resolution</label>
                <select id="streamRes">
                    <option value="192,144">192x144 (Super Low Latency)</option>
                    <option value="320,240">320x240 (Low Latency)</option>
                    <option value="424,240">424x240 (16:9 Low Latency)</option>
                    <option value="480,320">480x320 (Compact 3:2)</option>
                    <option value="640,360">640x360 (16:9 Balanced)</option>
                    <option value="640,480" selected>640x480 (4:3 Balanced)</option>
                    <option value="800,450">800x450 (16:9 High Quality)</option>
                    <option value="800,600">800x600 (4:3 High Quality)</option>
                    <option value="960,540">960x540 (qHD 16:9)</option>
                    <option value="1024,768">1024x768 (4:3 Sharp)</option>
                    <option value="1280,720">1280x720 (HD Stream)</option>
                    <option value="1280,960">1280x960 (4:3 HD Stream)</option>
                </select>
                <div class="info-text">Lower resolution = less latency</div>
            </div>
            <div class="setting-group">
                <label>FPS</label>
                <select id="streamFps">
                    <option value="15">15 FPS</option>
                    <option value="24">24 FPS</option>
                    <option value="30" selected>30 FPS</option>
                </select>
            </div>

            <h3 style="margin-top: 20px;">Recording Settings</h3>
            <div class="setting-group">
                <label>Resolution</label>
                <select id="recordRes">
                    <option value="640,480">640x480 (SD)</option>
                    <option value="1024,768">1024x768 (4:3 Medium)</option>
                    <option value="1280,720">1280x720 (HD 16:9)</option>
                    <option value="1280,960">1280x960 (HD 4:3)</option>
                    <option value="1600,1200">1600x1200 (2MP 4:3)</option>
                    <option value="1920,1080" selected>1920x1080 (Full HD 16:9)</option>
                    <option value="2048,1152">2048x1152 (Oversampled 16:9)</option>
                    <option value="2592,1458">2592x1458 (Max 16:9 Crop)</option>
                    <option value="2592,1080">2592x1080 (Super-Wide 2.40:1)</option>
                    <option value="2592,1944">2592x1944 (5MP Full Sensor 4:3)</option>
                </select>
                <div class="info-text">Recording resolution (independent of stream)</div>
            </div>
            <div class="setting-group">
                <label>FPS</label>
                <select id="recordFps">
                    <option value="15">15 FPS (Low Light)</option>
                    <option value="24">24 FPS (Cinematic)</option>
                    <option value="30" selected>30 FPS (Standard)</option>
                    <option value="45">45 FPS (Fast Smooth)</option>
                    <option value="60">60 FPS (High Motion)</option>
                </select>
            </div>

            <div class="info-text" style="margin: 15px 0;">
                Camera uses automatic exposure and white balance for best quality.
            </div>

            <button class="save-btn" onclick="saveSettings()">SAVE SETTINGS</button>
        </div>

        <div class="recordings-panel" id="recordingsPanel">
            <h3>Saved Recordings</h3>
            <div id="recordingsList">
                <div class="empty-message">Loading recordings...</div>
            </div>
        </div>
    </div>

    <script>
        let isRecording = false;
        let cameraReady = false;

        // Load current settings on page load
        function loadCurrentSettings() {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    cameraReady = data.camera_ready;
                    
                    // Update stream settings
                    const streamRes = `${data.stream_config.width},${data.stream_config.height}`;
                    document.getElementById('streamRes').value = streamRes;
                    document.getElementById('streamFps').value = data.stream_config.fps;
                    
                    // Update record settings
                    const recordRes = `${data.record_config.width},${data.record_config.height}`;
                    document.getElementById('recordRes').value = recordRes;
                    document.getElementById('recordFps').value = data.record_config.fps;
                    
                    // Update initial status
                    updateStatusDisplay(data);
                })
                .catch(err => {
                    console.log('Failed to load settings:', err);
                    document.getElementById('status').textContent = 'Status: Waiting for camera...';
                    document.getElementById('status').classList.remove('error');
                });
        }

        function updateStatusDisplay(data) {
            const status = document.getElementById('status');
            
            if (data.recording) {
                status.textContent = 'Status: Recording... (stream paused)';
                status.classList.remove('error');
            } else if (data.camera_ready) {
                status.textContent = 'Status: Ready';
                status.classList.remove('error');
            } else if (data.stream_active) {
                status.textContent = 'Status: Camera initializing...';
                status.classList.remove('error');
            } else {
                status.textContent = 'Status: Waiting for camera...';
                status.classList.remove('error');
            }
        }

        // Load settings when page loads
        loadCurrentSettings();

        function handleStreamError() {
            document.getElementById('status').textContent = 'Status: Camera stream error - refreshing...';
            document.getElementById('status').classList.add('error');
            setTimeout(() => {
                document.getElementById('stream').src = '{{ url_for("video_feed") }}?' + new Date().getTime();
            }, 2000);
        }

        function toggleRecording() {
            if (!cameraReady) {
                alert('Camera not ready. Please wait...');
                return;
            }

            const btn = document.getElementById('recordBtn');
            btn.disabled = true;
            
            const url = isRecording ? '/stop_recording' : '/start_recording';
            
            fetch(url, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    btn.disabled = false;
                    if (data.status === 'success') {
                        isRecording = !isRecording;
                        updateUI();
                    } else {
                        alert('Error: ' + data.message);
                        document.getElementById('status').textContent = 'Status: Error - ' + data.message;
                        document.getElementById('status').classList.add('error');
                    }
                })
                .catch(err => {
                    btn.disabled = false;
                    alert('Error: ' + err);
                    document.getElementById('status').textContent = 'Status: Connection error';
                    document.getElementById('status').classList.add('error');
                });
        }

        function updateUI() {
            const btn = document.getElementById('recordBtn');
            const status = document.getElementById('status');
            status.classList.remove('error');
            
            if (isRecording) {
                btn.textContent = 'STOP RECORDING';
                btn.classList.add('recording');
                status.textContent = 'Status: Recording... (stream paused)';
            } else {
                btn.textContent = 'START RECORDING';
                btn.classList.remove('recording');
                status.textContent = 'Status: Restarting camera...';
                
                // Wait for camera to reinitialize, then reload stream
                // Poll status until camera is ready
                let retries = 0;
                const maxRetries = 10;
                const recheckInterval = setInterval(() => {
                    fetch('/status')
                        .then(r => r.json())
                        .then(data => {
                            if (data.camera_ready && data.stream_active) {
                                clearInterval(recheckInterval);
                                status.textContent = 'Status: Reconnecting stream...';
                                
                                // Now reload the stream
                                setTimeout(() => {
                                    document.getElementById('stream').src = '{{ url_for("video_feed") }}?' + new Date().getTime();
                                    status.textContent = 'Status: Ready';
                                }, 500);
                            } else if (retries >= maxRetries) {
                                clearInterval(recheckInterval);
                                status.textContent = 'Status: Camera timeout - refresh page';
                                status.classList.add('error');
                            }
                            retries++;
                        })
                        .catch(err => {
                            retries++;
                            if (retries >= maxRetries) {
                                clearInterval(recheckInterval);
                                status.textContent = 'Status: Connection error - refresh page';
                                status.classList.add('error');
                            }
                        });
                }, 1000);
            }
        }

        function toggleSettings() {
            const panel = document.getElementById('settingsPanel');
            const recordingsPanel = document.getElementById('recordingsPanel');
            
            // Close recordings if open
            recordingsPanel.classList.remove('active');
            
            panel.classList.toggle('active');
        }

        function toggleRecordings() {
            const panel = document.getElementById('recordingsPanel');
            const settingsPanel = document.getElementById('settingsPanel');
            
            // Close settings if open
            settingsPanel.classList.remove('active');
            
            const wasActive = panel.classList.contains('active');
            panel.classList.toggle('active');
            
            // Load recordings when opening
            if (!wasActive) {
                loadRecordings();
            }
        }

        function loadRecordings() {
            const list = document.getElementById('recordingsList');
            list.innerHTML = '<div class="empty-message">Loading recordings...</div>';
            
            fetch('/list_recordings')
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        if (data.recordings.length === 0) {
                            list.innerHTML = '<div class="empty-message">No recordings found</div>';
                        } else {
                            list.innerHTML = data.recordings.map(rec => `
                                <div class="recording-item">
                                    <div class="recording-name">${rec.name}</div>
                                    <div class="recording-info">
                                        ${rec.size_mb} MB • ${rec.date}
                                    </div>
                                    <div class="recording-actions">
                                        <button class="download-btn" onclick="downloadRecording('${rec.name}')" 
                                                ${isRecording ? 'disabled' : ''}>
                                            DOWNLOAD
                                        </button>
                                        <button class="delete-btn" onclick="deleteRecording('${rec.name}')"
                                                ${isRecording ? 'disabled' : ''}>
                                            DELETE
                                        </button>
                                    </div>
                                </div>
                            `).join('');
                        }
                    } else {
                        list.innerHTML = '<div class="empty-message">Error loading recordings</div>';
                    }
                })
                .catch(err => {
                    list.innerHTML = '<div class="empty-message">Error loading recordings</div>';
                });
        }

        function downloadRecording(filename) {
            if (isRecording) {
                alert('Cannot download while recording');
                return;
            }
            window.location.href = `/download/${filename}`;
        }

        function deleteRecording(filename) {
            if (isRecording) {
                alert('Cannot delete while recording');
                return;
            }
            
            if (!confirm(`Delete ${filename}?`)) {
                return;
            }
            
            fetch(`/delete/${filename}`, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        loadRecordings(); // Refresh list
                    } else {
                        alert('Error: ' + data.message);
                    }
                })
                .catch(err => alert('Error: ' + err));
        }

        function saveSettings() {
            const streamRes = document.getElementById('streamRes').value.split(',');
            const streamFps = document.getElementById('streamFps').value;
            const recordRes = document.getElementById('recordRes').value.split(',');
            const recordFps = document.getElementById('recordFps').value;

            // Show saving message
            const status = document.getElementById('status');
            status.textContent = 'Status: Applying settings...';
            status.classList.remove('error');

            fetch('/update_stream_settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    width: parseInt(streamRes[0]),
                    height: parseInt(streamRes[1]),
                    fps: parseInt(streamFps)
                })
            }).then(() => {
                // Reload stream with new settings
                setTimeout(() => {
                    document.getElementById('stream').src = '{{ url_for("video_feed") }}?' + new Date().getTime();
                    status.textContent = 'Status: Stream settings applied';
                }, 1500);
            });

            fetch('/update_record_settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    width: parseInt(recordRes[0]),
                    height: parseInt(recordRes[1]),
                    fps: parseInt(recordFps)
                })
            });
        }

        function rebootPi() {
            if (isRecording) {
                alert('Cannot reboot while recording');
                return;
            }
            if (!confirm('Are you sure you want to reboot the Raspberry Pi?')) {
                return;
            }
            const btn = document.querySelector('.reboot-btn');
            btn.disabled = true;
            btn.textContent = 'REBOOTING...';
            fetch('/reboot', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        document.getElementById('status').textContent = 'Status: Rebooting...';
                        setTimeout(() => {
                            document.getElementById('status').textContent = 'Status: Pi is rebooting. Please wait ~30s then refresh this page.';
                        }, 2000);
                    } else {
                        alert('Error: ' + data.message);
                        btn.disabled = false;
                        btn.textContent = 'REBOOT';
                    }
                })
                .catch(err => {
                    alert('Reboot error: ' + err);
                    btn.disabled = false;
                    btn.textContent = 'REBOOT';
                });
        }

        function logout() {
            if (!confirm('Logout?')) {
                return;
            }
            
            fetch('/logout', { method: 'POST' })
                .then(() => {
                    window.location.href = '/';
                })
                .catch(err => {
                    alert('Logout error: ' + err);
                    window.location.href = '/';
                });
        }

        // Display username
        document.getElementById('username').textContent = 'User';

        // Check status periodically
        setInterval(() => {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    cameraReady = data.camera_ready;
                    
                    if (data.recording !== isRecording) {
                        isRecording = data.recording;
                        updateUI();
                    } else if (!isRecording) {
                        updateStatusDisplay(data);
                    }
                })
                .catch(err => {
                    console.log('Status check failed:', err);
                });
        }, 1000);
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    print("Starting FTC Robot Camera Server...")
    print("Access from phone: http://<raspberry-pi-ip>:8080")
    print("Press Ctrl+C to stop")
    try:
        app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_frame_grabber()  # <-- Stop frame grabber on shutdown
        if camera:
            camera.stop()
        print("Server stopped")