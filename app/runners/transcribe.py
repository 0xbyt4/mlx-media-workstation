"""Speech to text runner (runs in the Drama 3 environment, mlx-audio 0.6.1): Qwen3-ASR 0.6B, the speech recogniser
that also checks the Drama 3 takes. With a "model" in the job (the Nemotron 3.5 Transcribe folder), the same steps run
that model instead; it needs mlx-audio 0.6.4 (the local LLM environment) and does not name the language. Nemotron's
CTC head times each word, so its subtitles come from 30 s parts with word times. With "speakers" in the job, Nemotron
transcribes the whole audio in one pass (its speaker numbers hold for one pass only) and each line names its speaker.

The audio (16 kHz mono, made by the job's plan) is cut into parts of at most PART seconds at quiet points, and each
part is transcribed on its own, so each line of transcript.txt has the time of its part and the log shows "part i/n"
for the progress. With no language given, the model names the language of each part.

With "subtitles" in the job, it also writes transcript.srt and transcript.vtt (subtitles.py builds the cues). Qwen3-ASR
gives no word times, only the times of its parts. With an "aligner" (Qwen3-ForcedAligner, by its Hugging Face name),
each part's text gets word times from it. Without one, the parts are short (SUB_PART seconds) and each part's words
share the time from its first to its last loud sound by their length.

  python transcribe.py job.json
"""

import itertools
import json
import re
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
# a last part shorter than this goes with the part before: the splitter pads it to 1 s of silence, and Nemotron writes
# a word ("The.") for that
MIN_TAIL = 1.0
SPEAKER_TAG = re.compile(
    r"<spk:(\d+)>"
)  # Nemotron's speaker tags in the text of a "saa" pass


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


def join_tail(parts: list, total: float, sr: int) -> list:
    """The parts, with a last part of under MIN_TAIL seconds joined to the part before (its padding left out)."""
    if len(parts) < 2 or total - parts[-1][1] >= MIN_TAIL:
        return parts
    (before, start), (tail, offset) = parts[-2], parts[-1]
    n = round((total - offset) * sr)
    return parts[:-2] + [(np.concatenate([before, tail[:n]]), start)]


def nemotron_words(
    model, part, job: dict, start: float, max_tokens: int, stamps: bool
) -> tuple[str, list]:
    """Nemotron's text of a part, and its words as (text, start, end, speaker) from the start of the audio. No words
    when the part gets no word times."""
    kw = {
        "language": job["language"],
        "max_tokens": max_tokens,
        "task": "saa" if job.get("speakers") else "asr",
    }
    if stamps:
        try:
            r = model.generate(part, word_timestamps=True, **kw)
            words = [
                (w["text"], start + w["start"], start + w["end"], w.get("speaker", 0))
                for w in r.segments or []
            ]
            return r.text, in_order(r.text, words) if job.get("speakers") else words
        except (
            ValueError
        ) as e:  # Thai, or a speaker tag that the diarization did not find
            print(f"word times: {e}", flush=True)
    return model.generate(part, **kw).text, []


def in_order(text: str, words: list) -> list:
    """The words of a "saa" pass in the order of its text, with start times that do not go back. mlx-audio aligns the
    words of each speaker apart and sorts all of them by time, so the first word of a turn can get a time in an
    earlier turn of another speaker. The words stay in time order when their count is not that of the text (Chinese and
    Japanese words are characters)."""
    bits = SPEAKER_TAG.split(text.replace("—", " "))
    tagged = [
        (0 if len(bits) == 1 else None, bits[0])
    ]  # mlx-audio's tag for the text before the first tag
    tagged += [(int(k), t) for k, t in zip(bits[1::2], bits[2::2], strict=True)]
    queues: dict = {}
    for w in words:
        queues.setdefault(w[3], []).append(w)
    counts: dict = {}
    for k, t in tagged:
        if t.split():
            counts[k] = counts.get(k, 0) + len(t.split())
    if counts != {k: len(q) for k, q in queues.items()}:
        return words
    out, floor = [], 0.0
    for k, t in tagged:
        said = t.split()
        for n in range(len(said)):
            w = queues[k].pop(0)
            a = max(w[1], floor)
            out.append((w[0], a, max(w[2], a), 0 if k is None else k))
            floor = (
                a if n < len(said) - 1 else max(w[2], a)
            )  # a turn starts after the turn before it
    return out


