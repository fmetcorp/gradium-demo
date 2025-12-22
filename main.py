"""
Gradium Web Demo Server - Railway Edition

Text-to-Speech and Speech-to-Text with Echo Mode
"""

import asyncio
import base64
import io
import json
import os
import subprocess
import tempfile
import urllib.request
from urllib.parse import quote_plus
from typing import Optional, List, Dict

from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

try:
    import gradium
except ImportError:
    raise ImportError("Please install gradium: pip install gradium")


app = FastAPI(title="Gradium Demo")

# Create the Gradium client
client = gradium.client.GradiumClient()

# Voice catalog
VOICE_CATALOG: List[Dict[str, str]] = [
    {"name": "Emma", "voice_id": "YTpq7expH9539ERJ", "language": "en", "country": "us"},
    {"name": "Kent", "voice_id": "LFZvm12tW_z0xfGo", "language": "en", "country": "us"},
    {"name": "Sydney", "voice_id": "jtEKaLYNn6iif5PR", "language": "en", "country": "us"},
    {"name": "John", "voice_id": "KWJiFWu2O9nMPYcR", "language": "en", "country": "us"},
    {"name": "Eva", "voice_id": "ubuXFxVQwVYnZQhy", "language": "en", "country": "gb"},
    {"name": "Jack", "voice_id": "m86j6D7UZpGzHsNu", "language": "en", "country": "gb"},
    {"name": "Elise", "voice_id": "b35yykvVppLXyw_l", "language": "fr", "country": "fr"},
    {"name": "Leo", "voice_id": "axlOaUiFyOZhy4nv", "language": "fr", "country": "fr"},
    {"name": "Mia", "voice_id": "-uP9MuGtBqAvEyxI", "language": "de", "country": "de"},
    {"name": "Maximilian", "voice_id": "0y1VZjPabOBU3rWy", "language": "de", "country": "de"},
    {"name": "Valentina", "voice_id": "B36pbz5_UoWn4BDl", "language": "es", "country": "mx"},
    {"name": "Sergio", "voice_id": "xu7iJ_fn2ElcWp2s", "language": "es", "country": "es"},
    {"name": "Alice", "voice_id": "pYcGZz9VOo4n2ynh", "language": "pt", "country": "br"},
    {"name": "Davi", "voice_id": "M-FvVo9c-jGR4PgP", "language": "pt", "country": "br"},
]


def get_ffmpeg_path():
    """Find ffmpeg executable path."""
    possible_paths = [
        '/usr/bin/ffmpeg',           # Linux (Railway/Nixpacks)
        '/nix/store/ffmpeg/bin/ffmpeg',  # Nix
        '/opt/homebrew/bin/ffmpeg',  # Mac M1/M2
        '/usr/local/bin/ffmpeg',     # Mac Intel
        'ffmpeg',                     # In PATH
    ]
    
    # First try just 'ffmpeg' in PATH
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True)
        if result.returncode == 0:
            return 'ffmpeg'
    except FileNotFoundError:
        pass
    
    for path in possible_paths:
        try:
            result = subprocess.run([path, '-version'], capture_output=True)
            if result.returncode == 0:
                return path
        except FileNotFoundError:
            continue
    
    return None


def get_ffprobe_path():
    """Find ffprobe executable path."""
    possible_paths = [
        '/usr/bin/ffprobe',
        '/opt/homebrew/bin/ffprobe',
        '/usr/local/bin/ffprobe',
        'ffprobe',
    ]
    
    try:
        result = subprocess.run(['ffprobe', '-version'], capture_output=True)
        if result.returncode == 0:
            return 'ffprobe'
    except FileNotFoundError:
        pass
    
    for path in possible_paths:
        try:
            result = subprocess.run([path, '-version'], capture_output=True)
            if result.returncode == 0:
                return path
        except FileNotFoundError:
            continue
    
    return None


FFMPEG_PATH = get_ffmpeg_path()
FFPROBE_PATH = get_ffprobe_path()


