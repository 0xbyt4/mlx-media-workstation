"""Fish Drama 3 runner (runs in the Drama 3 environment, mlx-audio 0.6.1): N takes of a script, each checked.

Settings and checks follow docs/PROMPTING.md: line breaks removed (they end a turn early), temperature 1.0 / top_p 0.9,
max_tokens about 2.5x the expected length of the longest chunk (21.5 frames a second), streamed decode, and a take is redrawn once when it is far shorter
or longer than the words need, holds 3 s or more of near-silence, or the speech recogniser misses over 15 % of the
words. The cache is capped and cleared between takes, so memory does not creep up over a session. The model's raw speech is
often about 10 dB quiet (-26 LUFS), so each take is raised toward -16 LUFS; the gain stops where the loudest sample
reaches -1 dBFS, so nothing clips (peaky speech then ends under -16 LUFS).

  python drama3.py job.json
"""

import json
import re
import sys
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.stt.utils import load_model as load_stt
from mlx_audio.tts.models.fish_drama3.chunking import split_text
from mlx_audio.tts.utils import load_model as load_tts
from scipy.io import wavfile
from scipy.signal import lfilter

ASR = "mlx-community/Qwen3-ASR-0.6B-8bit"
WORDS_PER_SECOND = 2.5
TARGET_LUFS, MAX_PEAK_DB = -16.0, -1.0


def spoken(text: str) -> str:
    return re.sub(r"<\|[^|]*\|>|\[[^\]]*\]|</?\w+>", " ", text)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def missing_share(expected: str, heard: str) -> float:
    exp, got = words(expected), set(words(heard))
    return sum(w not in got for w in exp) / max(len(exp), 1)


def longest_silence(x: np.ndarray, sr: int) -> float:
    hop = sr // 20
    rms = np.sqrt(np.convolve(x**2, np.ones(hop) / hop, mode="same")[::hop] + 1e-12)
    quiet = rms < 10 ** (-45 / 20)
    best = run = 0
    for q in quiet:
        run = run + 1 if q else 0
        best = max(best, run)
    return best / 20


def k_weight(sr: int) -> list[tuple[list[float], list[float]]]:
    """The two ITU-R BS.1770 K-weighting filters (high shelf, then high pass) for a sample rate, as (b, a)."""
    out = []
    for f0, q, gain_db, shelf in (
        (1681.974450955533, 0.7071752369554196, 3.999843853973347, True),
        (38.13547087602444, 0.5003270373238773, 0.0, False),
    ):
        k = np.tan(np.pi * f0 / sr)
        a0 = 1 + k / q + k * k
        a = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
        if shelf:
            vh, vb = 10 ** (gain_db / 20), 10 ** (gain_db / 40)
            b = [
                (vh + vb * k / q + k * k) / a0,
                2 * (k * k - vh) / a0,
                (vh - vb * k / q + k * k) / a0,
            ]
        else:
            b = [1.0, -2.0, 1.0]
        out.append((b, a))
    return out


def loudness(x: np.ndarray, sr: int) -> float | None:
    """Integrated loudness of a mono signal in LUFS (BS.1770: 400 ms blocks, 75 % overlap, gates at -70 LUFS and
    10 LU under the mean); None when it is all under the -70 LUFS gate."""
    for b, a in k_weight(sr):
        x = lfilter(b, a, x)
    size, step = int(0.4 * sr), int(0.1 * sr)
    if len(x) < size:
        return None
    power = np.array(
        [np.mean(x[i : i + size] ** 2) for i in range(0, len(x) - size + 1, step)]
    )
    lufs = -0.691 + 10 * np.log10(np.maximum(power, 1e-12))
    power = power[lufs > -70]
    if not len(power):
        return None
    mean = -0.691 + 10 * np.log10(np.mean(power))
    power = power[-0.691 + 10 * np.log10(power) > mean - 10]
    return float(-0.691 + 10 * np.log10(np.mean(power)))


