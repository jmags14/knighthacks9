"""Run ONCE, with internet. Downloads the voice models into ai/models/voice/.
After this the voice pipeline never touches the network.

    python ai/scripts/download_voice_models.py

Downloads:
  - faster-whisper "tiny.en" (speech to text)  from Hugging Face  Systran/faster-whisper-tiny.en
  - one Piper voice (.onnx + .onnx.json)       from Hugging Face  rhasspy/piper-voices
Names come from ai/voice/config.py, so change them there to fetch a different model.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from huggingface_hub import hf_hub_download, snapshot_download

from ai.voice import config


def main():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading speech-to-text model '{config.STT_MODEL}' ...")
    snapshot_download(f"Systran/faster-whisper-{config.STT_MODEL}", local_dir=config.STT_DIR)

    print(f"Downloading Piper voice '{config.PIPER_VOICE}' ...")
    base = config.piper_repo_path()
    for ext in (".onnx", ".onnx.json"):
        # hf_hub_download keeps the repo's folder layout under local_dir, so we copy the file up one level.
        got = Path(hf_hub_download("rhasspy/piper-voices", base + ext, local_dir=config.VOICE_DIR / "_dl"))
        got.replace(config.VOICE_DIR / got.name)
    print(f"Done. Models are in {config.VOICE_DIR}")


if __name__ == "__main__":
    main()
