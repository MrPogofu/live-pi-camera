#!/usr/bin/env python3
from flask import Flask, Response, render_template_string, request, jsonify
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, Quality
from picamera2.outputs import FileOutput
import io
import time
import threading
from datetime import datetime
import os

app = Flask(__name__)

# Global variables
camera = None
recording = False
recording_lock = threading.Lock()
current_encoder = None
current_output = None

# Default settings for Waveshare Night Vision Camera (IMX335 5MP)
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

# Camera tuning parameters
camera_tuning = {
    'exposure': 10000,      # microseconds (adjustable for lighting)
    'gain': 2.0,            # Analog gain (1.0-8.0, higher for low light)
    'awb_mode': 'auto',     # Auto white balance
    'brightness': 0.0,      # -1.0 to 1.0
    'contrast': 1.0,        # 0.0 to 2.0
    'auto_exposure': True,  # Auto exposure control
    'auto_gain': True       # Auto gain control
}

class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

def apply_camera_controls():
    """Apply camera control settings based on auto/manual modes"""
    if camera is None:
        return
        
    controls = {}
    
    if camera_tuning['auto_exposure'] and camera_tuning['auto_gain']:
        # Use auto exposure and gain together
        controls["AeEnable"] = True
        controls["AeExposureMode"] = 0  # Normal exposure mode
    elif not camera_tuning['auto_exposure'] and not camera_tuning['auto_gain']:
        # Full manual mode
        controls["AeEnable"] = False
        controls["ExposureTime"] = camera_tuning['exposure']
        controls["AnalogueGain"] = camera_tuning['gain']
    elif not camera_tuning['auto_exposure']:
        # Manual exposure, auto gain
        controls["AeEnable"] = False
        controls["ExposureTime"] = camera_tuning['exposure']
        # Note: Can't have auto gain without auto exposure in libcamera
    else:
        # Auto exposure, manual gain (limited support)
        controls["AeEnable"] = True
        # Manual gain override may not work well with AE enabled
    
    # Always apply these
    controls["Brightness"] = camera_tuning['brightness']
    controls["Contrast"] = camera_tuning['contrast']
    controls["AwbEnable"] = True  # Keep auto white balance on
    
    try:
        camera.set_controls(controls)
    except Exception as e:
        print(f"Error setting camera controls: {e}")

def init_camera():
    global camera
    if camera is None:
        camera = Picamera2()
        # Configure for streaming with IMX335 5MP sensor settings
        config = camera.create_video_configuration(
            main={"size": (stream_config['width'], stream_config['height']), 
                  "format": "RGB888"},
            controls={
                "FrameRate": stream_config['fps'],
                "ExposureTime": 10000,  # Adjust for lighting conditions
                "AnalogueGain": 2.0     # Boost for low light
            }
        )
        camera.configure(config)
        camera.start()
        time.sleep(2)  # Camera warm-up for IMX335

def generate_frames():
    """Generate MJPEG frames for streaming"""
    init_camera()
    
    while True:
        frame = camera.capture_array()
        
        # Convert to JPEG
        import cv2
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        frame_bytes = buffer.tobytes()
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.route('/')
def index():
    return render_template_string(WEB_INTERFACE)

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/start_recording', methods=['POST'])
def start_recording():
    global recording, current_encoder, current_output
    
    with recording_lock:
        if recording:
            return jsonify({'status': 'error', 'message': 'Already recording'})
        
        try:
            # Create filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"/home/pi/videos/video_{timestamp}.h264"
            
            # Ensure directory exists
            os.makedirs("/home/pi/videos", exist_ok=True)
            
            # Reconfigure camera for recording with IMX335 settings
            config = camera.create_video_configuration(
                main={"size": (record_config['width'], record_config['height'])},
                controls={"FrameRate": record_config['fps']}
            )
            camera.configure(config)
            
            # Apply camera controls
            apply_camera_controls()
            
            # Start recording
            encoder = H264Encoder()
            output = FileOutput(filename)
            camera.start_recording(encoder, output)
            
            current_encoder = encoder
            current_output = output
            recording = True
            
            return jsonify({
                'status': 'success',
                'filename': filename,
                'settings': record_config
            })
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)})

@app.route('/stop_recording', methods=['POST'])
def stop_recording():
    global recording, current_encoder, current_output
    
    with recording_lock:
        if not recording:
            return jsonify({'status': 'error', 'message': 'Not recording'})
        
        try:
            camera.stop_recording()
            recording = False
            current_encoder = None
            current_output = None
            
            # Reconfigure back to streaming with IMX335 settings
            config = camera.create_video_configuration(
                main={"size": (stream_config['width'], stream_config['height'])},
                controls={"FrameRate": stream_config['fps']}
            )
            camera.configure(config)
            
            # Apply camera controls
            apply_camera_controls()
            
            return jsonify({'status': 'success'})
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)})