def convert_audio_with_ffmpeg(audio_bytes: bytes, input_filename: str, debug: bool = False) -> bytes:
    """Convert audio to Gradium STT format: 24kHz, 16-bit, mono WAV"""
    if not FFMPEG_PATH:
        raise ValueError("ffmpeg not found")
    
    ext = input_filename.lower().split('.')[-1] if '.' in input_filename else 'wav'
    
    with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as input_file:
        input_file.write(audio_bytes)
        input_path = input_file.name
    
    output_path = input_path + '.converted.wav'
    
    try:
        cmd = [
            FFMPEG_PATH, '-y', 
            '-i', input_path,
            '-ar', '24000',
            '-ac', '1',
            '-sample_fmt', 's16',
            '-f', 'wav',
            output_path
        ]
        
        if debug:
            print(f"[DEBUG] FFmpeg command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise ValueError(f"ffmpeg error: {result.stderr}")
        
        if not os.path.exists(output_path):
            raise ValueError("FFmpeg did not create output file")
        
        output_size = os.path.getsize(output_path)
        if output_size < 1000:
            raise ValueError(f"Converted file too small ({output_size} bytes)")
        
        with open(output_path, 'rb') as f:
            return f.read()
    
    finally:
        try:
            os.unlink(input_path)
        except:
            pass
        try:
            os.unlink(output_path)
        except:
            pass


def check_ffmpeg():
    return FFMPEG_PATH is not None


# Startup check
print(f"[Gradium Demo] Starting...")
if check_ffmpeg():
    print(f"[OK] ffmpeg found at: {FFMPEG_PATH}")
else:
    print("[WARNING] ffmpeg not found - STT will not work")


# HTML page
HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gradium TTS/STT Demo</title>
    <style>
        :root {
            --bg: #0f1419;
            --card: #1a1f2e;
            --text: #e7e9ea;
            --muted: #8b98a5;
            --accent: #1d9bf0;
            --accent-hover: #1a8cd8;
            --border: #2f3336;
            --success: #00ba7c;
            --error: #f4212e;
            --recording: #f4212e;
        }
        
        * { box-sizing: border-box; }
        
        body {
            margin: 0;
            padding: 20px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
        }
        
        .container { max-width: 900px; margin: 0 auto; }
        
        h1 { font-size: 28px; margin: 0 0 8px; font-weight: 700; }
        
        .subtitle { color: var(--muted); margin: 0 0 24px; }
        
        .grid { display: grid; gap: 20px; }
        
        @media (min-width: 768px) { .grid { grid-template-columns: 1fr 1fr; } }
        
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px;
        }
        
        .card h2 {
            font-size: 18px;
            margin: 0 0 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .card h2::before {
            content: '';
            width: 8px;
            height: 8px;
            background: var(--accent);
            border-radius: 50%;
        }
        
        .form-group { margin-bottom: 16px; }
        
        label {
            display: block;
            font-size: 14px;
            color: var(--muted);
            margin-bottom: 6px;
        }
        
        select, textarea, input[type="file"] {
            width: 100%;
            padding: 12px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text);
            font-size: 14px;
            outline: none;
        }
        
        select:focus, textarea:focus { border-color: var(--accent); }
        
        textarea {
            min-height: 120px;
            resize: vertical;
            font-family: inherit;
        }
        
        .row { display: flex; gap: 12px; }
        .row > * { flex: 1; }
        
        button {
            width: 100%;
            padding: 14px 20px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }
        
        button:hover:not(:disabled) { background: var(--accent-hover); }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        
        button.recording {
            background: var(--recording);
            animation: pulse 1.5s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        
        .status {
            margin-top: 12px;
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 13px;
            display: none;
        }
        
        .status.visible { display: block; }
        .status.loading { background: rgba(29, 155, 240, 0.1); color: var(--accent); }
        .status.success { background: rgba(0, 186, 124, 0.1); color: var(--success); }
        .status.error { background: rgba(244, 33, 46, 0.1); color: var(--error); }
        
        audio {
            width: 100%;
            margin-top: 16px;
            border-radius: 8px;
        }
        
        .transcript-box {
            margin-top: 16px;
            padding: 16px;
            background: var(--bg);
            border: 1px dashed var(--border);
            border-radius: 8px;
            min-height: 100px;
            font-size: 14px;
            line-height: 1.6;
            white-space: pre-wrap;
        }
        
        .transcript-box.empty {
            color: var(--muted);
            font-style: italic;
        }
        
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 12px;
        }
        
        .checkbox-group input[type="checkbox"] {
            width: 18px;
            height: 18px;
            accent-color: var(--accent);
        }
        
        .checkbox-group label {
            margin: 0;
            color: var(--text);
            cursor: pointer;
        }
        
        input[type="file"]::file-selector-button {
            padding: 8px 16px;
            margin-right: 12px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
        }
        
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
        }
        
        .tab {
            padding: 10px 16px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--muted);
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s;
        }
        
        .tab:hover { border-color: var(--accent); }
        
        .tab.active {
            background: var(--accent);
            color: white;
            border-color: var(--accent);
        }
        
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .recording-timer {
            margin-top: 8px;
            font-size: 24px;
            font-weight: bold;
            color: var(--recording);
            text-align: center;
        }
        
        .info-box {
            margin-top: 16px;
            padding: 12px;
            background: rgba(29, 155, 240, 0.1);
            border-radius: 8px;
            font-size: 12px;
            color: var(--muted);
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎙️ Gradium Demo</h1>
        <p class="subtitle">Text-to-Speech and Speech-to-Text with Echo</p>
        
        <div class="grid">
            <!-- Text to Speech Card -->
            <div class="card">
                <h2>Text to Speech</h2>
                
                <div class="form-group">
                    <div class="row">
                        <div>
                            <label for="tts-lang">Language</label>
                            <select id="tts-lang">
                                <option value="en">English</option>
                                <option value="fr">French</option>
                                <option value="de">German</option>
                                <option value="es">Spanish</option>
                                <option value="pt">Portuguese</option>
                            </select>
                        </div>
                        <div>
                            <label for="tts-voice">Voice</label>
                            <select id="tts-voice"></select>
                        </div>
                    </div>
                </div>
                
                <div class="form-group">
                    <label for="tts-text">Text to speak</label>
                    <textarea id="tts-text" placeholder="Enter text here...">Hello! This is a demonstration of the Gradium text-to-speech system. It sounds natural and expressive.</textarea>
                </div>
                
                <button id="tts-btn">🔊 Generate Speech</button>
                
                <div id="tts-status" class="status"></div>
                <audio id="tts-audio" controls style="display: none;"></audio>
            </div>
            
            <!-- Speech to Text Card -->
            <div class="card">
                <h2>Speech to Text</h2>
                
                <div class="tabs">
                    <div class="tab active" data-tab="mic">🎤 Microphone</div>
                    <div class="tab" data-tab="file">📁 File Upload</div>
                </div>
                
                <!-- Microphone Tab -->
                <div id="tab-mic" class="tab-content active">
                    <button id="record-btn">🎤 Start Recording</button>
                    <div id="recording-timer" class="recording-timer" style="display: none;">0:00</div>
                    <audio id="recorded-audio" controls style="display: none; margin-top: 12px;"></audio>
                </div>
                
                <!-- File Upload Tab -->
                <div id="tab-file" class="tab-content">
                    <div class="form-group">
                        <label for="stt-file">Audio file</label>
                        <input type="file" id="stt-file" accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.webm">
                    </div>
                    <button id="stt-btn">📝 Transcribe File</button>
                </div>
                
                <div class="form-group" style="margin-top: 16px;">
                    <div class="row">
                        <div>
                            <label for="echo-lang">Echo Language</label>
                            <select id="echo-lang">
                                <option value="en">English</option>
                                <option value="fr">French</option>
                                <option value="de">German</option>
                                <option value="es">Spanish</option>
                                <option value="pt">Portuguese</option>
                            </select>
                        </div>
                        <div>
                            <label for="echo-voice">Echo Voice</label>
                            <select id="echo-voice"></select>
                        </div>
                    </div>
                    
                    <div class="checkbox-group">
                        <input type="checkbox" id="echo-mode">
                        <label for="echo-mode">Enable echo (speak back transcription)</label>
                    </div>
                </div>
                
                <div id="stt-status" class="status"></div>
                
                <div id="transcript" class="transcript-box empty">Transcription will appear here...</div>
                
                <audio id="echo-audio" controls style="display: none;"></audio>
                
                <div class="info-box">
                    💡 Use Safari for best microphone compatibility. Chrome may have issues on some systems.
                </div>
            </div>
        </div>
    </div>

<script>
// ============ Utility Functions ============
async function loadVoices(lang, selectElement) {
    try {
        const response = await fetch(`/api/voices?lang=${encodeURIComponent(lang)}`);
        const voices = await response.json();
        selectElement.innerHTML = '';
        for (const voice of voices) {
            const option = document.createElement('option');
            option.value = voice.voice_id;
            const country = voice.country ? ` (${voice.country.toUpperCase()})` : '';
            option.textContent = `${voice.name}${country}`;
            selectElement.appendChild(option);
        }
    } catch (error) {
        console.error('Failed to load voices:', error);
    }
}

function showStatus(elementId, message, type) {
    const el = document.getElementById(elementId);
    el.textContent = message;
    el.className = `status visible ${type}`;
}

// ============ Elements ============
const ttsLang = document.getElementById('tts-lang');
const ttsVoice = document.getElementById('tts-voice');
const ttsText = document.getElementById('tts-text');
const ttsBtn = document.getElementById('tts-btn');
const ttsAudio = document.getElementById('tts-audio');

const sttFile = document.getElementById('stt-file');
const sttBtn = document.getElementById('stt-btn');
const recordBtn = document.getElementById('record-btn');
const recordingTimer = document.getElementById('recording-timer');
const recordedAudio = document.getElementById('recorded-audio');
const transcript = document.getElementById('transcript');

const echoLang = document.getElementById('echo-lang');
const echoVoice = document.getElementById('echo-voice');
const echoMode = document.getElementById('echo-mode');
const echoAudio = document.getElementById('echo-audio');

// ============ Tab Switching ============
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
    });
});

