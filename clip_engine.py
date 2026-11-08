#!/usr/bin/env python3
"""Victor Clipper AI: speech-to-shorts and ASS subtitle rendering.
Run on the HOSTING SERVER, never on the viewer's device.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
import zipfile
from pathlib import Path


def status(stage, progress, message):
    print('STATUS:' + json.dumps({'stage': stage, 'progress': int(progress), 'message': message}, ensure_ascii=False), flush=True)


def cmd(args, timeout=1200):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:] or 'Media command failed')
    return p.stdout


def media_length(source):
    raw = cmd(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nokey=1:noprint_wrappers=1', str(source)], timeout=60).strip()
    dur = float(raw)
    if not math.isfinite(dur) or dur < 1 or dur > 3601:
        raise ValueError('Source must be from 1 second to 60 minutes')
    return dur


def make_wav(source, target, max_time):
    cmd(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-vn', '-t', str(max_time), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)], timeout=300)


def energy_track(wav_path):
    with wave.open(str(wav_path), 'rb') as f:
        assert f.getnchannels() == 1 and f.getsampwidth() == 2
        size = f.getframerate() // 2
        values = []
        import audioop  # available in Python 3.12 and below
        while block := f.readframes(size):
            values.append(audioop.rms(block, 2) / 32768.)
    return values


def energy_track_portable(wav_path):
    # Use stdlib only, including Python 3.13+ where audioop was removed.
    import array
    import sys as _sys
    values = []
    with wave.open(str(wav_path), 'rb') as f:
        assert f.getnchannels() == 1 and f.getsampwidth() == 2
        size = max(1, f.getframerate() // 2)
        while block := f.readframes(size):
            samples = array.array('h'); samples.frombytes(block)
            if _sys.byteorder == 'big': samples.byteswap()
            if not samples:
                continue
            mean_square = sum(int(v)*int(v) for v in samples[::4]) / max(1, len(samples[::4]))
            values.append(min(1., math.sqrt(mean_square)/32768.))
    return values


def video_motion(source, duration):
    """Coarse visual movement score per second; no heavyweight CV dependencies.
    This improves non-speech selection (gaming/action) if ffmpeg decoding succeeds.
    """
    import subprocess
    try:
        seconds = min(int(duration), 1800)  # protect low-cost hosting from very long sources
        width, height = 64, 36
        proc = subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-i',str(source),
                               '-t',str(seconds),'-vf',f'fps=1,scale={width}:{height},format=gray',
                               '-an','-f','rawvideo','-pix_fmt','gray','pipe:1'],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=150)
        if proc.returncode: return []
        size=width*height; data=proc.stdout; motion=[]; previous=None
        for off in range(0,len(data)-size+1,size):
            frame=data[off:off+size]
            if previous is None:motion.append(0.)
            else:
                diff=sum(abs(frame[i]-previous[i]) for i in range(0,size,4))
                motion.append(diff/(255*(size//4)))
            previous=frame
        return motion
    except Exception:
        return []


def transcribe(wav_path):
    from faster_whisper import WhisperModel
    model = os.getenv('WHISPER_MODEL', 'base')
    allowed = {'tiny', 'base', 'small', 'medium', 'large-v3', 'turbo'}
    if model not in allowed: model = 'base'
    engine = WhisperModel(model, device='cpu', compute_type='int8', cpu_threads=min(4, os.cpu_count() or 2))
    segments, _ = engine.transcribe(str(wav_path), beam_size=4, word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
    words = []
    for segment in segments:
        for w in (segment.words or []):
            if w.word.strip() and w.start is not None and w.end is not None:
                words.append({'start': round(w.start, 3), 'end': round(w.end, 3), 'text': w.word.strip(), 'confidence': round(float(w.probability or 0), 3)})
    return words


HOOK_RE = re.compile(r'(?:\b(?:why|how|what|never|secret|wait|watch|imagine|actually|crazy|unbelievable|mistake|dangerous|surprising|shocking|best|worst|first|finally|story|truth|nobody|everyone|money|million|important|but|however)\b|(?:защо|какво|как|никога|тайна|чакай|виж|представи|всъщност|луд|невероят|грешк|опасн|изненада|шок|най.?добр|най.?лош|първи|най.?после|истори|истина|никой|всички|пари|милион|важн|обаче|но))', re.IGNORECASE)
BORING_RE = re.compile(r'(?:\b(?:subscribe|notification|sponsor|welcome|thanks for watching|hit the like|like and subscribe)\b|(?:абонира|харесайте|благодаря за гледането|добре дошли|спонсор))', re.IGNORECASE)


def signal_in_window(energy, start, end):
    vals = energy[max(0,int(start*2)):min(len(energy),max(int(start*2)+1,int(end*2)))]
    if not vals: return (0, 0, 0)
    avg = sum(vals)/len(vals)
    spread = (sum((v-avg)**2 for v in vals)/len(vals))**.5
    return avg, max(vals), spread


def summarize(words, start, end):
    pieces = [w['text'] for w in words if start <= w['start'] < end]
    return ' '.join(pieces).strip()


def pick_clips(words, energy, duration, count=3, length=30, llm=None, motion=None):
    """Rank candidate clips based on speech hooks, clarity, emphasis and sound dynamics.
    This is a heuristic + Whisper system; LLM output, if available, overrides candidate ranking.
    """
    duration = max(1.0, float(duration)); length = min(int(length), duration)
    count = min(max(int(count), 1), 5)
    starts = {0.0, max(0., duration-length)}
    for i in range(0, max(1, int(duration-length)+1), 4): starts.add(float(i))
    for w in words:
        if w['start'] < duration-length:
            starts.add(max(0., min(duration-length, w['start']-1.0)))
    max_energy = max(.015, max(energy, default=0.))
    candidates = []
    motion = motion or []
    max_motion = max(.04, max(motion, default=0))
    for start in sorted(starts):
        end = min(duration, start+length)
        ws = [w for w in words if w['end'] > start and w['start'] < end]
        text = ' '.join(w['text'] for w in ws)
        first = ' '.join(w['text'] for w in ws if w['start'] < start+5)
        avg, peak, var = signal_in_window(energy, start, end)
        loud = avg/max_energy; dynamic = var/max_energy
        density = len(ws)/max(1, end-start)
        confidence = sum(w.get('confidence', .8) for w in ws)/len(ws) if ws else .5
        hook = min(8, len(HOOK_RE.findall(text)))
        hook_first = min(4, len(HOOK_RE.findall(first)))
        questions = min(5, text.count('?')+text.count('!'))
        punctuation = min(5, text.count('.')+text.count('?')+text.count('!'))
        silence = 0.0
        for j in range(1, len(ws)):
            silence = max(silence, max(0,ws[j]['start']-ws[j-1]['end']))
        frames = motion[int(start):min(len(motion),int(end))]
        movement = (sum(frames)/len(frames))/max_motion if frames else 0.
        score = 1.25*loud + .9*dynamic + min(density,3.0)*.8 + hook*.32 + hook_first*.65 + questions*.42 + punctuation*.07 + confidence*.5 + .55*movement
        if words and density < .55: score -= 2
        if silence > 4: score -= min(3, (silence-4)*.8)
        if BORING_RE.search(text): score -= 1.1
        if ws and ws[0]['start'] >= start + 3.5: score -= 1.5
        if not words: score = 2.3*loud+1.6*dynamic+peak/max_energy+.9*movement
        # Avoid proposing pure silence at the last part of a clip.
        if ws and ws[-1]['end'] < end-4: score -= .9
        title = ' '.join((w['text'] for w in ws[:8]))[:82].strip() or 'Динамичен момент'
        candidates.append({'start':round(start,2), 'end':round(end,2),'score':round(score,3),'title':title,'preview':text[:180]})
    if llm:
        for c in candidates:
            for r in llm:
                if abs(c['start']-r.get('start',-100)) < 8:
                    c['score'] += float(r.get('score',0))*.18
                    if r.get('title'): c['title'] = str(r['title'])[:82]
                    break
    candidates.sort(key=lambda x:x['score'], reverse=True)
    chosen=[]
    for c in candidates:
        if len(chosen) >= count: break
        if any(max(0.,min(c['end'],p['end'])-max(c['start'],p['start'])) > length*.15 for p in chosen): continue
        chosen.append(c)
    return chosen or candidates[:1]


def gemini_rank(words, duration, clip_length, count):
    key = os.environ.get('GEMINI_API_KEY')
    if not key or not words: return None
    import urllib.request
    # Send sparse timed chunks instead of thousands of single-word objects.
    lines = []
    for i in range(0, len(words), 30):
        ws=words[i:i+30]
        lines.append(f"{ws[0]['start']:.1f}s: {' '.join(w['text'] for w in ws)}")
    source='\n'.join(lines)
    if len(source)>52000: source=source[:52000]
    prompt=(f'You are a professional short-form video editor. Analyze this timed transcript (source duration {duration:.0f}s). '
            f'Select {count} highly engaging DISTINCT moments for {clip_length}s vertical clips. Prefer a strong immediate hook, surprising reveal, emotional climax, self-contained insight, coherent ending, not intros or subscribe pleas. '
            'Return only a JSON array of objects with numeric "start" in seconds, numeric "score" from 0 to 10, and short "title" in the same language as the content. '
            'Do not fabricate moments outside the transcript.\n'+source)
    data=json.dumps({'contents':[{'parts':[{'text':prompt}]}], 'generationConfig':{'responseMimeType':'application/json','temperature':0.25}},ensure_ascii=False).encode()
    req=urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent',data=data,headers={'Content-Type':'application/json','x-goog-api-key':key},method='POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as f:
            res=json.load(f)
        text=res['candidates'][0]['content']['parts'][0]['text']
        items=json.loads(text)
        return [x for x in items if isinstance(x,dict) and isinstance(x.get('start'),(int,float)) and 0<=x['start']<=duration and isinstance(x.get('score'),(int,float))][:10]
    except Exception as ex:
        print('INFO:Gemini highlight ranking unavailable; using local ranking: '+str(ex)[:150], flush=True)
        return None


def ass_time(seconds):
    cs=max(0,int(round(seconds*100)))
    return f'{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}'


def ass_escape(s):
    return str(s).replace('\\','').replace('{','(').replace('}',')').replace('\n',' ')


def subtitle_ass(path, words, begin, end):
    chosen=[w for w in words if w['end']>begin and w['start']<end]
    hdr='''[Script Info]
Title: Victor Clipper AI Captions
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,43,&H00FFFFFF,&H00FFFFFF,&H000A0918,&H77000000,-1,0,0,0,100,100,0,0,1,4,1,2,45,45,255,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    entries=[]
    for off in range(0,len(chosen),4):
        group=chosen[off:off+4]
        if not group: continue
        for i,w in enumerate(group):
            s=max(begin, w['start']); e=min(end,w['end'])
            if e-s < .035:continue
            parts=[]
            for j,t in enumerate(group):
                color='&H005BE9FF&' if i==j else '&H00FFFFFF&' # warm yellow in BGR
                parts.append('{\\c'+color+'}'+ass_escape(t['text']).upper())
            entries.append('Dialogue: 0,'+ass_time(s-begin)+','+ass_time(e-begin)+',Default,,0,0,0,,'+' '.join(parts))
    path.write_text(hdr+'\n'.join(entries)+'\n',encoding='utf8')


def render_clip(source, out, begin, end, ass_path, burned=True):
    duration=end-begin
    # Video fills 9:16 with a center crop. Source audio remains original.
    filt='scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1'
    if burned:
        # ASS must be located in a non-user-controlled path without ffmpeg filter delimiter characters.
        filt+=',ass='+str(ass_path)+',format=yuv420p'
    else:
        filt+=',format=yuv420p'
    args=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-ss',f'{begin:.3f}','-i',str(source),'-t',f'{duration:.3f}', '-vf',filt,
          '-c:v','libx264','-preset','veryfast','-crf','24','-r','30','-c:a','aac','-b:a','128k','-ar','44100','-movflags','+faststart','-shortest',str(out)]
    cmd(args,timeout=360)
    if not out.exists() or out.stat().st_size<2000: raise RuntimeError('Unable to generate MP4')


def srt_time(s):
    ms=max(0,round(s*1000));return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'


def make_srt(path, words, start, end):
    selected=[w for w in words if w['end']>start and w['start']<end]
    lines=[]
    for i in range(0,len(selected),4):
        g=selected[i:i+4]
        lines.append(str(i//4+1)+'\n'+srt_time(max(0,g[0]['start']-start))+' --> '+srt_time(min(end-start,g[-1]['end']-start))+'\n'+' '.join(w['text'] for w in g)+'\n')
    path.write_text('\n'.join(lines),encoding='utf8')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--input',required=True)
    p.add_argument('--out',required=True)
    p.add_argument('--count',type=int,default=3)
    p.add_argument('--length',type=int,default=30)
    p.add_argument('--test-words',help='Only for local testing, not accepted by public API')
    opts=p.parse_args()
    root=Path(opts.out).resolve();root.mkdir(parents=True,exist_ok=True)
    source=Path(opts.input).resolve()
    status('audio',23,'Подготвям аудиото за AI разпознаване…')
    duration=media_length(source)
    wav=root/'audio.wav';make_wav(source,wav,duration)
    energy=energy_track_portable(wav)
    if opts.test_words:
        words=json.loads(Path(opts.test_words).read_text(encoding='utf8'))
        status('transcribe',48,'Тестови времеви субтитри…')
    else:
        status('transcribe',31,'Whisper AI разпознава думите и времето им…')
        words=transcribe(wav)
        status('transcribe',62,'Транскрипцията е готова.')
    status('visual',64,'Проверявам динамиката на кадрите…')
    motion=video_motion(source,duration) if not opts.test_words else []
    status('rank',66,'Избирам най-интересните моменти…')
    llm=gemini_rank(words,duration,opts.length,opts.count) if not opts.test_words else None
    clips=pick_clips(words,energy,duration,opts.count,opts.length,llm,motion)
    data=[]
    for idx,c in enumerate(clips,1):
        status('export',68+int(27*(idx-1)/max(1,len(clips))),f'Монтирам Short {idx} от {len(clips)}…')
        name=f'Victor_Clipper_Short_{idx}'
        ass=root/(name+'.ass');srt=root/(name+'.srt');mp4=root/(name+'.mp4')
        subtitle_ass(ass,words,c['start'],c['end'])
        make_srt(srt,words,c['start'],c['end'])
        render_clip(source,mp4,c['start'],c['end'],ass,bool(words))
        data.append({'id':idx,'start':c['start'],'end':c['end'],'score':c['score'],'title':c['title'],'preview':c['preview'],'video':mp4.name,'subtitles':srt.name})
    archive=root/'Victor_Clipper_Shorts.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
        for c in data:
            z.write(root/c['video'],arcname=c['video'])
            z.write(root/c['subtitles'],arcname=c['subtitles'])
    (root/'result.json').write_text(json.dumps({'clips':data,'duration':duration,'hasSpeech':bool(words),'aiRanking':'gemini+local' if llm else 'whisper+local'},ensure_ascii=False,indent=2),encoding='utf8')
    wav.unlink(missing_ok=True)
    status('ready',100,'Готово! Shorts са монтирани със субтитри.')


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('ERROR:'+str(e),file=sys.stderr,flush=True)
        raise
