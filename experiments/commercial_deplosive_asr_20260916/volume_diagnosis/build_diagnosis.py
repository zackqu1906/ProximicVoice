from pathlib import Path
import hashlib
import html
import json
import re
import numpy as np
import soundfile as sf
from scipy import signal

ROOT = Path(__file__).resolve().parent
SOURCE = Path('/Users/admin/Downloads/ProximicVoice-main/experiments/commercial_deplosive_asr_20260916')
design = json.loads((ROOT/'design.json').read_text())
raw = json.loads((ROOT/'doubao_volume.json').read_text())
spec = json.loads((ROOT/'spectral_analysis.json').read_text())
old = json.loads((SOURCE/'sentences_results.json').read_text())['cases']
prompts = {c['sentence']: c['reading_prompt'] for c in old}
groups = ['original_high', 'original_low', 'commercial_low', 'commercial_high']
labels = {'original_high': '原音／高电平', 'original_low': '原音／低电平', 'commercial_low': '商业版／低电平', 'commercial_high': '商业版／高电平'}
cases = {}
for c in raw['cases'].values():
    assert 'doubao' in c['results'], 'Wait for all 24 cases before reporting.'
    assert c['results']['doubao']['error'] is None, c
    cases[c['sentence'], c['group']] = c
assert len(cases) == 24
coverage = {g: sum(bool(cases[n,g]['results']['doubao']['text']) for n in design['selected_sentences']) for g in groups}

checks = []
for rec in design['records']:
    n = rec['sentence']
    original, fs = sf.read(SOURCE/'sentences'/f'{n:02d}_original.wav')
    filt = signal.butter(4, [500, 4000], btype='bandpass', fs=fs, output='sos')
    b = signal.sosfilt(filt, original)
    env = signal.convolve(b*b, np.ones(640)/640, mode='same')
    mask = env > max(1e-6, .03*np.quantile(env, .8))
    measured = {}
    for g in groups:
        p = ROOT/'inputs'/f'{n:02d}_{g}.wav'
        x, sr = sf.read(p)
        assert sr == 16000 and len(x) == len(original)
        assert hashlib.sha256(p.read_bytes()).hexdigest() == rec['metrics'][g]['sha256']
        assert np.max(np.abs(x)) <= .95004
        filtered = signal.sosfilt(filt, x)
        measured[g] = 10*np.log10(np.mean(filtered[mask]**2))
    high_diff = float(measured['commercial_high']-measured['original_high'])
    low_diff = float(measured['commercial_low']-measured['original_low'])
    assert abs(high_diff) < .01 and abs(low_diff) < .01
    checks.append({'sentence': n, 'high_level_mismatch_db': high_diff, 'low_level_mismatch_db': low_diff})

def norm(s):
    return re.sub(r'[^a-z0-9\u3400-\u9fff]', '', s.lower())

def distance(a, b):
    p = list(range(len(b)+1))
    for i, x in enumerate(a, 1):
        q = [i]
        for j, y in enumerate(b, 1):
            q.append(min(p[j]+1, q[-1]+1, p[j-1]+(x != y)))
        p = q
    return p[-1]

chinese = [n for n in design['selected_sentences'] if n < 9]
denominator = sum(len(norm(prompts[n])) for n in chinese)
cer = {g: sum(distance(norm(prompts[n]), norm(cases[n,g]['results']['doubao']['text'])) for n in chinese)/denominator for g in groups}
summary = {'nonempty_count_out_of_6': coverage, 'api_errors': 0, 'level_match_verification': checks,
           'prompt_cer_auxiliary_5_chinese_sentences': cer, 'prompt_reference_characters': denominator,
           'caveats': ['Nonempty text is not recognition accuracy.', 'Screen prompts are not manually transcribed ground truth.', 'One cloud run per condition; results can vary.', 'Energy removed includes intended noise/plosive suppression; no clean reference used to quantify artifacts.'],
           'cloud_authorization': 'User explicitly approved these 24 gain variants before this run.'}