// ============ Language Change Handlers ============
ttsLang.addEventListener('change', () => loadVoices(ttsLang.value, ttsVoice));
echoLang.addEventListener('change', () => loadVoices(echoLang.value, echoVoice));

// ============ TTS Handler ============
ttsBtn.addEventListener('click', async () => {
    const text = ttsText.value.trim();
    if (!text) {
        showStatus('tts-status', 'Please enter some text', 'error');
        return;
    }
    
    ttsBtn.disabled = true;
    ttsAudio.style.display = 'none';
    showStatus('tts-status', '⏳ Generating speech...', 'loading');
    
    try {
        const response = await fetch('/api/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: text,
                voice_id: ttsVoice.value,
                output_format: 'wav'
            })
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }
        
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        
        ttsAudio.src = url;
        ttsAudio.style.display = 'block';
        showStatus('tts-status', '✓ Audio generated!', 'success');
        
        await ttsAudio.play();
        
    } catch (error) {
        console.error('TTS error:', error);
        showStatus('tts-status', `Error: ${error.message}`, 'error');
    } finally {
        ttsBtn.disabled = false;
    }
});

// ============ File STT Handler ============
sttBtn.addEventListener('click', async () => {
    const file = sttFile.files[0];
    if (!file) {
        showStatus('stt-status', 'Please select an audio file', 'error');
        return;
    }
    
    sttBtn.disabled = true;
    transcript.textContent = '';
    transcript.className = 'transcript-box empty';
    echoAudio.style.display = 'none';
    showStatus('stt-status', '⏳ Transcribing...', 'loading');
    
    try {
        const formData = new FormData();
        formData.append('file', file);
        
        const response = await fetch('/api/stt', {
            method: 'POST',
            body: formData
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }
        
        const data = await response.json();
        const text = (data.text || '').trim();
        
        if (text) {
            transcript.textContent = text;
            transcript.className = 'transcript-box';
            showStatus('stt-status', '✓ Transcription complete!', 'success');
            
            if (echoMode.checked) {
                await doEcho(text);
            }
        } else {
            transcript.textContent = '(No speech detected)';
            transcript.className = 'transcript-box empty';
            showStatus('stt-status', 'No speech detected', 'error');
        }
        
    } catch (error) {
        console.error('STT error:', error);
        showStatus('stt-status', `Error: ${error.message}`, 'error');
    } finally {
        sttBtn.disabled = false;
    }
});

// ============ Echo Function ============
async function doEcho(text) {
    showStatus('stt-status', '⏳ Generating echo...', 'loading');
    
    try {
        const response = await fetch('/api/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: text,
                voice_id: echoVoice.value,
                output_format: 'wav'
            })
        });
        
        if (!response.ok) throw new Error('Echo TTS failed');
        
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        
        echoAudio.src = url;
        echoAudio.style.display = 'block';
        showStatus('stt-status', '✓ Done!', 'success');
        
        await echoAudio.play();
    } catch (error) {
        console.error('Echo error:', error);
        showStatus('stt-status', `Echo error: ${error.message}`, 'error');
    }
}

