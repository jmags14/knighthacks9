// Run by test_voice_frontend.py:  node encode_wav_check.js <out.wav>
// Encodes known samples with the browser's StormVoice.encodeWav and writes the result to a file,
// so Python can open it with its own `wave` module (an independent check that the WAV is valid).
const fs = require("fs");
const path = require("path");
const StormVoice = require(path.join(__dirname, "..", "..", "..", "pi", "web", "static", "js", "voice.js"));

// 0, +1 (full scale), -1, then values past the limits (must be clipped), then a quiet value
const samples = new Float32Array([0, 1, -1, 2.5, -3.0, 0.5, -0.25]);
const buf = StormVoice.encodeWav(samples, 16000);
fs.writeFileSync(process.argv[2], Buffer.from(buf));
console.log(JSON.stringify({ bytes: buf.byteLength, maxSeconds: StormVoice.MAX_SECONDS, minSeconds: StormVoice.MIN_SECONDS }));
