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
from werkzeug.security import generate_password_hash, check_password_hash
import time
import threading
from datetime import datetime, timedelta
import os
import cv2
import secrets

app = Flask(__name__)

# Production security settings
app.secret_key = secrets.token_hex(32)
app.config['SESSION_COOKIE_SECURE'] = True  # Only send over HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Not accessible from JavaScript
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF protection
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# Global variables
camera = None
recording = False
recording_lock = threading.Lock()
stream_active = False

# Default settings
stream_config = {
    'width': 640,
    'height': 480,
    'fps': 30
}

record_config = {
    'width': 1920,
    'height': 1080,
    'fps': 30
}

# Production credentials - change username and password!
# Generate hash: python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your_password'))"
CREDENTIALS = {
    'admin': generate_password_hash('gary2026')
}

# Rate limiting for login attempts
login_attempts = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW = 300  # 5 minutes

def cleanup_old_attempts():
    """Remove old login attempt records"""
    current_time = time.time()
    expired = [ip for ip, data in login_attempts.items() 
               if current_time - data['first_attempt'] > LOGIN_ATTEMPT_WINDOW]
    for ip in expired:
        del login_attempts[ip]

def check_rate_limit(ip):
    """Check if IP has exceeded login attempts"""
    cleanup_old_attempts()
    
    if ip not in login_attempts:
        login_attempts[ip] = {'count': 0, 'first_attempt': time.time()}
    
    data = login_attempts[ip]
    if time.time() - data['first_attempt'] > LOGIN_ATTEMPT_WINDOW:
        login_attempts[ip] = {'count': 0, 'first_attempt': time.time()}
        data = login_attempts[ip]
    
    if data['count'] >= MAX_LOGIN_ATTEMPTS:
        return False
    
    return True

def increment_failed_attempt(ip):
    """Track failed login attempt"""
    if ip not in login_attempts:
        login_attempts[ip] = {'count': 0, 'first_attempt': time.time()}
    login_attempts[ip]['count'] += 1

def reset_login_attempts(ip):
    """Clear login attempts for IP"""
    if ip in login_attempts:
        del login_attempts[ip]

def require_login(f):
    """Decorator to require login for routes"""
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

@app.before_request
def security_headers():
    """Add security headers to responses"""
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'unsafe-inline'"
        return response

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
    except Exception as e:
        print(f"Error initializing camera: {e}")
        stream_active = False
        camera = None

