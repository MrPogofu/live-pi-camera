#!/usr/bin/env python3
"""
Raspberry Pi Zero Camera Server for FTC Robot
Install: pip3 install picamera2 flask opencv-python ffmpeg-python
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
import subprocess

app = Flask(__name__)

# Set secure session secret key (change this to a strong random string in production)
app.secret_key = secrets.token_hex(32)

# Global variables
camera = None
recording = False
recording_lock = threading.Lock()
stream_active = False

# --- Add these globals for frame grabbing ---
latest_frame = None
latest_frame_lock = threading.Lock()
frame_grabber_thread = None
frame_grabber_running = False
# --------------------------------------------

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
        # --- Start frame grabber after camera is started ---
        start_frame_grabber()
    except Exception as e:
        print(f"Error initializing camera: {e}")
        stream_active = False
        camera = None

def start_frame_grabber():
    """Start a background thread to always grab the latest frame from the camera."""
    global frame_grabber_thread, frame_grabber_running, camera, latest_frame

    def grabber():
        global frame_grabber_running, camera, latest_frame
        frame_grabber_running = True
        while frame_grabber_running and camera is not None:
            try:
                frame = camera.capture_array()
                with latest_frame_lock:
                    latest_frame = frame
            except Exception as e:
                print(f"Frame grabber error: {e}")
                time.sleep(0.1)
        frame_grabber_running = False

    # Stop any previous grabber
    stop_frame_grabber()
    if camera is not None:
        frame_grabber_thread = threading.Thread(target=grabber, daemon=True)
        frame_grabber_thread.start()

def stop_frame_grabber():
    """Stop the background frame grabber thread."""
    global frame_grabber_running, frame_grabber_thread
    frame_grabber_running = False
    if frame_grabber_thread is not None:
        frame_grabber_thread.join(timeout=1)
    frame_grabber_thread = None

def generate_frames():
    """Generate MJPEG frames for streaming (always serve the latest frame)."""
    global stream_active, camera, latest_frame

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

                # --- Serve the latest frame only ---
                with latest_frame_lock:
                    frame = latest_frame.copy() if latest_frame is not None else None

                if frame is None:
                    # No frame available yet, send blank
                    blank = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18l\xf6\x00\x00\x00\x00IEND\xaeB`\x82'
                    yield (b'--frame\r\n'
                           b'Content-Type: image/png\r\n\r\n' + blank + b'\r\n')
                    time.sleep(0.1)
                    continue

                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ret:
                    continue

                error_count = 0
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

            except Exception as e:
                error_count += 1
                print(f"Frame serve error ({error_count}): {e}")
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
            # --- Stop frame grabber before reconfiguring camera ---
            stop_frame_grabber()
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
            # --- Stop frame grabber before closing camera ---
            stop_frame_grabber()
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

@app.route('/update_stream_settings', methods=['POST'])
@require_login
def update_stream_settings():
    global stream_config, camera, stream_active
    
    try:
        data = request.json
        stream_config['width'] = data.get('width', stream_config['width'])
        stream_config['height'] = data.get('height', stream_config['height'])
        stream_config['fps'] = data.get('fps', stream_config['fps'])
        
        print(f"Updated stream settings: {stream_config['width']}x{stream_config['height']} @ {stream_config['fps']}fps")
        
        # Reinitialize camera with new settings
        init_camera()
        
        return jsonify({'status': 'success', 'message': 'Stream settings updated'})
    except Exception as e:
        print(f"Error updating stream settings: {e}")
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/update_record_settings', methods=['POST'])
@require_login
def update_record_settings():
    global record_config
    
    try:
        data = request.json
        record_config['width'] = data.get('width', record_config['width'])
        record_config['height'] = data.get('height', record_config['height'])
        record_config['fps'] = data.get('fps', record_config['fps'])
        
        print(f"Updated record settings: {record_config['width']}x{record_config['height']} @ {record_config['fps']}fps")
        
        return jsonify({'status': 'success', 'message': 'Record settings updated'})
    except Exception as e:
        print(f"Error updating record settings: {e}")
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/list_recordings', methods=['GET'])
@require_login
def list_recordings():
    try:
        video_dir = "/home/pi/videos"
        if not os.path.exists(video_dir):
            return jsonify({'status': 'success', 'recordings': []})
        
        files = []
        for filename in os.listdir(video_dir):
            # Only list MP4 files (or H.264 if MP4 not yet converted)
            if filename.endswith('.h264'):
                # Check if MP4 version exists
                mp4_filename = filename.replace('.h264', '.mp4')
                mp4_path = os.path.join(video_dir, mp4_filename)
                
                if os.path.exists(mp4_path):
                    # Use MP4 if available
                    filepath = mp4_path
                    display_filename = mp4_filename
                else:
                    # Fall back to H.264
                    filepath = os.path.join(video_dir, filename)
                    display_filename = filename
                
                try:
                    size = os.path.getsize(filepath)
                    mtime = os.path.getmtime(filepath)
                    files.append({
                        'name': display_filename,
                        'original_h264': filename,
                        'size': size,
                        'size_mb': round(size / (1024 * 1024), 2),
                        'date': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                    })
                except Exception as e:
                    print(f"Error getting file info for {filename}: {e}")
        
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
        
        # Security check - ensure filename doesn't contain path traversal
        if '..' in filename or '/' in filename:
            return "Invalid filename", 400
        
        # If requesting H.264, convert to MP4 first
        if filename.endswith('.h264'):
            h264_path = os.path.join(video_dir, filename)
            if not os.path.exists(h264_path):
                return "File not found", 404
            
            mp4_filename = filename.replace('.h264', '.mp4')
            mp4_path = os.path.join(video_dir, mp4_filename)
            
            # Convert if MP4 doesn't exist
            if not os.path.exists(mp4_path):
                mp4_path = convert_h264_to_mp4(h264_path)
                if mp4_path is None:
                    return "Conversion failed", 500
            
            filepath = mp4_path
            download_filename = mp4_filename
        else:
            filepath = os.path.join(video_dir, filename)
            download_filename = filename
        
        if not os.path.exists(filepath):
            return "File not found", 404
        
        from flask import send_file
        return send_file(filepath, as_attachment=True, download_name=download_filename)
    except Exception as e:
        return str(e), 500

@app.route('/delete/<filename>', methods=['POST'])
@require_login
def delete_file(filename):
    try:
        if recording:
            return jsonify({'status': 'error', 'message': 'Cannot delete while recording'})
        
        video_dir = "/home/pi/videos"
        
        # Security check
        if '..' in filename or '/' in filename:
            return jsonify({'status': 'error', 'message': 'Invalid filename'})
        
        filepath = os.path.join(video_dir, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'status': 'error', 'message': 'File not found'})
        
        # Delete the file
        os.remove(filepath)
        
        # Also try to delete corresponding MP4 or H.264 if deleting the other format
        if filename.endswith('.mp4'):
            h264_path = filepath.replace('.mp4', '.h264')
            if os.path.exists(h264_path):
                try:
                    os.remove(h264_path)
                except:
                    pass
        elif filename.endswith('.h264'):
            mp4_path = filepath.replace('.h264', '.mp4')
            if os.path.exists(mp4_path):
                try:
                    os.remove(mp4_path)
                except:
                    pass
        
        return jsonify({'status': 'success', 'message': 'File deleted'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/reboot', methods=['POST'])
@require_login
def reboot_pi():
    try:
        # Respond before rebooting to avoid client disconnect
        threading.Thread(target=lambda: (time.sleep(1), os.system('sudo reboot'))).start()
        return jsonify({'status': 'success', 'message': 'Rebooting Raspberry Pi...'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

def convert_h264_to_mp4(h264_path):
    """Convert H.264 video to MP4 format using ffmpeg"""
    mp4_path = h264_path.replace('.h264', '.mp4')
    
    # Check if MP4 already exists
    if os.path.exists(mp4_path):
        return mp4_path
    
    try:
        print(f"Converting {h264_path} to MP4...")
        # Use ffmpeg to convert H.264 to MP4
        # -c:v copy uses the same codec (faster)
        # -c:a aac adds audio codec
        # -y overwrites output file
        subprocess.run([
            'ffmpeg',
            '-i', h264_path,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-y',
            mp4_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        
        if os.path.exists(mp4_path):
            print(f"Conversion successful: {mp4_path}")
            return mp4_path
        else:
            print(f"Conversion failed: MP4 file not created")
            return None
    except subprocess.TimeoutExpired:
        print(f"Conversion timeout for {h264_path}")
        return None
    except Exception as e:
        print(f"Conversion error: {e}")
        return None

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
            .then((r) => r.json())
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
        .reboot-btn {
            position: absolute;
            top: 10px;
            right: 110px;
            background: #ff9800;
            color: white;
            font-size: 14px;
            padding: 10px 15px;
            border-radius: 8px;
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
                .then((r) => r.json())
                .then((data) => {
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
                .catch((err) => {
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
                .then((r) => r.json())
                .then((data) => {
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
                .catch((err) => {
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
                .then((r) => r.json())
                .then((data) => {
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
                .catch((err) => {
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
                .then((r) => r.json())
                .then((data) => {
                    if (data.status === 'success') {
                        loadRecordings(); // Refresh list
                    } else {
                        alert('Error: ' + data.message);
                    }
                })
                .catch((err) => alert('Error: ' + err));
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
                    window.location.href = '/';
                })
                .catch(err => {
                    alert('Logout error: ' + err);
                    window.location.href = '/';
                });
        }

        function rebootPi() {
            if (!confirm('Are you sure you want to reboot the Raspberry Pi?')) return;
            const btn = document.querySelector('.reboot-btn');
            btn.disabled = true;
            btn.textContent = 'REBOOTING...';
            fetch('/reboot', {method: 'POST'})
                .then((r) => r.json())
                .then((data) => {
                    if (data.status === 'success') {
                        document.getElementById('status').textContent = 'Status: ' + data.message;
                        setTimeout(() => {
                            btn.textContent = 'REBOOT';
                            btn.disabled = false;
                        }, 10000);
                    } else {
                        alert('Error: ' + data.message);
                        btn.textContent = 'REBOOT';
                        btn.disabled = false;
                    }
                })
                .catch((err) => {
                    alert('Reboot error: ' + err);
                    btn.textContent = 'REBOOT';
                    btn.disabled = false;
                });
        }

        // Display username
        document.getElementById('username').textContent = 'User';

        // Check status periodically
        setInterval(() => {
            fetch('/status')
                .then((r) => r.json())
                .then((data) => {
                    cameraReady = data.camera_ready;
                    
                    if (data.recording !== isRecording) {
                        isRecording = data.recording;
                        updateUI();
                    } else if (!isRecording) {
                        updateStatusDisplay(data);
                    }
                })
                .catch((err) => {
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
        if camera:
            camera.stop()
        stop_frame_grabber()
        print("Server stopped")