// ============ Microphone Recording ============
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingStartTime = null;
let timerInterval = null;

recordBtn.addEventListener('click', async () => {
    if (isRecording) {
        stopRecording();
    } else {
        await startRecording();
    }
});

async function startRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ 
            audio: {
                sampleRate: 48000,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            } 
        });
        
        const mimeTypes = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/ogg;codecs=opus',
            'audio/mp4',
            'audio/wav'
        ];
        
        let selectedMimeType = '';
        for (const mimeType of mimeTypes) {
            if (MediaRecorder.isTypeSupported(mimeType)) {
                selectedMimeType = mimeType;
                break;
            }
        }
        
        if (!selectedMimeType) {
            throw new Error('No supported audio format');
        }
        
        mediaRecorder = new MediaRecorder(stream, { 
            mimeType: selectedMimeType,
            audioBitsPerSecond: 128000
        });
        audioChunks = [];
        
        mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                audioChunks.push(event.data);
            }
        };
        
        mediaRecorder.onstop = async () => {
            const audioBlob = new Blob(audioChunks, { type: selectedMimeType });
            stream.getTracks().forEach(track => track.stop());
            
            const audioUrl = URL.createObjectURL(audioBlob);
            recordedAudio.src = audioUrl;
            recordedAudio.style.display = 'block';
            
            await transcribeRecording(audioBlob, selectedMimeType);
        };
        
        mediaRecorder.start(250);
        isRecording = true;
        recordingStartTime = Date.now();
        
        recordBtn.textContent = '⏹️ Stop Recording';
        recordBtn.classList.add('recording');
        recordingTimer.style.display = 'block';
        recordingTimer.textContent = '0:00';
        recordedAudio.style.display = 'none';
        
        timerInterval = setInterval(() => {
            const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
            const mins = Math.floor(elapsed / 60);
            const secs = elapsed % 60;
            recordingTimer.textContent = `${mins}:${secs.toString().padStart(2, '0')}`;
        }, 1000);
        
        transcript.textContent = '';
        transcript.className = 'transcript-box empty';
        echoAudio.style.display = 'none';
        showStatus('stt-status', '🎤 Recording... Click stop when done.', 'loading');
        
    } catch (error) {
        console.error('Microphone error:', error);
        showStatus('stt-status', `Microphone error: ${error.message}`, 'error');
    }
}