def generate_frames():
    """Generate MJPEG frames for streaming"""
    global stream_active, camera
    
    # Try to initialize camera if needed
    if not stream_active or camera is None:
        print("Camera not active, initializing...")
        init_camera()
    
    if not stream_active or camera is None:
        print("Camera not available for streaming")
        # Return a blank frame to prevent HTML error response
        blank = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18l\xf6\x00\x00\x00\x00IEND\xaeB`\x82'
        yield (b'--frame\r\n'
               b'Content-Type: image/png\r\n\r\n' + blank + b'\r\n')
        time.sleep(1)
        return
    
    # Calculate delay based on FPS to limit frame rate
    frame_delay = 1.0 / stream_config['fps']
    last_frame_time = 0
    error_count = 0
    
    try:
        while True:
            try:
                # Throttle frame rate
                current_time = time.time()
                time_since_last = current_time - last_frame_time
                if time_since_last < frame_delay:
                    time.sleep(frame_delay - time_since_last)
                
                last_frame_time = time.time()
                
                # Capture frame as numpy array
                frame = camera.capture_array()
                
                # Encode to JPEG with quality 70 for balance
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                
                if not ret:
                    continue
                
                error_count = 0  # Reset error count on success
                frame_bytes = buffer.tobytes()
                
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                       
            except Exception as e:
                error_count += 1
                print(f"Frame capture error ({error_count}): {e}")
                
                # If too many errors, try reinitializing
                if error_count > 5:
                    print("Too many frame errors, attempting camera reinit...")
                    try:
                        if camera:
                            camera.stop()
                            camera.close()
                    except:
                        pass
                    init_camera()
                    error_count = 0
                
                time.sleep(0.1)
                continue
                
    except GeneratorExit:
        print("Stream client disconnected")
    except Exception as e:
        print(f"Streaming error: {e}")
        stream_active = False

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    client_ip = request.remote_addr
    
    # Rate limiting check
    if not check_rate_limit(client_ip):
        print(f"Login rate limit exceeded for IP: {client_ip}")
        return jsonify({'status': 'error', 'message': 'Too many login attempts. Try again later.'}), 429
    
    # Input validation
    if not username or not password:
        increment_failed_attempt(client_ip)
        return jsonify({'status': 'error', 'message': 'Missing credentials'}), 400
    
    # Check credentials
    if username in CREDENTIALS and check_password_hash(CREDENTIALS[username], password):
        reset_login_attempts(client_ip)
        session.permanent = True
        session['user'] = username
        session['login_time'] = datetime.now().isoformat()
        print(f"Successful login for user: {username} from IP: {client_ip}")
        return jsonify({'status': 'success', 'message': 'Logged in'})
    else:
        increment_failed_attempt(client_ip)
        print(f"Failed login attempt for user: {username} from IP: {client_ip}")
        return jsonify({'status': 'error', 'message': 'Invalid credentials'}), 401

@app.route('/logout', methods=['POST'])
def logout():
    username = session.get('user', 'Unknown')
    client_ip = request.remote_addr
    print(f"User {username} logged out from IP: {client_ip}")
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
    global recording, camera

    with recording_lock:
        if not recording:
            return jsonify({'status': 'error', 'message': 'Not recording'})
        
        try:
            print("Stopping recording")
            
            # Stop recording first
            try:
                camera.stop_recording()
            except Exception as e:
                print(f"Error stopping recording: {e}")
            
            # Stop camera
            try:
                camera.stop()
            except Exception as e:
                print(f"Error stopping camera: {e}")
            
            recording = False
            time.sleep(0.5)  # Brief pause

            # Close and fully release camera
            try:
                camera.close()
            except Exception as e:
                print(f"Camera close error: {e}")
            
            camera = None
            time.sleep(1)  # Give hardware time to fully reset

            # Restart camera with streaming configuration
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
        
        from flask import send_file
        return send_file(filepath, as_attachment=True, download_name=filename)
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
    global stream_config
    data = request.json
    
    if 'width' in data:
        stream_config['width'] = int(data['width'])
    if 'height' in data:
        stream_config['height'] = int(data['height'])
    if 'fps' in data:
        stream_config['fps'] = int(data['fps'])
    
    print(f"Stream settings updated: {stream_config['width']}x{stream_config['height']} @ {stream_config['fps']}fps")
    print("Settings will apply on next camera restart")
    
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

LOGIN_INTERFACE = '''
<!DOCTYPE html>
<html>
<head>
    <title>FTC Robot Camera - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'unsafe-inline'">
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
        .login-btn:hover:not(:disabled) {
            background: #45a049;
        }
        .login-btn:active:not(:disabled) {
            background: #3d8b40;
        }
        .login-btn:disabled {
            background: #666;
            cursor: not-allowed;
            opacity: 0.7;
        }
        .error-message {
            display: none;
            background: #dc3545;
            color: white;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
            word-wrap: break-word;
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
        .security-notice {
            background: #1e3a5f;
            border-left: 4px solid #4CAF50;
            padding: 12px;
            border-radius: 6px;
            margin-top: 20px;
            font-size: 12px;
            color: #b0d4ff;
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
                <input type="text" id="username" name="username" required autofocus autocomplete="username">
            </div>

            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required autocomplete="current-password">
            </div>

            <button type="submit" class="login-btn" id="loginBtn">
                Sign In
            </button>
        </form>

        <div class="loading" id="loading">
            Authenticating...
        </div>

        <div class="security-notice">
            <strong>🔒 Secure Connection</strong><br>
            This server requires HTTPS when accessed over the internet.
        </div>
    </div>

    <script>
        let loginAttempts = 0;
        const MAX_CLIENT_ATTEMPTS = 5;

        function handleLogin(event) {
            event.preventDefault();

            // Client-side rate limiting
            if (loginAttempts >= MAX_CLIENT_ATTEMPTS) {
                showError('Too many login attempts. Please wait before trying again.');
                return;
            }

            const username = document.getElementById('username').value.trim();
            const password = document.getElementById('password').value;
            const btn = document.getElementById('loginBtn');
            const errorMsg = document.getElementById('errorMsg');
            const loading = document.getElementById('loading');

            // Basic validation
            if (!username || !password) {
                showError('Username and password are required');
                return;
            }

            btn.disabled = true;
            loading.classList.add('show');
            errorMsg.classList.remove('show');

            fetch('/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            })
            .then(r => {
                if (r.status === 429) {
                    throw new Error('Too many login attempts. Try again later.');
                }
                return r.json();
            })
            .then(data => {
                if (data.status === 'success') {
                    // Redirect on successful login
                    window.location.href = '/';
                } else {
                    loginAttempts++;
                    showError(data.message || 'Login failed');
                    btn.disabled = false;
                    loading.classList.remove('show');
                    document.getElementById('password').value = '';
                    document.getElementById('password').focus();
                }
            })
            .catch(err => {
                loginAttempts++;
                showError(err.message || 'Connection error');
                btn.disabled = false;
                loading.classList.remove('show');
            });
        }

        function showError(message) {
            const errorMsg = document.getElementById('errorMsg');
            errorMsg.textContent = message;
            errorMsg.classList.add('show');
        }

        // Auto-focus password when username is complete
        document.getElementById('username').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                document.getElementById('password').focus();
            }
        });

        document.getElementById('password').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                document.getElementById('loginForm').dispatchEvent(new Event('submit'));
            }
        });

        // Clear error on input
        document.getElementById('username').addEventListener('input', function() {
            document.getElementById('errorMsg').classList.remove('show');
        });
        document.getElementById('password').addEventListener('input', function() {
            document.getElementById('errorMsg').classList.remove('show');
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
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'unsafe-inline'">
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
            width: 100%;
            height: auto;
            display: block;
            border: 2px solid #333;
            border-radius: 8px;
            background: #1a1a1a;
            min-height: 240px;
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
    </style>
</head>
<body>
    <div class="container">
        <div class="user-info">
            Logged in as <strong id="username">User</strong>
        </div>
        <button class="logout-btn" onclick="logout()">LOGOUT</button>

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
                    <option value="320,240">320x240 (Low Latency)</option>
                    <option value="640,480" selected>640x480 (Balanced)</option>
                    <option value="800,600">800x600 (High Quality)</option>
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
                    <option value="1280,720">1280x720 (HD)</option>
                    <option value="1920,1080" selected>1920x1080 (Full HD)</option>
                    <option value="2592,1944">2592x1944 (5MP Max)</option>
                </select>
                <div class="info-text">Recording resolution (independent of stream)</div>
            </div>
            <div class="setting-group">
                <label>FPS</label>
                <select id="recordFps">
                    <option value="24">24 FPS</option>
                    <option value="30" selected>30 FPS</option>
                    <option value="60">60 FPS</option>
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
                status.textContent = 'Status: Ready';
                
                // Reload stream after recording stops
                setTimeout(() => {
                    document.getElementById('stream').src = '{{ url_for("video_feed") }}?' + new Date().getTime();
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

        function logout() {
            if (!confirm('Logout?')) {
                return;
            }
            
            fetch('/logout', { method: 'POST' })
                .then(() => {
                    // Clear session and redirect
                    window.location.href = '/';
                })
                .catch(err => {
                    console.error('Logout error:', err);
                    window.location.href = '/';
                });
        }

        // Display username from session
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
    print("=" * 50)
    print("SECURITY NOTICE:")
    print("1. Change default credentials in CREDENTIALS dict")
    print("2. Generate password hash with:")
    print('   python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash(\'your_password\'))"')
    print("3. Use HTTPS in production (reverse proxy with nginx/Apache)")
    print("4. Keep Flask updated for security patches")
    print("=" * 50)
    print("Access from phone: http://<raspberry-pi-ip>:8080")
    print("Press Ctrl+C to stop")
    try:
        app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
        if camera:
            try:
                camera.stop()
            except:
                pass
        print("Server stopped")