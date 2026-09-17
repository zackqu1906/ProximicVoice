from pathlib import Path
import csv, html, json, re, hashlib

ROOT=Path(__file__).resolve().parent
def merge_reports(*files):
    merged={}
    for file in files:
        for c in json.loads((ROOT/file).read_text())['cases'].values():
            if not c.get('results'):continue
            name=Path(c['path']).name
            if name not in merged:merged[name]={**c,'results':{}}
            merged[name]['results'].update(c['results'])
    return {'cases':merged}
full=merge_reports('local.json','doubao.json')
sentences=merge_reports('sentences_local.json','sentences_doubao.json')
segments=json.loads((ROOT/'sentences_manifest.json').read_text())
levels=json.loads((ROOT/'level_comparison.json').read_text())
backends=['doubao','sensevoice','funasr_nano']
names={'doubao':'豆包 Seed-ASR','sensevoice':'SenseVoice Small','funasr_nano':'Fun-ASR Nano'}
assert len(full['cases'])==2
assert all(all(b in c['results'] for b in backends) for c in full['cases'].values()),'Full-file cloud test is still running'
rows=[]
normalized=[]
for c in sentences['cases'].values():
    # The main tool resolves /var to /private/var; discard empty seed aliases.
    if not c.get('results'):continue
    c['sentence']=int(Path(c['path']).name.split('_',1)[0])
    c['reading_prompt']=segments[c['sentence']-1]['reading_prompt']
    normalized.append(c)
    for backend in backends:
        assert backend in c['results'],f'Incomplete: {c["path"]} / {backend}'
        result=c['results'][backend]
        rows.append(dict(sentence=c['sentence'],version=c['group'],backend=backend,
            prompt=c['reading_prompt'],text=result['text'],elapsed_s=result['elapsed_s'],error=result['error'],
            clip='sentences/'+Path(c['path']).name))
lookup={(e['sentence'],e['version'],e['backend']):e for e in rows}
assert len(normalized)==18 and len(rows)==54
coverage={b:{v:{'nonempty':sum(bool(e['text']) for e in rows if e['backend']==b and e['version']==v),'errors':sum(e['error'] is not None for e in rows if e['backend']==b and e['version']==v),'total':9} for v in ['original','commercial']} for b in backends}
(ROOT/'recognition_summary.json').write_text(json.dumps({'coverage':coverage,'note':'Nonempty output is not accuracy; all wording is retained exactly.'},ensure_ascii=False,indent=2))
(ROOT/'sentences_results.json').write_text(json.dumps({'cases':normalized,'sources':['sentences_local.json','sentences_doubao.json'],'note':'Empty seed aliases removed; sentence labels restored from the filename/collector manifest; backend results merged by clip filename. Recognition text unchanged.'},ensure_ascii=False,indent=2))

with (ROOT/'逐句识别.csv').open('w',newline='',encoding='utf-8-sig') as f:
    writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

def norm(s):return re.sub(r'[^a-z0-9\u3400-\u9fff]','',s.lower())
def edit(a,b):
    prev=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        cur=[i]
        for j,y in enumerate(b,1):cur.append(min(prev[j]+1,cur[-1]+1,prev[j-1]+(x!=y)))
        prev=cur
    return prev[-1]
prompt_stats={}
for backend in backends:
    errors={};total=0
    for ver in ['original','commercial']:
        parts=[r for r in rows if r['backend']==backend and r['version']==ver and r['sentence']<=8]
        total=sum(len(norm(r['prompt'])) for r in parts)
        errors[ver]=sum(edit(norm(r['prompt']),norm(r['text'])) for r in parts)
    prompt_stats[backend]={'reference_characters':total,'original_edits':errors['original'],'commercial_edits':errors['commercial'],'original_prompt_cer':errors['original']/total,'commercial_prompt_cer':errors['commercial']/total}
(ROOT/'prompt_comparison_auxiliary.json').write_text(json.dumps({'caveat':'参考是屏幕提示词，未逐字人工核听；仅辅助比较，不能当成真实识别准确率。只统计前 8 句中文，第 9 句英文另看原文。','results':prompt_stats},ensure_ascii=False,indent=2))

