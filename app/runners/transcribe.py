"""Speech to text runner (runs in the Drama 3 environment, mlx-audio 0.6.1): Qwen3-ASR 0.6B, the speech recogniser
that also checks the Drama 3 takes.

The audio (16 kHz mono, made by the job's plan) is cut into parts of at most PART seconds at quiet points, and each
part is transcribed on its own, so each line of transcript.txt has the time of its part and the log shows "part i/n"
for the progress. With no language given, the model names the language of each part.

With "subtitles" in the job, it also writes transcript.srt and transcript.vtt (subtitles.py builds the cues). Qwen3-ASR
gives no word times, only the times of its parts. With an "aligner" (Qwen3-ForcedAligner, by its Hugging Face name),
each part's text gets word times from it. Without one, the parts are short (SUB_PART seconds) and each part's words
share the time from its first to its last loud sound by their length.

  python transcribe.py job.json
"""

import json
import sys
from pathlib import Path

import mlx.core as mx
import numpy as np
import subtitles
from mlx_audio.stt.models.qwen3_asr.qwen3_asr import split_audio_into_chunks
from mlx_audio.stt.utils import load_audio, load_model

ASR = "mlx-community/Qwen3-ASR-0.6B-8bit"
PART = 30.0  # seconds of audio per transcript line
# subtitles without an aligner: parts of about this length, cut at the quietest point up to SUB_SEARCH seconds away
SUB_PART, SUB_SEARCH = 6.0, 2.0
TOKENS_PER_SECOND = (
    12  # far above speech (about 4 tokens a second), so a part is never cut
)
# languages written without spaces: a subtitle line is cut between characters
NO_SPACES = ("Chinese", "Cantonese", "Japanese")
FRAME = 0.05  # seconds of one loudness window when the speech in a part is found
QUIET_DB = 35.0  # a window this much under the part's loudest one is not speech


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def speech_span(part, sr: int) -> tuple[float, float]:
    """Seconds from the part's start to its first and last loud window (the whole part when it is silent)."""
    n = int(sr * FRAME)
    count = len(part) // n
    if count == 0:
        return 0.0, len(part) / sr
    power = (
        np.asarray(part[: count * n], dtype=np.float32).reshape(count, n) ** 2
    ).mean(axis=1)
    db = 10 * np.log10(power + 1e-12)
    loud = np.nonzero(db > db.max() - QUIET_DB)[0]
    if len(loud) == 0:
        return 0.0, len(part) / sr
    return float(loud[0] * FRAME), float((loud[-1] + 1) * FRAME)


def timed_words(text, language, part, sr, start, aligner) -> tuple[list, str]:
    """The part's words with times from the start of the audio, and the text between words ("" or " ")."""
    sep = "" if language in NO_SPACES else " "
    units = list(text.replace(" ", "")) if not sep else text.split()
    if aligner is not None:
        try:
            items = list(
                aligner.generate(part, text=text, language=language or "English")
            )
            mx.clear_cache()
        except Exception as e:  # noqa: BLE001 - a part that cannot be aligned keeps the spread times
            print(f"aligner: {type(e).__name__}: {e}", flush=True)
            items = []
        if items:
            if not sep:  # one item a character; punctuation is not aligned
                return [
                    (i.text, start + i.start_time, start + i.end_time) for i in items
                ], sep
            # the aligner drops punctuation and may split a word: give each word the times of its items
            out, k = [], 0
            for word in units:
                n = len(aligner.aligner_processor.tokenize_space_lang(word))
                if n == 0:  # punctuation only: it goes with the word before
                    if out:
                        out[-1] = (out[-1][0] + " " + word, out[-1][1], out[-1][2])
                    continue
                got = items[k : k + n]
                if len(got) < n:
                    break
                out.append((word, start + got[0].start_time, start + got[-1].end_time))
                k += n
            else:
                return out, sep
            print(
                "aligner: the words did not match; this part keeps the spread times",
                flush=True,
            )
    a, b = speech_span(part, sr)
    return subtitles.spread(units, start + a, start + b), sep


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text())
    out = Path(job["out"])
    subs = bool(job.get("subtitles"))
    mx.set_cache_limit(1 << 30)
    model = load_model(ASR)
    aligner = load_model(job["aligner"]) if subs and job.get("aligner") else None
    sr = model.sample_rate
    audio = np.array(load_audio(job["audio"], sr=sr))
    total = len(audio) / sr
    if subs and aligner is None:
        parts = split_audio_into_chunks(
            audio, sr=sr, chunk_duration=SUB_PART, search_expand_sec=SUB_SEARCH
        )
    else:
        parts = split_audio_into_chunks(audio, sr=sr, chunk_duration=PART)
    ends = [offset for _, offset in parts[1:]] + [total]
    lines, languages, cues = [], [], []
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
            if subs:
                words, sep = timed_words(text, language, part, sr, start, aligner)
                cues += subtitles.build(words, sep)
        print(f"part {i}/{len(parts)}: {clock(start)}-{clock(end)}", flush=True)
    head = f"Language: {', '.join(languages) or 'unknown'}"
    body = "\n".join(lines) or "(No speech was heard.)"
    (out / "transcript.txt").write_text(f"{head}\n\n{body}\n")
    notes = {"transcript.txt": f"{', '.join(languages) or 'no speech'}, {clock(total)}"}
    if subs and cues:
        (out / "transcript.srt").write_text(subtitles.srt(cues))
        (out / "transcript.vtt").write_text(subtitles.vtt(cues))
        how = (
            "word times from the aligner"
            if aligner is not None
            else "times from short parts, about +-1 s"
        )
        notes["transcript.srt"] = notes["transcript.vtt"] = (
            f"{len(cues)} subtitles, {how}"
        )
    (out / "notes.json").write_text(json.dumps(notes))


if __name__ == "__main__":
    main()
