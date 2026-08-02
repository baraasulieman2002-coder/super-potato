#!/usr/bin/env python3
"""
Minimal synthesized sound design for the Nebula Nest motion-graphics cut.
No licensed music library is available here, so this is a placeholder pad
+ a few spot SFX (ignition, shimmer/lock-in, bell) - a real score/mix
should replace this before publishing.
"""
import numpy as np
import wave

SR = 44100
T_TOTAL = 25.0

def env_exp(n, decay):
    t = np.arange(n) / SR
    return np.exp(-t * decay)

def sine(freq, n, phase=0.0):
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * freq * t + phase)

def add(buf, sound, start_s):
    i0 = int(start_s * SR)
    i1 = min(len(buf), i0 + len(sound))
    if i0 >= len(buf):
        return
    buf[i0:i1] += sound[: i1 - i0]

n_total = int(T_TOTAL * SR)
buf = np.zeros(n_total, dtype=np.float32)

# --- soft ambient pad: slow swell across the whole piece, low volume ---
t = np.arange(n_total) / SR
pad = (
    0.5 * np.sin(2 * np.pi * 55 * t)
    + 0.3 * np.sin(2 * np.pi * 82.4 * t)
    + 0.2 * np.sin(2 * np.pi * 110 * t)
)
swell = np.clip((t - 0.5) / 10.0, 0, 1) ** 1.5          # builds in
swell *= np.clip((T_TOTAL - t) / 4.0, 0, 1) ** 0.6       # releases at the end
swell *= (0.06 + 0.03 * np.sin(2 * np.pi * 0.1 * t))      # gentle amplitude motion
buf += (pad * swell).astype(np.float32)

# --- ignition whoosh at 0:00.5 ---
n = int(0.9 * SR)
sweep = np.linspace(180, 40, n)
ph = 2 * np.pi * np.cumsum(sweep) / SR
whoosh = np.sin(ph) * env_exp(n, 4.0)
noise = np.random.default_rng(1).normal(0, 1, n) * env_exp(n, 6.0) * 0.15
add(buf, 0.5 * (whoosh + noise), 0.5)

# --- rising shimmer + lock-in chime around the wordmark lock (~11.0s absolute) ---
n = int(1.2 * SR)
rng = np.random.default_rng(2)
shimmer = rng.normal(0, 1, n)
# bandpass-ish via simple difference filter cascade
for _ in range(3):
    shimmer = np.convolve(shimmer, [1, -0.7], mode="same")
shimmer *= np.linspace(0, 1, n) ** 2 * 0.05
add(buf, shimmer, 9.9)

n2 = int(1.6 * SR)
chime = (
    sine(587.33, n2) * 0.5 + sine(880.0, n2) * 0.3 + sine(1174.7, n2) * 0.2
) * env_exp(n2, 2.2) * 0.12
add(buf, chime, 11.05)

# --- soft bell when the title lands (~17.5s absolute: 14 + 0.55*5 approx) ---
n3 = int(2.0 * SR)
bell = (
    sine(392.0, n3) * 0.5 + sine(784.0, n3) * 0.25 + sine(1176.0, n3) * 0.12
) * env_exp(n3, 1.6) * 0.14
add(buf, bell, 17.6)

# --- fade the whole mix in the last 0.6s to match the visual fade to black ---
fade_start = T_TOTAL - 0.8
fade_mask = np.ones(n_total, dtype=np.float32)
fade_idx = t > fade_start
fade_mask[fade_idx] = np.clip((T_TOTAL - t[fade_idx]) / 0.8, 0, 1)
buf *= fade_mask

buf = np.clip(buf, -1.0, 1.0)
pcm = (buf * 32767).astype(np.int16)

with wave.open("/home/user/super-potato/render/audio.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())

print("wrote audio.wav")