def level(x: np.ndarray, sr: int) -> tuple[np.ndarray, float | None]:
    """The take set to TARGET_LUFS, held under MAX_PEAK_DB; with the loudness it had (None for silence)."""
    measured = loudness(x, sr)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if measured is None or peak == 0:
        return x, measured
    gain_db = min(TARGET_LUFS - measured, MAX_PEAK_DB - 20 * np.log10(peak))
    return (x * 10 ** (gain_db / 20)).astype(np.float32), measured


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text())
    out = Path(job["out"])
    text = " ".join(job["text"].split())  # one line: a line break can end a turn early
    expected = max(1.5, len(words(spoken(text))) / WORDS_PER_SECOND)
    # max_tokens caps each chunk the model splits the text into, so it is sized for the longest chunk, not the
    # whole text: a runaway chunk then stops early instead of growing into the next chunks' history
    longest = max(
        max(1.5, len(words(spoken(c))) / WORDS_PER_SECOND) for c in split_text(text)
    )
    kw = {
        "temperature": job["temperature"],
        "top_p": job["top_p"],
        "top_k": job["top_k"],
        "max_tokens": int(longest * 2.5 * 21.5) + 40,
        # decode every 4 s of frames instead of a whole chunk at once: same audio (SNR 69 dB), peak -3.5 GB
        "stream": True,
        "streaming_interval": 4.0,
    }
    if job["refs"]:
        kw["ref_audio"] = [r["audio"] for r in job["refs"]]
        # tag each transcript with its own speaker slot, so a lone speaker-1 reference is not read as speaker 0
        kw["ref_text"] = [
            r["text"]
            if r["text"].startswith("<|speaker:")
            else f"<|speaker:{r['speaker']}|> {r['text']}"
            for r in job["refs"]
        ]
        if len(kw["ref_audio"]) == 1 and job["refs"][0]["speaker"] == 0:
            kw["ref_audio"], kw["ref_text"] = (
                kw["ref_audio"][0],
                re.sub(r"^<\|speaker:\d+\|>\s*", "", kw["ref_text"][0]),
            )
    mx.set_cache_limit(1 << 30)
    tts, asr = load_tts(job["model"]), load_stt(ASR)
    sr = tts.sample_rate
    seed0 = (
        int(job["seed"])
        if job.get("seed") is not None
        else int.from_bytes(np.random.bytes(3), "little")
    )
    notes, n_ok = {}, 0
    for k in range(job["takes"]):
        best = None
        for attempt in range(2):
            seed = seed0 + 100 * k + attempt
            mx.random.seed(seed)
            audio = np.concatenate(
                [np.asarray(r.audio, np.float32) for r in tts.generate(text=text, **kw)]
            )
            mx.clear_cache()
            audio, _ = level(audio, sr)
            path = out / f"take{k + 1}.wav"
            wavfile.write(path, sr, audio)
            heard = asr.generate(str(path)).text
            dur, miss, sil = (
                len(audio) / sr,
                missing_share(spoken(text), heard),
                longest_silence(audio, sr),
            )
            problems = [
                p
                for p, bad in (
                    (
                        f"{dur:.1f} s for ~{expected:.1f} s of words",
                        not 0.35 * expected <= dur <= 2.3 * expected,
                    ),
                    (f"{sil:.1f} s silence", sil >= 3.0),
                    (f"{miss:.0%} of words not heard", miss > 0.15),
                )
                if bad
            ]
            best = (seed, dur, heard, problems)
            print(
                f"take {k + 1} seed {seed}: {dur:.1f} s, {'ok' if not problems else '; '.join(problems)} | {heard}",
                flush=True,
            )
            if not problems:
                break
        seed, dur, heard, problems = best
        n_ok += not problems
        status = (
            "checked ok" if not problems else "CHECK FAILED: " + "; ".join(problems)
        )
        notes[f"take{k + 1}.wav"] = (
            f"seed {seed}, {dur:.1f} s, {status}. Heard: {heard}"
        )
    (out / "notes.json").write_text(json.dumps(notes, indent=1))
    print(
        f"{n_ok}/{job['takes']} takes passed the check; peak {mx.get_peak_memory() / 1e9:.1f} GB"
    )


if __name__ == "__main__":
    main()