@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        'recording': recording,
        'stream_config': stream_config,
        'record_config': record_config
    })

@app.route('/update_camera_tuning', methods=['POST'])
def update_camera_tuning():
    global camera_tuning
    data = request.json
    
    if 'exposure' in data:
        camera_tuning['exposure'] = int(data['exposure'])
    if 'gain' in data:
        camera_tuning['gain'] = float(data['gain'])
    if 'brightness' in data:
        camera_tuning['brightness'] = float(data['brightness'])
    if 'contrast' in data:
        camera_tuning['contrast'] = float(data['contrast'])
    if 'auto_exposure' in data:
        camera_tuning['auto_exposure'] = bool(data['auto_exposure'])
    if 'auto_gain' in data:
        camera_tuning['auto_gain'] = bool(data['auto_gain'])
    
    # Apply settings to camera
    if camera and not recording:
        apply_camera_controls()
    
    return jsonify({'status': 'success', 'tuning': camera_tuning})

@app.route('/update_stream_settings', methods=['POST'])
def update_stream_settings():
    global stream_config
    data = request.json
    
    if 'width' in data:
        stream_config['width'] = int(data['width'])
    if 'height' in data:
        stream_config['height'] = int(data['height'])
    if 'fps' in data:
        stream_config['fps'] = int(data['fps'])
    
    # Restart camera with new settings if not recording
    if not recording:
        camera.stop()
        init_camera()
    
    return jsonify({'status': 'success', 'settings': stream_config})