function stopRecording() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
    
    if (mediaRecorder && isRecording) {
        mediaRecorder.stop();
        isRecording = false;
        
        recordBtn.textContent = '🎤 Start Recording';
        recordBtn.classList.remove('recording');
        
        showStatus('stt-status', '⏳ Processing...', 'loading');
    }
}

async function transcribeRecording(audioBlob, mimeType) {
    try {
        let extension = 'webm';
        if (mimeType.includes('ogg')) extension = 'ogg';
        else if (mimeType.includes('mp4')) extension = 'm4a';
        else if (mimeType.includes('wav')) extension = 'wav';
        
        const formData = new FormData();
        formData.append('file', audioBlob, `recording.${extension}`);
        
        const response = await fetch('/api/stt', {
            method: 'POST',
            body: formData
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `HTTP ${response.status}`);
        }
        
        const data = await response.json();
        const text = (data.text || '').trim();
        
        recordingTimer.style.display = 'none';
        
        if (text) {
            transcript.textContent = text;
            transcript.className = 'transcript-box';
            showStatus('stt-status', '✓ Transcription complete!', 'success');
            
            if (echoMode.checked) {
                await doEcho(text);
            }
        } else {
            transcript.textContent = '(No speech detected)';
            transcript.className = 'transcript-box empty';
            showStatus('stt-status', 'No speech detected', 'error');
        }
        
    } catch (error) {
        console.error('Transcription error:', error);
        recordingTimer.style.display = 'none';
        showStatus('stt-status', `Error: ${error.message}`, 'error');
    }
}