lines=['# 商业降爆破版与原音：主项目 ASR 对比','',
'## 测试结论','',
'主项目的豆包 Seed-ASR、SenseVoice Small 和 Fun-ASR Nano 已完成整段及逐句识别。商业处理版没有表现出稳定的识别改善，多句出现更明显的漏词或错词；也有个别句改善、或两版相同。请以以下未改写的识别原文为准。','',
'整段识别仅作记录：SenseVoice 对长文件漏句较多，Fun-ASR 在原音整段上出现大量重复生成。因此主要对照 9 句话分别识别的结果，不能把整段的出字量当成正确率。','',
'## 输入与设置','',
'- 用户商业处理版：`/Users/admin/Desktop/ring_raw.wav`，16 kHz / 单声道 / PCM16 / 108 秒。',
'- 真正原音：上次比较保留的 `deplosive_compare_speaker001_20260915T071439_605580Z/native/original.wav`，98 秒，SHA256 以 `4f5fb681...` 开头。',
'- 采集目录当前同名 `data/raw/.../ring_raw.wav` 与桌面商业版哈希完全相同；本次没有把这个已变化的文件当成原音。',
'- 商业版在约 24.27–34.27 秒新增近 10 秒全零区间；核对 4 处朗读窗口，后半段内容均后移 10 秒。逐句切片据此对应，未拉伸或重采样。',
'- 主项目程序：`/Users/admin/Downloads/ProximicVoice-main/tools/compare_asr_audio.py`。使用其 `.runtime/venv/bin/python` 及实际后端。',
'- 本地模型使用 CPU、语言 `zh`、结束时整段解码；豆包使用主项目现有云端后端，`--doubao-pace 1` 按实时节奏、每 20 ms 音频块送入。',
'- 无热词、无 LLM 文本整理、无额外增益、无再次降噪。`--max-seconds 0`，未采用默认的前 15 秒截断。',
f'- 商业版朗读段总 RMS 比原音低 {abs(levels["speech_total_rms_change_db"]):.2f} dB，500–4000 Hz 语音频段低 {abs(levels["speech_500_4000_rms_change_db"]):.2f} dB。此处比较的是用户交付的实际音频，不是额外匹配音量后的算法消融实验。',
'- 全文与逐句共 60 次识别（2×3 + 9×2×3），原始文本完整保留。',
'- 用户明确允许发送两份录音至豆包／火山引擎云端后，才执行豆包测试。','',
'## 逐句识别原文','',
'“朗读提示”来自采集记录，不保证录音者逐字照读；以下识别输出没有人工纠正。','']
cards=[]
for s in segments:
    n=s['sentence'];lines += [f'### 第 {n} 句','',f'朗读提示：{s["reading_prompt"]}','',
        f'对应时间：原音 {s["original_start_s"]:.1f}–{s["original_end_s"]:.1f} 秒；商业版 {s["commercial_start_s"]:.1f}–{s["commercial_end_s"]:.1f} 秒。','',
        '| 模型 | 原音识别 | 商业处理版识别 |','|---|---|---|']
    trs=[]
    for b in backends:
        a=lookup[n,'original',b];c=lookup[n,'commercial',b]
        lines.append(f'| {names[b]} | {a["text"] or "（空）"} | {c["text"] or "（空）"} |')
        def display(text):
            if len(text)>250:return html.escape(text[:80])+'…<details><summary>输出过长 / 重复，展开原文</summary><p>'+html.escape(text)+'</p></details>'
            return html.escape(text or '（空）')
        trs.append(f'<tr><th>{names[b]}</th><td>{display(a["text"])}</td><td>{display(c["text"])}</td></tr>')
    lines.append('')
    cards.append(f'''<section><h2>第 {n} 句</h2><p class="prompt">{html.escape(s['reading_prompt'])}</p><div class="audio"><div><span>原音 · {s['original_start_s']:.1f}–{s['original_end_s']:.1f} s</span><audio controls preload="none" src="sentences/{n:02d}_original.wav"></audio></div><div><span>商业处理 · {s['commercial_start_s']:.1f}–{s['commercial_end_s']:.1f} s</span><audio controls preload="none" src="sentences/{n:02d}_commercial.wav"></audio></div></div><div class="scroll"><table><tr><th>模型</th><th>原音识别</th><th>商业处理版识别</th></tr>{''.join(trs)}</table></div></section>''')
lines += ['## 与提示词的辅助对照','',
'仅供排查：前 8 句中文去标点后，与屏幕朗读提示计算字符编辑率（CER）。提示词不是人工听写真值，因此不能将 1−CER 称为识别准确率。','',
'| 模型 | 原音 vs 提示词 | 商业版 vs 提示词 |','|---|---:|---:|']
for b,stat in prompt_stats.items():lines.append(f'| {names[b]} | {100*stat["original_prompt_cer"]:.1f}% | {100*stat["commercial_prompt_cer"]:.1f}% |')
lines += ['','Fun-ASR 商业版第 3 句反复生成“皮蓬”；大量插入错误会使 CER 超过 100%，该值不是概率。','',
          '## 逐句出字情况（不是正确率）','','| 模型 | 原音有文字 | 商业版有文字 |','|---|---:|---:|']