(ROOT/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
design['cloud'] = 'User explicitly approved the 24 gain variants on 2026-09-16 before execution.'
(ROOT/'design.json').write_text(json.dumps(design, ensure_ascii=False, indent=2))

lines = ['# 豆包识别退化：音量与处理变化对照', '',
         '## 结论', '',
         '补偿音量未恢复原音的识别结果。当前证据支持：整体音量降低不能充分解释退化；明显的中低频削弱及随处理改变的声音线索也需要考虑。不能仅凭这份对照认定为某种 artifact，或断言云端内部 VAD / 置信度门限的具体行为。', '',
         '## 对照设计', '',
         '- 第 1、3、6、7、8、9 句，共 24 个输入；每句各测试原音高/低电平及商业版高/低电平。',
         '- 同一句话两版精确对应，商业文件额外约 10 秒静音造成的时间偏移已补偿。',
         '- 用原音确定共同活动区域，按 500–4000 Hz 能量匹配整体标量增益。测量滤波不作用于实际 ASR 输入。',
         '- 商业版补偿约 +1.66 至 +5.34 dB；高/低电平对应组的语音频段 RMS 相同。',
         '- 四版统一保留峰值余量，避免新增削波。高电平原音在部分句子会整体降低最多 1.97 dB；这不是原文件逐样本重测。',
         '- PCM16 / 单声道 / 16 kHz，主项目 compare_asr_audio.py + 实际豆包后端，按实时速率送入，无新增滤波或文字纠错。',
         '- 检查实际落盘 WAV 的哈希、时长、采样率、峰值与匹配误差；所有配对电平误差小于 0.01 dB，24 个请求均无 API 错误。',
         '- 用户明确授权这 24 个音量版本后执行云端测试。', '',
         '## 逐句原始输出', '',
         '| 句子 | 原音高电平 | 原音低电平 | 商业版低电平 | 商业版补回电平 |',
         '|---|---|---|---|---|']
for n in design['selected_sentences']:
    texts = [cases[n,g]['results']['doubao']['text'] or '（空）' for g in groups]
    lines.append('| '+str(n)+' | '+' | '.join(texts)+' |')
lines += ['', '有文字的句数（不是准确率）：'+'；'.join(f'{labels[g]} {coverage[g]}/6' for g in groups)+'。', '',
          '第 7 句是较清楚的反例：原音降到商业版相同电平后仍完整识别；商业版升回相同电平后仍明显错词。第 6 句商业版反而改善，说明处理效果随具体片段变化。', '',
          '## 本地频段测量', '',
          '以下是全部 9 句、同一原音活动区域内的分频段 RMS 变化；负值表示商业版变弱。不是总体听感响度、语音失真分数或 artifact 分数。', '',
          '| 频段 | 商业版相对原音 |', '|---|---:|']
for b in spec['bands_aggregate']:
    lines.append(f'| {b["band_hz"][0]}–{b["band_hz"][1]} Hz | {b["change_db"]:.2f} dB |')
lines += ['',
          '这呈现明显的频率选择性。整体调大可以补充音量，却同时放大未被削弱的高频，无法复原被压低的频谱比例。被移除的低频也包含目标爆破噪声，因此这些数字本身不能量化误删了多少语音。', '',
          '## 怎样解释 artifact', '',
          'Artifact 指处理引入的非自然失真，例如颗粒声、金属声或时间上的涂抹；另外一种问题是原有语音线索被过度压低。两者都可能妨碍识别，但本实验更直接确认的是频谱改变和增益补偿无力恢复识别，并没有把两类影响分开量化。', '',
          '空文本发生在 API 成功返回的情况下，主项目该离线测试路径没有客户端低音量/VAD拦截；可能与云端语音检测或解码决策相关，但服务内部原因没有观测证据。', '',
          'Acon 官方说明 DePlosive:Dialogue 使用深度学习，并可在全频段或分频段调整检测灵敏度。本次用户确认只启用了 DePlosive；具体软件版本和参数尚未记录，不能据此断言所有默认设置都会产生本次结果。', '',
          '## 后续使用建议', '',
          '降低降爆破强度/检测灵敏度，保留正常语音段；在相同语音电平下同时评估听感和逐句 ASR。较低的噪声不保证较高的可识别性。对自研模型，应同时检查非爆破语音的保真程度。', '',
          '## 资料', '',
          '- [Acon DePlosive:Dialogue 官方说明](https://acondigital.com/products/acoustica/acoustica-features)',
          '- [Iwamoto 等，Interspeech 2022：How bad are artifacts?](https://www.isca-archive.org/interspeech_2022/iwamoto22_interspeech.html)', '',
          '## 文件', '',
          '- `doubao_volume.json`：主项目原始返回，识别文字未改写。',
          '- `inputs/`：实际送入的 24 个音量版本。',
          '- `design.json`：设计、增益、峰值、每份输入哈希。',
          '- `spectral_analysis.json`：9 句分频段结果。',
          '- `summary.json`：出字数量及配对验证；提示词 CER 仅辅助排查，不是真值准确率。',
          '- `analyze_spectrum.py`、`build_diagnosis.py`：本地分析与报告生成。', '']
(ROOT/'诊断报告.md').write_text('\n'.join(lines))

cards = []
for n in design['selected_sentences']:
    cells = []
    for g in groups:
        c = cases[n,g]
        cells.append(f'<div><h3>{labels[g]}</h3><audio controls preload="none" src="inputs/{n:02d}_{g}.wav"></audio><p>{html.escape(c["results"]["doubao"]["text"] or "（空文本）")}</p></div>')
    cards.append(f'<section><h2>第 {n} 句</h2><p>{html.escape(prompts[n])}</p><div class="grid">'+''.join(cells)+'</div></section>')
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>豆包音量对照</title><style>body{font:16px/1.6 -apple-system,sans-serif;background:#f4f6f5;color:#24352f;margin:0}main{max-width:1180px;margin:auto;padding:30px 24px}section{background:white;border:1px solid #dce4df;border-radius:12px;padding:22px;margin:20px 0}h1{font-size:28px}h2{font-size:20px}h3{font-size:15px}a{color:#147e65}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}audio{width:100%;height:35px}p{overflow-wrap:anywhere}.note{font-size:13px;color:#63736b}@media(max-width:850px){.grid{grid-template-columns:1fr 1fr}}@media(max-width:500px){.grid{grid-template-columns:1fr}}</style><main><a href="../">← 返回三模型原始对比</a><h1>变小的音量能解释识别退化吗？</h1><section><h2>补回音量，仍未恢复原音的识别表现</h2><p>同一句话分为四种情况。原音高电平与商业版高电平配对，原音低电平与商业版低电平配对；按 500–4000 Hz 语音频段 RMS 匹配。</p><p>商业版显著削弱了中低频。整体放大无法恢复原有频谱比例；当前实验尚不能单独量化 artifact 与语音误抑制。</p><p class="note">每种情况测试一次。所有音频保留共同峰值余量，未新增削波。朗读提示未人工核听，不是真值转写。</p><a href="诊断报告.md">完整诊断</a> · <a href="summary.json">汇总与验证</a> · <a href="doubao_volume.json">豆包原始输出</a></section>__CARDS__</main><script>document.querySelectorAll('audio').forEach(a=>a.addEventListener('play',()=>document.querySelectorAll('audio').forEach(b=>{if(a!==b)b.pause()})));</script></html>'''
(ROOT/'index.html').write_text(page.replace('__CARDS__', ''.join(cards)))
print(json.dumps(summary, ensure_ascii=False, indent=2))
for n in design['selected_sentences']:
    print(n, {g: cases[n,g]['results']['doubao']['text'] for g in groups})