// ============ Initialize ============
(async () => {
    await loadVoices(ttsLang.value, ttsVoice);
    await loadVoices(echoLang.value, echoVoice);
})();
</script>
</body>
</html>
"""


class TTSRequest(BaseModel):
    text: str
    voice_id: str
    output_format: str = "wav"
    model_name: str = "default"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/voices")
async def get_voices(lang: Optional[str] = None):
    if lang:
        return [v for v in VOICE_CATALOG if v.get("language") == lang]
    return VOICE_CATALOG


@app.post("/api/tts")
async def text_to_speech(req: TTSRequest):
    try:
        result = await client.tts(
            setup={
                "model_name": req.model_name,
                "voice_id": req.voice_id,
                "output_format": req.output_format,
            },
            text=req.text,
        )
        
        media_type = "audio/wav" if req.output_format == "wav" else "application/octet-stream"
        return Response(content=result.raw_data, media_type=media_type)
    
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/stt")
async def speech_to_text(file: UploadFile = File(...)):
    try:
        if not check_ffmpeg():
            return JSONResponse(
                status_code=500,
                content={"error": "ffmpeg is not installed on server"}
            )
        
        audio_bytes = await file.read()
        filename = file.filename or "audio.wav"
        
        if len(audio_bytes) < 1000:
            return JSONResponse(
                status_code=400,
                content={"error": f"Audio too short ({len(audio_bytes)} bytes)"}
            )
        
        try:
            converted_audio = convert_audio_with_ffmpeg(audio_bytes, filename)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        
        result = await client.stt(
            setup={"model_name": "default", "input_format": "wav"},
            audio=converted_audio,
        )
        
        text = getattr(result, "text", "") or ""
        return JSONResponse({"text": text})
    
    except Exception as e:
        error_msg = str(e)
        if "1011" in error_msg:
            error_msg = "Audio processing error"
        return JSONResponse(status_code=500, content={"error": error_msg})



# -------------------------
# Twilio voice webhook routes (Gradium STT + TTS)
# -------------------------

def _twiml(body: str) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'
    return Response(content=xml, media_type="application/xml")


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host")
    return f"{proto}://{host}"


def _download_twilio_recording(recording_url: str) -> bytes:
    """Download a Twilio RecordingUrl using basic auth.
    Requires TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN env vars.
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        raise ValueError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN")

    url = recording_url
    if not url.endswith(".wav") and not url.endswith(".mp3"):
        url = url + ".wav"

    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("utf-8")
    req.add_header("Authorization", f"Basic {auth}")

    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


async def _gradium_stt_text(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    if not check_ffmpeg():
        raise ValueError("ffmpeg is not installed on server")

    converted_audio = convert_audio_with_ffmpeg(audio_bytes, filename)
    result = await client.stt(
        setup={"model_name": "default", "input_format": "wav"},
        audio=converted_audio,
    )
    return (getattr(result, "text", "") or "").strip()


async def _gradium_tts_wav_bytes(text: str, voice_id: str = "ubuXFxVQwVYnZQhy") -> bytes:
    result = await client.tts(
        setup={"model_name": "default", "voice_id": voice_id, "output_format": "wav"},
        text=text,
    )
    return result.raw_data


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """Entry point for inbound calls.
    Records audio, then Twilio posts RecordingUrl to /twilio/recorded.
    """
    b = _base_url(request)
    prompt = "Hi. You are connected to Gradium. After the beep, speak for a few seconds."
    prompt_url = f"{b}/twilio/tts?text={quote_plus(prompt)}"

    return _twiml(
        f"""
        <Play>{prompt_url}</Play>
        <Record action=\"{b}/twilio/recorded\" method=\"POST\" playBeep=\"true\" maxLength=\"10\" trim=\"trim-silence\" />
        <Play>{prompt_url}</Play>
        """
    )


@app.post("/twilio/recorded")
async def twilio_recorded(request: Request, RecordingUrl: str = Form(None)):
    b = _base_url(request)

    if not RecordingUrl:
        msg = "I did not get the recording. Please try again."
        return _twiml(
            f'<Play>{b}/twilio/tts?text={quote_plus(msg)}</Play><Redirect method="POST">{b}/twilio/voice</Redirect>'
        )

    try:
        audio = _download_twilio_recording(RecordingUrl)
        user_text = await _gradium_stt_text(audio, "twilio.wav")
    except Exception as e:
        msg = f"Sorry, I could not process that audio. {str(e)}"
        return _twiml(
            f'<Play>{b}/twilio/tts?text={quote_plus(msg)}</Play><Redirect method="POST">{b}/twilio/voice</Redirect>'
        )

    if not user_text:
        msg = "I did not catch that. Please try again after the beep."
        return _twiml(
            f'<Play>{b}/twilio/tts?text={quote_plus(msg)}</Play><Redirect method="POST">{b}/twilio/voice</Redirect>'
        )

    reply = f"You said: {user_text}. Say something else after the beep."
    reply_url = f"{b}/twilio/tts?text={quote_plus(reply)}"

    return _twiml(
        f"""
        <Play>{reply_url}</Play>
        <Redirect method=\"POST\">{b}/twilio/voice</Redirect>
        """
    )


@app.get("/twilio/tts")
async def twilio_tts(text: str = "", voice_id: str = "ubuXFxVQwVYnZQhy"):
    text = (text or "")[:400]
    wav = await _gradium_tts_wav_bytes(text=text, voice_id=voice_id)
    return Response(content=wav, media_type="audio/wav")


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "ffmpeg": check_ffmpeg()
    }