@app.route('/update_record_settings', methods=['POST'])
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
        }
        .container {
            max-width: 100vw;
            padding: 10px;
        }
        #stream {
            width: 100%;
            height: auto;
            display: block;
            border: 2px solid #333;
            border-radius: 8px;
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
        .setting-group select, .setting-group input {
            width: 100%;
            padding: 10px;
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 5px;
            color: #fff;
            font-size: 14px;
        }
        .toggle-container {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 15px;
            padding: 12px;
            background: #2a2a2a;
            border-radius: 5px;
        }
        .toggle-label {
            font-size: 14px;
            color: #fff;
        }
        .toggle-switch {
            position: relative;
            width: 50px;
            height: 26px;
        }
        .toggle-switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #555;
            transition: 0.3s;
            border-radius: 26px;
        }
        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 20px;
            width: 20px;
            left: 3px;
            bottom: 3px;
            background-color: white;
            transition: 0.3s;
            border-radius: 50%;
        }
        .toggle-switch input:checked + .toggle-slider {
            background-color: #4CAF50;
        }
        .toggle-switch input:checked + .toggle-slider:before {
            transform: translateX(24px);
        }
        .manual-controls {
            opacity: 0.5;
            pointer-events: none;
            transition: opacity 0.3s;
        }
        .manual-controls.active {
            opacity: 1;
            pointer-events: auto;
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
    </style>
</head>
<body>
    <div class="container">
        <img id="stream" src="{{ url_for('video_feed') }}" alt="Camera Stream">
        
        <div class="controls">
            <button id="recordBtn" class="record-btn" onclick="toggleRecording()">
                START RECORDING
            </button>
            <button class="settings-btn" onclick="toggleSettings()">
                SETTINGS
            </button>
        </div>

        <div class="status" id="status">
            Status: Ready
        </div>

        <div class="settings-panel" id="settingsPanel">
            <h3>Stream Settings</h3>
            <div class="setting-group">
                <label>Resolution</label>
                <select id="streamRes">
                    <option value="320,240">320x240 (Low)</option>
                    <option value="640,480" selected>640x480 (Medium)</option>
                    <option value="800,600">800x600 (High)</option>
                </select>
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
                    <option value="640,480">640x480</option>
                    <option value="1280,720">1280x720 (HD)</option>
                    <option value="1920,1080" selected>1920x1080 (Full HD)</option>
                    <option value="2592,1944">2592x1944 (5MP Max)</option>
                </select>
            </div>
            <div class="setting-group">
                <label>FPS</label>
                <select id="recordFps">
                    <option value="24">24 FPS</option>
                    <option value="30" selected>30 FPS</option>
                    <option value="60">60 FPS</option>
                </select>
            </div>

            <h3 style="margin-top: 20px;">Camera Tuning (Night Vision)</h3>
            
            <div class="toggle-container">
                <span class="toggle-label">Auto Exposure</span>
                <label class="toggle-switch">
                    <input type="checkbox" id="autoExposure" checked onchange="toggleAutoControls()">
                    <span class="toggle-slider"></span>
                </label>
            </div>
            
            <div id="exposureControls" class="manual-controls">
                <div class="setting-group">
                    <label>Exposure Time: <span id="exposureVal">10000</span>µs</label>
                    <input type="range" id="exposure" min="1000" max="100000" value="10000" step="1000">
                </div>
            </div>
            
            <div class="toggle-container">
                <span class="toggle-label">Auto Gain</span>
                <label class="toggle-switch">
                    <input type="checkbox" id="autoGain" checked onchange="toggleAutoControls()">
                    <span class="toggle-slider"></span>
                </label>
            </div>
            
            <div id="gainControls" class="manual-controls">
                <div class="setting-group">
                    <label>Gain: <span id="gainVal">2.0</span>x</label>
                    <input type="range" id="gain" min="1" max="8" value="2" step="0.1">
                </div>
            </div>
            
            <div class="setting-group">
                <label>Brightness: <span id="brightnessVal">0.0</span></label>
                <input type="range" id="brightness" min="-1" max="1" value="0" step="0.1">
            </div>
            <div class="setting-group">
                <label>Contrast: <span id="contrastVal">1.0</span></label>
                <input type="range" id="contrast" min="0" max="2" value="1" step="0.1">
            </div>

            <button class="save-btn" onclick="saveSettings()">SAVE SETTINGS</button>
        </div>
    </div>

    <script>
        let isRecording = false;

        // Update slider value displays
        document.getElementById('exposure').oninput = function() {
            document.getElementById('exposureVal').textContent = this.value;
        };
        document.getElementById('gain').oninput = function() {
            document.getElementById('gainVal').textContent = parseFloat(this.value).toFixed(1);
        };
        document.getElementById('brightness').oninput = function() {
            document.getElementById('brightnessVal').textContent = parseFloat(this.value).toFixed(1);
        };
        document.getElementById('contrast').oninput = function() {
            document.getElementById('contrastVal').textContent = parseFloat(this.value).toFixed(1);
        };

        // Toggle manual controls based on auto settings
        function toggleAutoControls() {
            const autoExposure = document.getElementById('autoExposure').checked;
            const autoGain = document.getElementById('autoGain').checked;
            
            const exposureControls = document.getElementById('exposureControls');
            const gainControls = document.getElementById('gainControls');
            
            if (autoExposure) {
                exposureControls.classList.remove('active');
            } else {
                exposureControls.classList.add('active');
            }
            
            if (autoGain) {
                gainControls.classList.remove('active');
            } else {
                gainControls.classList.add('active');
            }
        }

        // Initialize toggle states on load
        toggleAutoControls();

        function toggleRecording() {
            const btn = document.getElementById('recordBtn');
            const url = isRecording ? '/stop_recording' : '/start_recording';
            
            fetch(url, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'success') {
                        isRecording = !isRecording;
                        updateUI();
                    } else {
                        alert('Error: ' + data.message);
                    }
                })
                .catch(err => alert('Error: ' + err));
        }

        function updateUI() {
            const btn = document.getElementById('recordBtn');
            if (isRecording) {
                btn.textContent = 'STOP RECORDING';
                btn.classList.add('recording');
                document.getElementById('status').textContent = 'Status: Recording...';
            } else {
                btn.textContent = 'START RECORDING';
                btn.classList.remove('recording');
                document.getElementById('status').textContent = 'Status: Ready';
            }
        }

        function toggleSettings() {
            document.getElementById('settingsPanel').classList.toggle('active');
        }

        function saveSettings() {
            const streamRes = document.getElementById('streamRes').value.split(',');
            const streamFps = document.getElementById('streamFps').value;
            const recordRes = document.getElementById('recordRes').value.split(',');
            const recordFps = document.getElementById('recordFps').value;

            // Update camera tuning
            fetch('/update_camera_tuning', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    exposure: parseInt(document.getElementById('exposure').value),
                    gain: parseFloat(document.getElementById('gain').value),
                    brightness: parseFloat(document.getElementById('brightness').value),
                    contrast: parseFloat(document.getElementById('contrast').value),
                    auto_exposure: document.getElementById('autoExposure').checked,
                    auto_gain: document.getElementById('autoGain').checked
                })
            });

            fetch('/update_stream_settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    width: parseInt(streamRes[0]),
                    height: parseInt(streamRes[1]),
                    fps: parseInt(streamFps)
                })
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

            alert('Settings saved! Camera controls updated.');
        }

        // Check recording status periodically
        setInterval(() => {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    if (data.recording !== isRecording) {
                        isRecording = data.recording;
                        updateUI();
                    }
                });
        }, 2000);
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    print("Starting FTC Robot Camera Server...")
    print("Access from phone: http://<raspberry-pi-ip>:8080")
    app.run(host='0.0.0.0', port=8080, threaded=True)