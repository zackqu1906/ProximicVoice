"""Local spectral diagnostics. Differences are not an artifact/clean-speech metric."""
from pathlib import Path
import json
import numpy as np
import soundfile as sf
from scipy import signal

ROOT = Path(__file__).resolve().parent
SOURCE = Path('/Users/admin/Downloads/ProximicVoice-main/experiments/commercial_deplosive_asr_20260916')
FS = 16000
BANDS = [(20, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 7900)]

def band(x, lo, hi):
    return signal.sosfilt(signal.butter(4, [lo, hi], btype='bandpass', fs=FS, output='sos'), x)

def db(v):
    return float(10*np.log10(max(v, 1e-20)))

records = []
energies = np.zeros((len(BANDS), 2))
for n in range(1, 10):
    x, sx = sf.read(SOURCE / 'sentences' / f'{n:02d}_original.wav')
    y, sy = sf.read(SOURCE / 'sentences' / f'{n:02d}_commercial.wav')
    assert sx == sy == FS and x.shape == y.shape
    xb, yb = band(x, 500, 4000), band(y, 500, 4000)
    envelope = signal.convolve(xb**2, np.ones(640)/640, mode='same')
    mask = envelope > max(1e-6, .03*np.quantile(envelope, .8))
    gain = np.sqrt(np.sum(xb[mask]**2)/np.sum(yb[mask]**2))
    changes = []
    for j, (lo, hi) in enumerate(BANDS):
        ex, ey = np.sum(band(x, lo, hi)[mask]**2), np.sum(band(y, lo, hi)[mask]**2)
        energies[j] += [ex, ey]
        changes.append({'band_hz': [lo, hi], 'change_db': db(ey/ex), 'gain_matched_change_db': db(ey/ex)+20*np.log10(gain)})
    # 40 ms windows / 10 ms hops; exclude low original-energy windows.
    window = np.ones(640)/640
    px = signal.convolve(xb**2, window, mode='valid')[::160]
    py = signal.convolve(yb**2, window, mode='valid')[::160]
    active = px > max(1e-6, .1*np.quantile(px, .8))
    ratio = 10*np.log10(np.maximum(py[active], 1e-20)/px[active])+20*np.log10(gain)
    records.append({'sentence': n, 'gain_match_db': float(20*np.log10(gain)), 'bands': changes,
                    'gain_matched_40ms_500_4000_change_db_percentiles_10_50_90': np.quantile(ratio, [.1, .5, .9]).tolist()})

report = {'method': 'Butterworth order 4 energy, same original-derived active mask for both signals; aligned nine prompt intervals. Relative level is commercial/original. Scalar gain match uses 500–4000 Hz.',
          'caveat': 'Frequency/time selective changes show this is not constant attenuation. They include intended noise/plosive removal and cannot alone establish lost speech or artifacts without a clean reference.',
          'bands_aggregate': [{'band_hz': list(b), 'change_db': db(e[1]/e[0])} for b, e in zip(BANDS, energies)],
          'sentences': records}
(ROOT/'spectral_analysis.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({'bands_aggregate': report['bands_aggregate'], 'sentence7': records[6]}, ensure_ascii=False, indent=2))
