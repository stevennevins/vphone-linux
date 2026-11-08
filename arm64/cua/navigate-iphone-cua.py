#!/usr/bin/env python3
"""Open a Settings subpage on the virtual iPhone with Jev choices and CUA taps.

The VM window must be on a private Xvfb display (:90 through :99), showing the
home screen. The script:

1. Starts a continuous ffmpeg x11grab recording of the display before any input.
2. Starts a cua-driver daemon and finds the VM window.
3. For each tap, takes a fresh window screenshot through CUA, finds candidate
   labels with OCR, lets Jev choose one (or abstain), and sends the tap with a
   CUA click bound to that screenshot's capture_id.
4. Taps Settings, verifies the Settings list, taps General, verifies the
   General page, taps the Back button, and verifies the Settings list again.
   Jev makes each verification from a fresh screenshot.
5. Stops the recording after the final check and writes the evidence:
   session.mp4, calls.json, decisions.json, timings.json, result.json, and
   screenshots/.

Run it with the cua-desktop-toolkit venv Python (it has typesafe_sdk, rapidocr,
numpy and PIL). TYPESAFE_API_KEY must be set.
"""
import argparse
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

DEFAULT_TOOLKIT = Path.home() / 'Documents/Codex/2026-11-07/unzip-both-use-the-cua-and/cua-desktop-toolkit'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--display', required=True)
    p.add_argument('--window-title', default='vphone-linux',
                   help='title prefix of the VM window (REIMS_VGPU_WINDOW_TITLE)')
    p.add_argument('--toolkit', type=Path, default=Path(os.environ.get('CUA_TOOLKIT', DEFAULT_TOOLKIT)))
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--fps', type=int, default=10, help='recording frame rate (real time)')
    p.add_argument('--subpage', default='General', help='Settings row to open')
    p.add_argument('--transition-timeout', type=float, default=120,
                   help='seconds to wait for a screen to change after a tap (TCG is slow)')
    # Foreground clicks go through XTEST, which the window's SDL input takes. Background clicks on
    # X11 are XSendEvent events, which SDL's XInput2 input ignores. Foreground needs a window manager.
    p.add_argument('--delivery-mode', choices=['background', 'foreground'], default='foreground')
    a = p.parse_args()
    if a.display not in [f':{i}' for i in range(90, 100)]:
        p.error('use a private display :90 through :99')
    if not os.environ.get('TYPESAFE_API_KEY'):
        p.error('TYPESAFE_API_KEY is not set')

    sys.path.insert(0, str(a.toolkit / 'cua/jev-use/python'))
    from jev_adapter import choose_bounded_with_typesafe
    from typesafe_sdk import TypeSafeClient, Image
    from rapidocr import RapidOCR
    from PIL import Image as PILImage
    import numpy as np

    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        p.error(f'{out} is not empty; move the earlier evidence away first')
    shots = out / 'screenshots'
    shots.mkdir(parents=True, exist_ok=True)
    driver = str(a.toolkit / 'cua/venv/bin/cua-driver')
    sock = f'/tmp/iphone-cua-{os.getpid()}.sock'
    env = dict(os.environ, DISPLAY=a.display)
    started = time.monotonic()
    started_wall = time.time()
    timings, calls, decisions, checks, taps = [], [], [], [], []

    def save(name, data):
        (out / name).write_text(json.dumps(data, indent=2))

    def measured(kind, label, since, **extra):
        timings.append(dict(kind=kind, label=label, seconds=round(time.monotonic() - since, 4),
                            at_seconds=round(time.monotonic() - started, 4), **extra))
        save('timings.json', timings)

    def call(name, args):
        t = time.monotonic()
        r = subprocess.run([driver, 'call', name, json.dumps(dict(args, session='iphone-settings')),
                            '--socket', sock], env=env, capture_output=True, text=True, timeout=60)
        measured('cua', name, t)
        rec = dict(tool=name, args=args, exit_code=r.returncode, at_seconds=round(time.monotonic() - started, 4),
                   response=r.stdout[:4000], stderr=r.stderr[:2000])
        calls.append(rec)
        save('calls.json', calls)
        if r.returncode:
            raise RuntimeError(f'cua {name} failed: {r.stderr}{r.stdout}')
        return json.loads(r.stdout)

    client = TypeSafeClient(headers={'TypeSafe-Danger-Zone-Images': 'true'}, timeout=60)

    def choose(goal, shot, criteria, **extra):
        t = time.monotonic()
        c = choose_bounded_with_typesafe(
            client, goal=goal,
            observation=dict(screenshot=Image.from_pil(shot['image'], format='png'), **extra),
            criteria=criteria)
        measured('jev', goal[:70], t)
        decisions.append(dict(dataclasses.asdict(c), goal=goal, criteria=criteria, screenshot=shot['file'],
                              at_seconds=round(time.monotonic() - started, 4)))
        save('decisions.json', decisions)
        return c.selected_id

    n_shot = [0]
    window = {}

    def shot(label):
        """A fresh CUA screenshot of the VM window, registered as a capture for a later click."""
        n_shot[0] += 1
        f = shots / f'{n_shot[0]:03d}-{label}.png'
        t = time.monotonic()
        r = call('get_window_state', dict(pid=window['pid'], window_id=window['window_id'],
                                          include_accessibility_tree=False, screenshot_out_file=str(f)))
        measured('capture', label, t)
        im = PILImage.open(f).convert('RGB')
        return dict(file=str(f.relative_to(out)), image=im, capture_id=r.get('capture_id'),
                    window_bounds=r.get('window_bounds'))

    engine = None

    def ocr(s):
        nonlocal engine
        if engine is None:
            t = time.monotonic()
            engine = RapidOCR(params={'EngineConfig.onnxruntime.intra_op_num_threads': 4,
                                      'EngineConfig.onnxruntime.inter_op_num_threads': 1})
            measured('ocr', 'engine initialization', t)
        t = time.monotonic()
        r = engine(str(out / s['file']))
        measured('ocr', 'text detection', t)
        found = []
        if r.txts is not None:
            for box, text, score in zip(r.boxes, r.txts, r.scores):
                c = np.asarray(box).mean(axis=0)
                found.append(dict(text=str(text), x=float(c[0]), y=float(c[1]), score=float(score)))
        return found

    def same_pixels(s1, s2):
        a1, a2 = np.asarray(s1['image']).astype(int), np.asarray(s2['image']).astype(int)
        return a1.shape == a2.shape and float(np.abs(a1 - a2).mean()) < 0.5

    def tap(step, goal, match, attempts=10, extra=None):
        """Jev picks one OCR label that `match` accepts from a fresh screenshot; CUA taps it.
        `extra(texts, image)` may add candidates that have no text label (an icon-only button)."""
        for attempt in range(attempts):
            s = shot(f'{step}-choose')
            texts = ocr(s)
            found = [f for f in texts if f['score'] >= 0.6 and match(f, s['image'])]
            if not found and extra is not None:
                found = extra(texts, s['image'])
            if not found:
                time.sleep(2)
                continue
            cands = {f'tap_{i}': f for i, f in enumerate(found)}
            criteria = {k: f"Tap the visible '{v['text']}' label at ({round(v['x'])},{round(v['y'])})"
                        for k, v in cands.items()}
            criteria['abstain'] = 'Do not tap: the target is not visible, or the screen is loading or unexpected'
            sel = choose(goal, s, criteria, targets={k: dict(text=v['text'], x=round(v['x']), y=round(v['y']))
                                                     for k, v in cands.items()})
            if sel == 'abstain':
                time.sleep(2)
                continue
            # Tap only if the screen still shows what Jev saw: a fresh capture, pixel-equal.
            fresh = shot(f'{step}-confirm')
            if not same_pixels(s, fresh):
                continue
            if not fresh['capture_id']:
                raise RuntimeError('get_window_state returned no capture_id')
            target = cands[sel]
            # x/y are pixels of the bound capture; the driver maps them to the window.
            call('click', dict(pid=window['pid'], window_id=window['window_id'], x=round(target['x']),
                               y=round(target['y']), capture_id=fresh['capture_id'],
                               delivery_mode=a.delivery_mode))
            taps.append(dict(step=step, selected=sel, target=target, chosen_from=s['file'],
                             confirmed_with=fresh['file'], at_seconds=round(time.monotonic() - started, 4)))
            save('taps.json', taps)
            return s
        raise RuntimeError(f'{step}: no tap target within {attempts} attempts')

    def verify(step, goal, yes, no, previous):
        """Wait for the screen to change, then let Jev judge fresh screenshots until `yes` or timeout."""
        t = time.monotonic()
        deadline = t + a.transition_timeout
        while time.monotonic() < deadline:
            s = shot(f'{step}-verify')
            if previous is not None and same_pixels(s, previous):
                time.sleep(1.5)
                continue
            sel = choose(goal, s, {'yes': yes, 'wait': no})
            if sel == 'yes':
                measured('transition', step, t)
                checks.append(dict(name=step, passed=True, screenshot=s['file']))
                save('checks.json', checks)
                return s
            previous = None  # changed once; judge every later frame
            time.sleep(2)
        checks.append(dict(name=step, passed=False))
        save('checks.json', checks)
        raise RuntimeError(f'{step}: not verified within {a.transition_timeout} s')

    rec = daemon = None
    result = {}
    try:
        # 1. Continuous recording first, before any input.
        geo = subprocess.run(['xdpyinfo', '-display', a.display], capture_output=True, text=True, check=True).stdout
        size = next(l.split()[1] for l in geo.splitlines() if 'dimensions:' in l)
        rec_log = open(out / 'recorder.log', 'w')
        rec = subprocess.Popen(['ffmpeg', '-hide_banner', '-nostdin', '-y', '-f', 'x11grab',
                                '-framerate', str(a.fps), '-video_size', size, '-draw_mouse', '1',
                                '-i', f'{a.display}.0', '-c:v', 'libx264', '-preset', 'veryfast',
                                '-pix_fmt', 'yuv420p', str(out / 'session.mp4')],
                               stdin=subprocess.DEVNULL, stdout=rec_log, stderr=rec_log)
        rec_started = time.monotonic()
        time.sleep(2)
        if rec.poll() is not None:
            raise RuntimeError('ffmpeg recording did not start (see recorder.log)')
        measured('recording', 'started', rec_started, wall=time.time())

        # 2. CUA daemon and the VM window.
        with open(out / 'driver.log', 'w') as log:
            daemon = subprocess.Popen([driver, 'serve', '--socket', sock, '--pid-file', str(out / 'driver.pid'),
                                       '--no-overlay'],
                                      env={k: v for k, v in env.items() if k != 'TYPESAFE_API_KEY'},
                                      stdout=log, stderr=log)
        for _ in range(100):
            if Path(sock).exists():
                break
            if daemon.poll() is not None:
                raise RuntimeError('cua-driver exited')
            time.sleep(0.1)
        wins = call('list_windows', {'on_screen_only': True})['windows']
        cands = [w for w in wins if str(w.get('title', '')).startswith(a.window_title)]
        if not cands:
            raise RuntimeError(f'no window titled {a.window_title!r}: {[w.get("title") for w in wins]}')
        w = max(cands, key=lambda w: w['width'] * w['height'])
        cmd = Path(f"/proc/{w['pid']}/cmdline").read_bytes().replace(b'\0', b' ').decode(errors='replace')
        window.update(pid=w['pid'], window_id=w['window_id'], title=w['title'], owner_cmdline=cmd[:300],
                      bounds=dict(x=w['x'], y=w['y'], width=w['width'], height=w['height']))
        save('window.json', window)
        if 'qemu-system-aarch64' not in cmd:
            raise RuntimeError(f'the window belongs to {cmd[:120]!r}, not QEMU')

        h_of = lambda im: im.size[1]

        def back_chevron(texts, im):
            """iOS 26 can show the back button as an icon-only chevron. Offer the top-left point on
            the row of the page title as a candidate; Jev still decides from the screenshot."""
            title = [f for f in texts if f['text'].strip().lower() == a.subpage.lower() and f['y'] < 0.15 * h_of(im)]
            if not title:
                return []
            return [dict(text='< (back chevron, no text)', x=0.09 * im.size[0], y=title[0]['y'], score=1.0)]
        # 3. Home screen -> Settings.
        s0 = tap('01-home-settings',
                 'This is a virtual iPhone home screen. Open the Settings app: choose the visible "Settings" '
                 'label under the Settings app icon. Abstain on a lock screen, a dialog, a loading screen, or '
                 'any screen that is not the home screen.',
                 lambda f, im: f['text'].strip().lower() == 'settings')
        s1 = verify('02-settings-list-opened',
                    'Has the iOS Settings app opened at its main list? Require the large "Settings" title and '
                    'category rows such as General, Accessibility, Wallpaper or Privacy. The home screen does not count.',
                    'The main Settings list is visible', 'The main Settings list is not visible yet', s0)
        # 4. Settings -> subpage.
        sub = a.subpage
        s2 = tap('03-settings-' + sub.lower(),
                 f'This is the iOS Settings main list. Open the "{sub}" subpage: choose the visible "{sub}" row '
                 'label. Abstain if the Settings list is not visible.',
                 lambda f, im: f['text'].strip().lower() == sub.lower())
        s3 = verify(f'04-{sub.lower()}-opened',
                    f'Has the iOS Settings "{sub}" subpage opened? Require the "{sub}" page title at the top, a '
                    'back button ("<" or "< Settings") at the top left, and rows of that subpage (for General: About, '
                    'Software Update, AirDrop, Date & Time). The main Settings list does not count.',
                    f'The {sub} subpage is visible', f'The {sub} subpage is not visible yet', s2)
        # 5. Back to the Settings list.
        s4 = tap('05-back-to-settings',
                 f'This is the iOS Settings "{sub}" subpage. Go back: choose the back button at the top left of '
                 'the screen (a "Settings" label or a "<" chevron). Abstain if this is not the subpage.',
                 lambda f, im: ('settings' in f['text'].strip().lower() or f['text'].strip() in ('<', '‹', 'く'))
                 and f['y'] < 0.15 * h_of(im) and f['x'] < 0.4 * im.size[0],
                 extra=back_chevron)
        verify('06-settings-list-restored',
               f'Has the screen returned to the iOS Settings main list? Require the large "Settings" title and '
               f'category rows such as General, Accessibility or Privacy. The "{sub}" subpage does not count.',
               'The main Settings list is visible again', 'The main Settings list is not visible yet', s4)
        final = shot('07-final')
        result = dict(passed=True, checks=checks, taps=len(taps), decisions=len(decisions),
                      provider='live TypeSafe/Jev', input_transport=f'CUA click ({a.delivery_mode}) bound to capture_id',
                      final_screenshot=final['file'])
    except Exception as ex:
        result = dict(passed=False, error=str(ex), checks=checks, taps=len(taps), decisions=len(decisions))
    finally:
        if rec is not None and rec.poll() is None:
            time.sleep(2)  # keep the final verified frame on video
            rec.send_signal(2)  # SIGINT: ffmpeg finishes the MP4
            try:
                rec.wait(timeout=30)
            except subprocess.TimeoutExpired:
                rec.kill()
            measured('recording', 'stopped', rec_started, wall=time.time())
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
        Path(sock).unlink(missing_ok=True)

    models = sorted({d.get('model') for d in decisions if d.get('model')})
    result.update(total_seconds=round(time.monotonic() - started, 4),
                  started_at=time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(started_wall)),
                  jev_seconds=round(sum(t['seconds'] for t in timings if t['kind'] == 'jev'), 4),
                  jev_models=models, display=a.display, window=window)
    mp4 = out / 'session.mp4'
    if mp4.exists() and shutil.which('ffprobe'):
        pr = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=r_frame_rate,nb_frames',
                             '-of', 'json', str(mp4)], capture_output=True, text=True)
        try:
            info = json.loads(pr.stdout)
            rec_wall = next(t['seconds'] for t in reversed(timings) if t['label'] == 'stopped')
            result['video'] = dict(file='session.mp4', duration_seconds=float(info['format']['duration']),
                                   recording_wall_seconds=rec_wall, frame_rate=info['streams'][0].get('r_frame_rate'),
                                   real_time=abs(float(info['format']['duration']) - rec_wall) < 0.1 * rec_wall + 3)
        except Exception as ex:  # keep the run's result even if probing fails
            result['video'] = dict(file='session.mp4', probe_error=str(ex))
    save('result.json', result)
    print(json.dumps(result, indent=2))
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
