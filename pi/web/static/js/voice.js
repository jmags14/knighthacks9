/*
 * StormBox browser voice (mic button). Plain JavaScript, no libraries, no CDN.
 *
 *   StormVoice.supported()          -> {ok, reason}  can this page record at all?
 *   StormVoice.start(onTick, onMax) -> Promise       start recording (asks for the mic)
 *   StormVoice.stop()               -> Promise<{blob, seconds}>
 *   StormVoice.cancel()             stop and throw the recording away
 *   StormVoice.transcribe(blob)     -> Promise<{text, stt_ms}>   offline Whisper on the server
 *
 * Why we convert to WAV in the browser: MediaRecorder gives webm/mp4, but the server wants plain
 * 16 kHz mono WAV. The browser can decode and resample it, so the server needs no ffmpeg.
 * Why not the browser's SpeechRecognition: Chrome sends that audio to Google. This stays offline.
 */
var StormVoice = (function () {
  var TARGET_RATE = 16000;     // Whisper wants 16 kHz mono
  var MAX_SECONDS = 14;        // the server rejects > 15 s, so stop a little before
  var MIN_SECONDS = 0.4;       // shorter than this is an accidental tap

  var recorder = null, stream = null, chunks = [], timer = null, startedAt = 0;

  // ---------------------------------------------------------------- can we record?
  function supported() {
    if (typeof window === "undefined" || !window.isSecureContext) {
      return { ok: false, reason: "The microphone needs HTTPS or localhost. Open this page at " +
                                  "http://localhost:8000 on the laptop, or type your question." };
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      return { ok: false, reason: "This browser cannot record audio. Try Chrome or Edge." };
    }
    return { ok: true };
  }

  function friendlyMicError(e) {
    var name = e && e.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "Microphone is blocked. Click the lock icon in the address bar and allow the microphone.";
    }
    if (name === "NotFoundError") return "No microphone found.";
    if (name === "NotReadableError") return "The microphone is busy in another app.";
    return "Could not start the microphone.";
  }

  // ---------------------------------------------------------------- recording
  function cleanup() {
    if (timer) { clearInterval(timer); timer = null; }
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }  // mic light off
    recorder = null;
  }

  function start(onTick, onMax) {
    return navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s;
      chunks = [];
      recorder = new MediaRecorder(s);
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.start();
      startedAt = Date.now();
      timer = setInterval(function () {
        var secs = (Date.now() - startedAt) / 1000;
        if (onTick) onTick(secs);
        if (secs >= MAX_SECONDS && onMax) { clearInterval(timer); timer = null; onMax(); }
      }, 200);
    }, function (e) { throw new Error(friendlyMicError(e)); });
  }

  function stop() {
    return new Promise(function (resolve) {
      if (!recorder || recorder.state === "inactive") { cleanup(); resolve(null); return; }
      var rec = recorder, seconds = (Date.now() - startedAt) / 1000;
      rec.onstop = function () {
        var blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        cleanup();
        resolve({ blob: blob, seconds: seconds });
      };
      rec.stop();
    });
  }

  function cancel() {
    if (recorder && recorder.state !== "inactive") { recorder.onstop = null; recorder.stop(); }
    cleanup();
  }

  // ---------------------------------------------------------------- recording -> 16 kHz mono WAV
  function writeText(view, offset, text) {
    for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  }

  // samples: Float32Array in [-1, 1]  ->  ArrayBuffer of a 16-bit mono PCM WAV file.
  function encodeWav(samples, rate) {
    var bytes = samples.length * 2;
    var buf = new ArrayBuffer(44 + bytes);
    var v = new DataView(buf);
    writeText(v, 0, "RIFF");  v.setUint32(4, 36 + bytes, true);
    writeText(v, 8, "WAVE");  writeText(v, 12, "fmt ");
    v.setUint32(16, 16, true);          // size of this block
    v.setUint16(20, 1, true);           // 1 = plain PCM
    v.setUint16(22, 1, true);           // mono
    v.setUint32(24, rate, true);        // sample rate
    v.setUint32(28, rate * 2, true);    // bytes per second
    v.setUint16(32, 2, true);           // bytes per sample
    v.setUint16(34, 16, true);          // bits per sample
    writeText(v, 36, "data"); v.setUint32(40, bytes, true);
    for (var i = 0; i < samples.length; i++) {
      var s = Math.max(-1, Math.min(1, samples[i]));          // clip
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
  }

  function blobToWav16k(blob) {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    var ctx = new Ctx();
    return blob.arrayBuffer().then(function (ab) {
      return new Promise(function (resolve, reject) { ctx.decodeAudioData(ab, resolve, reject); });
    }).then(function (decoded) {
      ctx.close();
      // An OfflineAudioContext at 16 kHz resamples and mixes down to mono for us.
      var frames = Math.max(1, Math.ceil(decoded.duration * TARGET_RATE));
      var off = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, frames, TARGET_RATE);
      var src = off.createBufferSource();
      src.buffer = decoded;
      src.connect(off.destination);
      src.start();
      return off.startRendering();
    }).then(function (rendered) {
      return new Blob([encodeWav(rendered.getChannelData(0), TARGET_RATE)], { type: "audio/wav" });
    });
  }

  // ---------------------------------------------------------------- send to the server (offline Whisper)
  function transcribe(blob) {
    return blobToWav16k(blob).then(function (wav) {
      return fetch("/api/voice/stt", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
    }, function () {
      throw new Error("Could not read that recording. Please try again.");
    }).then(function (r) {
      if (r.ok) return r.json();
      return r.json().then(function (j) { throw new Error(j.detail || "Speech to text failed."); },
                           function () { throw new Error("Speech to text failed (HTTP " + r.status + ")."); });
    }, function (e) {
      if (e instanceof TypeError) throw new Error("Could not reach the box.");
      throw e;
    });
  }

  return {
    supported: supported, start: start, stop: stop, cancel: cancel, transcribe: transcribe,
    encodeWav: encodeWav, MAX_SECONDS: MAX_SECONDS, MIN_SECONDS: MIN_SECONDS
  };
})();

if (typeof module !== "undefined") module.exports = StormVoice;   // lets Node test encodeWav
