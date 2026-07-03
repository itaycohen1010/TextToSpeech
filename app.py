import base64
import io
import os
import tempfile
from pathlib import Path

from flask import Flask, Response, jsonify, request

from pdf_to_speech import (
    clean_text,
    concatenate_audio,
    extract_text,
    extract_text_docx,
    segment_text,
    synthesize_segments,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc"}

ORIGINAL_HTML = Path(r"C:\Users\Itayc\Downloads\TTS Generator - Standalone.html")

PATCH_SCRIPT = """
<script>
(function patch() {
  function findInstance() {
    // Wait for the file input the component creates
    const el = document.getElementById('tts-file-input');
    if (!el) return null;

    // Get its React fiber key
    const fiberKey = Object.keys(el).find(k =>
      k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
    if (!fiberKey) return null;

    // Walk UP via .return to find the StreamableComponent (has .logic)
    let node = el[fiberKey];
    while (node) {
      if (node.stateNode && node.stateNode.logic &&
          typeof node.stateNode.logic.processFile === 'function')
        return node.stateNode.logic;
      node = node.return || null;
    }
    return null;
  }

  function applyPatch(inst) {
    let _file = null, _audio = null, _audioUrl = null;

    inst.processFile = function(file) {
      _file = file;
      const mb = (file.size / (1024*1024)).toFixed(1);
      inst.setState({ stage: 'uploaded', fileName: file.name,
        fileSize: (parseFloat(mb) < 0.1 ? '< 0.1' : mb) + ' MB' });
    };

    inst.startConversion = function() {
      if (!_file) return;
      inst.setState({ stage: 'converting', progress: 0 });
      inst.conversionTimer = setInterval(function() {
        inst.setState(function(s) {
          return { progress: Math.min(s.progress + 1.5 + (88 - s.progress) * 0.04, 88) };
        });
      }, 160);

      const fd = new FormData();
      fd.append('file', _file);
      fetch('/process', { method: 'POST', body: fd })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          clearInterval(inst.conversionTimer);
          if (data.error) { inst.setState({ stage: 'uploaded' }); alert('שגיאה: ' + data.error); return; }
          const bytes = atob(data.audio);
          const arr = new Uint8Array(bytes.length);
          for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
          _audioUrl = URL.createObjectURL(new Blob([arr], { type: 'audio/mpeg' }));
          _audio = new Audio(_audioUrl);
          _audio.onloadedmetadata = function() {
            inst.setState({ stage: 'done', isPlaying: false, playSeconds: 0,
              totalSeconds: Math.round(_audio.duration) });
            // Auto-download the MP3
            const a = document.createElement('a');
            a.href = _audioUrl;
            a.download = (_file ? _file.name.replace(/\.[^.]+$/, '') : 'speech') + '_speech.mp3';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
          };
          _audio.ontimeupdate = function() {
            inst.setState({ playSeconds: Math.floor(_audio.currentTime) });
          };
          _audio.onended = function() { inst.setState({ isPlaying: false, playSeconds: 0 }); };
        })
        .catch(function(err) {
          clearInterval(inst.conversionTimer);
          inst.setState({ stage: 'uploaded' });
          alert('שגיאת רשת: ' + err.message);
        });
    };

    inst.togglePlay = function() {
      if (!_audio) return;
      if (!inst.state.isPlaying) { _audio.play(); inst.setState({ isPlaying: true }); }
      else { _audio.pause(); inst.setState({ isPlaying: false }); }
    };

    inst.reset = function() {
      clearInterval(inst.conversionTimer);
      if (_audio) { _audio.pause(); _audio.src = ''; }
      if (_audioUrl) { URL.revokeObjectURL(_audioUrl); _audioUrl = null; }
      _file = null; _audio = null;
      inst.setState({ stage: 'idle', fileName: '', fileSize: '', progress: 0,
        isPlaying: false, isDragging: false, playSeconds: 0 });
    };

    console.log('[patch] connected to backend');
  }

  function wait() {
    const inst = findInstance();
    if (inst) {
      applyPatch(inst);
      console.log('[pdf2speech] patched OK', inst);
    } else {
      console.log('[pdf2speech] component not ready, retrying...');
      setTimeout(wait, 400);
    }
  }
  setTimeout(wait, 800);
})();
</script>
"""


@app.route("/")
def index():
    html = ORIGINAL_HTML.read_text(encoding="utf-8")
    html = html.replace("</html>", PATCH_SCRIPT + "</html>")
    return Response(html, mimetype="text/html")


@app.route("/process", methods=["POST"])
def process():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    suffix = Path(file.filename).suffix.lower()

    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {suffix}"}), 400

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        if suffix == ".pdf":
            text = extract_text(tmp_path)
        else:
            text = extract_text_docx(tmp_path)

        text = clean_text(text)
        segments = segment_text(text)[:3]
        audio_chunks = synthesize_segments(segments)

        mp3_buf = io.BytesIO()
        concatenate_audio(audio_chunks, mp3_buf)
        mp3_b64 = base64.b64encode(mp3_buf.getvalue()).decode()

        return jsonify({"audio": mp3_b64})

    except SystemExit as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    app.run(debug=True)
