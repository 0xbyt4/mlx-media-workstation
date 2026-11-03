"""Speech to text runner (runs in the Drama 3 environment, mlx-audio 0.6.1): Qwen3-ASR 0.6B, the speech recogniser
that also checks the Drama 3 takes.

The audio (16 kHz mono, made by the job's plan) is cut into parts of at most PART seconds at quiet points, and each
part is transcribed on its own, so each line of transcript.txt has the time of its part and the log shows "part i/n"
for the progress. With no language given, the model names the language of each part.

  python transcribe.py job.json
"""

import json
import sys
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.stt.models.qwen3_asr.qwen3_asr import split_audio_into_chunks
from mlx_audio.stt.utils import load_audio, load_model

ASR = "mlx-community/Qwen3-ASR-0.6B-8bit"
PART = 30.0  # seconds of audio per transcript line
TOKENS_PER_SECOND = (
    12  # far above speech (about 4 tokens a second), so a part is never cut
)


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text())
    out = Path(job["out"])
    mx.set_cache_limit(1 << 30)
    model = load_model(ASR)
    sr = model.sample_rate
    audio = np.array(load_audio(job["audio"], sr=sr))
    total = len(audio) / sr
    parts = split_audio_into_chunks(audio, sr=sr, chunk_duration=PART)
    ends = [offset for _, offset in parts[1:]] + [total]
    lines, languages = [], []
    for i, ((part, start), end) in enumerate(zip(parts, ends, strict=True), 1):
        result = model.generate(
            part,
            language=job["language"],
            max_tokens=int((end - start) * TOKENS_PER_SECOND) + 64,
        )
        mx.clear_cache()
        text = " ".join(result.text.split())
        found = result.language[0] if isinstance(result.language, list) else None
        language = job["language"] or found
        if text:
            lines.append(f"[{clock(start)}-{clock(end)}] {text}")
            if language and language not in languages:
                languages.append(language)
        print(f"part {i}/{len(parts)}: {clock(start)}-{clock(end)}", flush=True)
    head = f"Language: {', '.join(languages) or 'unknown'}"
    body = "\n".join(lines) or "(No speech was heard.)"
    (out / "transcript.txt").write_text(f"{head}\n\n{body}\n")
    (out / "notes.json").write_text(
        json.dumps(
            {"transcript.txt": f"{', '.join(languages) or 'no speech'}, {clock(total)}"}
        )
    )


if __name__ == "__main__":
    main()