for b in backends:lines.append(f'| {names[b]} | {coverage[b]["original"]["nonempty"]}/9 | {coverage[b]["commercial"]["nonempty"]}/9 |')
lines += ['','## 完整文件的识别原文','', '长录音异常输出原样保留，不作为逐句效果结论。','']
full_details=[]
for c in full['cases'].values():
    label='原音（98 秒）' if Path(c['path']).name=='01_original.wav' else '商业处理版（108 秒）'
    for b in backends:
        res=c['results'][b]
        lines += [f'### {label} · {names[b]}','',res['text'] or '（空）','']
        full_details.append(f'<details><summary>{label} · {names[b]}</summary><p>{html.escape(res["text"] or "（空）")}</p></details>')
lines += ['## 复现记录','',
'- `local.json`：主项目整段输出；其中 `plus24db-near` 是脚本默认的数据组名称，没有实际添加 24 dB。',
'- `sentences_local.json`：主项目逐句输出及耗时，明确分 original / commercial。',
'- `doubao.json`、`sentences_doubao.json`：主项目豆包整段与逐句原始输出；`sentences_results.json` 合并三个后端的结果，不改识别文本。',
'- `input_manifest.json`、`test_configuration.json`：输入哈希、工具源码哈希、模型环境与设置。',
'- `audio_verification.json`：时长、10 秒偏移及匹配相关性证据。',
'- `inputs/` 和 `sentences/`：本次实际送入本地测试程序的音频副本。','']
(ROOT/'识别对比.md').write_text('\n'.join(lines))
page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>商业降爆破前后 · 识别对比</title><style>body{background:#f4f6f5;color:#24352f;font:15px/1.7 -apple-system,BlinkMacSystemFont,sans-serif;margin:0}main{max-width:1120px;margin:auto;padding:35px 22px}h1{font-size:29px;line-height:1.4}h2{font-size:20px;margin-top:0}section{background:white;border:1px solid #dce4df;border-radius:14px;padding:22px;margin:20px 0}.note{color:#68766e;font-size:13px}.prompt{background:#edf5f0;padding:12px;border-radius:8px}.audio{display:grid;grid-template-columns:1fr 1fr;gap:20px}.audio span{display:block;font-size:12px;color:#68766e}audio{width:100%;height:35px;margin:8px 0 16px}table{border-collapse:collapse;width:100%;table-layout:fixed}td,th{border-bottom:1px solid #e5ebe7;vertical-align:top;text-align:left;padding:12px 10px;overflow-wrap:anywhere}th:first-child{width:145px}td{width:40%}a{color:#147e65}details{margin:15px 0}summary{cursor:pointer;color:#147e65}details p{white-space:pre-wrap;overflow-wrap:anywhere}.scroll{overflow:auto}@media(max-width:700px){.audio{grid-template-columns:1fr}table{min-width:600px}}</style><main><p class="note">RING AUDIO LAB · ASR COMPARISON</p><h1>商业降爆破前后，主项目识别出了什么</h1><p>同一段录音，豆包 / SenseVoice Small / Fun-ASR Nano；原音 98 秒，商业版 108 秒。</p><section><h2>先看结论</h2><p>商业版没有表现出稳定的识别改善。下面按同一句话列出原始识别文本，方便核对漏词和错词。</p><p class="note">商业版中途增加了约 10 秒静音，已补偿逐句偏移。没有额外放大或做文本纠错。长文件一次识别出现漏句和重复生成，折叠保留在页面末尾。豆包已在用户明确授权后完成测试。</p><p><a href="volume_diagnosis/">音量与识别退化对照</a> · <a href="识别对比.md">完整报告</a> · <a href="逐句识别.csv">下载逐句识别表</a> · <a href="sentences_results.json">三模型逐句 JSON</a></p></section>__CARDS__<section><h2>完整文件一次识别（异常输出也原样保留）</h2>__FULL__</section><p class="note">使用主项目现有测试脚本及后端，无修改。朗读提示不等于人工听写真值。原音取自上次保留的副本，两个输入文件哈希均已记录。</p></main><script>document.querySelectorAll('audio').forEach(a=>a.addEventListener('play',()=>document.querySelectorAll('audio').forEach(b=>{if(a!==b)b.pause()})));</script></html>'''
(ROOT/'index.html').write_text(page.replace('__CARDS__',''.join(cards)).replace('__FULL__',''.join(full_details)))
print(json.dumps(prompt_stats,ensure_ascii=False,indent=2))
for n in [2,6,8,9]:
    print('Sentence',n)
    for b in backends:print(names[b],lookup[n,'original',b]['text'],'→',lookup[n,'commercial',b]['text'])