def turns(text: str, words: list, start: float, end: float, sep: str) -> list:
    """The speaker turns of a pass as (speaker, start, end, text, words). A turn longer than PART seconds goes on in a
    new one. Without word times, the turns come from the text's speaker tags and share the time of the part."""
    if not words:
        bits = SPEAKER_TAG.split(text)
        tagged = list(zip(bits[1::2], bits[2::2], strict=True))
        if bits[0].strip():
            tagged.insert(0, ("0", bits[0]))
        return [
            (int(k), start, end, " ".join(t.split()), [])
            for k, t in tagged
            if t.strip()
        ]
    out: list = []
    for w in words:
        last = out[-1] if out else None
        if last and last[0] == w[3] and w[1] - last[1] <= PART:
            last[2], last[4] = w[2], [*last[4], w]
        else:
            out.append([w[3], w[1], w[2], "", [w]])
    return [(k, a, b, sep.join(x[0] for x in ws), ws) for k, a, b, _, ws in out]


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
    nemotron = bool(job.get("model"))
    speakers = nemotron and bool(job.get("speakers"))
    stamps = nemotron and (subs or speakers)  # Nemotron's word times
    model = load_model(job.get("model") or ASR)
    aligner = (
        load_model(job["aligner"])
        if subs and job.get("aligner") and not nemotron
        else None
    )
    sr = model.sample_rate
    audio = np.array(load_audio(job["audio"], sr=sr))
    total = len(audio) / sr
    if speakers:
        parts = [(audio, 0.0)]
    elif subs and aligner is None and not nemotron:
        parts = split_audio_into_chunks(
            audio, sr=sr, chunk_duration=SUB_PART, search_expand_sec=SUB_SEARCH
        )
    else:
        parts = split_audio_into_chunks(audio, sr=sr, chunk_duration=PART)
    parts = join_tail(parts, total, sr)
    ends = [offset for _, offset in parts[1:]] + [total]
    lines, languages, cues, voices = [], [], [], set()
    for i, ((part, start), end) in enumerate(zip(parts, ends, strict=True), 1):
        tokens = int((end - start) * TOKENS_PER_SECOND) + 64
        if nemotron:
            text, words = nemotron_words(model, part, job, start, tokens, stamps)
            found = None
        else:
            result = model.generate(part, language=job["language"], max_tokens=tokens)
            text, words = result.text, []
            found = result.language[0] if isinstance(result.language, list) else None
        mx.clear_cache()
        language = job["language"] or found
        sep = "" if language in NO_SPACES else " "
        if speakers:
            for k, a, b, said, ws in turns(text, words, start, end, sep):
                voices.add(k)
                lines.append(f"[{clock(a)}-{clock(b)}] Speaker {k + 1}: {said}")
                if subs and ws:  # a turn's first subtitle names its speaker
                    first = (f"Speaker {k + 1}: {ws[0][0]}", ws[0][1], ws[0][2])
                    cues += subtitles.build([first] + [w[:3] for w in ws[1:]], sep)
            if lines and language and language not in languages:
                languages.append(language)
            text = ""  # the turns are the lines
        text = " ".join(SPEAKER_TAG.sub(" ", text).split())
        if text:
            lines.append(f"[{clock(start)}-{clock(end)}] {text}")
            if language and language not in languages:
                languages.append(language)
            if subs and words:
                cues += subtitles.build([w[:3] for w in words], sep)
            elif subs:
                spread, sep = timed_words(text, language, part, sr, start, aligner)
                cues += subtitles.build(spread, sep)
        print(f"part {i}/{len(parts)}: {clock(start)}-{clock(end)}", flush=True)
    if speakers:  # the cues of each turn are built apart: none may run into the next
        for a, b in itertools.pairwise(cues):
            if b.start > a.start:
                a.end = min(a.end, b.start)
    head = f"Language: {', '.join(languages) or 'unknown'}"
    if speakers:
        head += f"\nSpeakers: {len(voices)}"
    body = "\n".join(lines) or "(No speech was heard.)"
    (out / "transcript.txt").write_text(f"{head}\n\n{body}\n")
    heard = ", ".join(languages) or ("language not named" if lines else "no speech")
    notes = {"transcript.txt": f"{heard}, {clock(total)}"}
    if speakers:
        notes["transcript.txt"] += f", {len(voices)} speakers"
    if subs and cues:
        (out / "transcript.srt").write_text(subtitles.srt(cues))
        (out / "transcript.vtt").write_text(subtitles.vtt(cues))
        if aligner is not None:
            how = "word times from the aligner"
        elif stamps:
            how = "word times from Nemotron"
        else:
            how = "times from short parts, about +-1 s"
        notes["transcript.srt"] = notes["transcript.vtt"] = (
            f"{len(cues)} subtitles, {how}"
        )
    (out / "notes.json").write_text(json.dumps(notes))


if __name__ == "__main__":
    main()
