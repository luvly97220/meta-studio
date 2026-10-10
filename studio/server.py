#!/usr/bin/env python3
"""Meta-Studio - serveur local pour Termux.

Lance avec :  python server.py
Puis ouvre :  http://localhost:8080

Ce serveur :
  - affiche l'application (index.html)
  - transmet les demandes a fal.ai (evite les blocages du navigateur)
  - enregistre les videos dans le dossier "videos"
  - assemble les scenes en une seule video (montage avec ffmpeg)
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
ROOT = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(ROOT, "videos")
UPLOADS = os.path.join(ROOT, "uploads")
WORK = os.path.join(ROOT, ".travail")
FAL_QUEUE = "https://queue.fal.run/"
FAL_STORAGE = "https://rest.alpha.fal.ai/storage/upload/initiate"
ALLOWED_HOSTS = ("fal.run", "fal.media", "fal.ai", "falserverless", "googleapis.com", "fal-cdn")

for d in (VIDEOS, UPLOADS, WORK):
    os.makedirs(d, exist_ok=True)


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def host_ok(url):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return url.startswith("https://") and any(a in h for a in ALLOWED_HOSTS)


def safe_name(s, default="fichier"):
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in (s or ""))[:60]
    return s or default


def local_path(rel):
    """Convertit '/videos/x.mp4' en chemin disque, sans sortir du dossier."""
    p = os.path.normpath(os.path.join(ROOT, rel.lstrip("/")))
    if not p.startswith(ROOT + os.sep):
        raise ValueError("chemin refuse")
    return p


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-600:])
    return r.stdout


def has_audio(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
               "stream=index", "-of", "csv=p=0", path])
    return bool(out.strip())


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


FONT_CANDIDATES = [
    "/system/fonts/Roboto-Black.ttf", "/system/fonts/Roboto-Bold.ttf",
    "/system/fonts/RobotoStatic-Bold.ttf", "/system/fonts/Roboto-Regular.ttf",
    "/system/fonts/NotoSans-Bold.ttf", "/system/fonts/DroidSans-Bold.ttf",
    os.path.expanduser("~/../usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def find_font():
    for f in FONT_CANDIDATES:
        if os.path.isfile(f):
            return f
    if os.path.isdir("/system/fonts"):
        for f in sorted(os.listdir("/system/fonts")):
            if f.lower().endswith(".ttf"):
                return os.path.join("/system/fonts", f)
    return None


def clean_sub(txt):
    """Retire les emojis (non affichables) et les espaces en trop."""
    out = []
    for ch in str(txt):
        o = ord(ch)
        if o > 0xFFFF or 0x2600 <= o <= 0x27BF or 0xFE00 <= o <= 0xFE0F or o == 0x200D:
            continue
        out.append(ch)
    return " ".join("".join(out).replace("\r", "").split(" ")).strip()


def wrap_sub(txt, width=24):
    lines = []
    for para in txt.split("\n"):
        cur = ""
        for word in para.split():
            if cur and len(cur) + 1 + len(word) > width:
                lines.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        if cur:
            lines.append(cur)
    return lines[:4]


def burn_subs(src, subs, stamp):
    """Incruste des sous-titres (gros, blancs, contour noir, centres) sur la video."""
    font = find_font()
    if not font or not subs:
        return src, bool(subs) and not font
    filters, tmp = [], []
    size, gap = 46, 60
    for i, sub in enumerate(subs):
        lines = wrap_sub(clean_sub(sub.get("text", "")))
        if not lines:
            continue
        a, b = float(sub.get("start", 0)), float(sub.get("end", 0))
        top = 0.74 - (len(lines) - 1) * gap / 2.0 / 1280
        for j, line in enumerate(lines):
            tf = os.path.join(WORK, "st_%s_%02d_%d.txt" % (stamp, i, j))
            with open(tf, "w", encoding="utf-8") as fh:
                fh.write(line)
            tmp.append(tf)
            filters.append(
                "drawtext=fontfile='%s':textfile='%s':expansion=none:fontcolor=white:fontsize=%d:"
                "borderw=5:bordercolor=black@0.9:fix_bounds=1:"
                "x=(w-text_w)/2:y=h*%.4f+%d:enable='between(t,%.2f,%.2f)'" % (font, tf, size, top, j * gap, a, b))
    if not filters:
        return src, False
    out = os.path.join(WORK, "subs_%s.mp4" % stamp)
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", ",".join(filters),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "copy", out])
    for t in tmp:
        try:
            os.remove(t)
        except OSError:
            pass
    return out, False


def video_size(src):
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=width,height", "-of", "csv=p=0", src]).strip().split(",")
        return int(out[0]), int(out[1])
    except Exception:
        return 0, 0


def canvas_for(src):
    """Format du montage d'apres le premier clip : 9:16, 4:5, 1:1 ou 16:9."""
    w, h = video_size(src)
    if not (w and h):
        return 720, 1280
    r = w / float(h)
    if r <= 0.7:
        return 720, 1280
    if r <= 0.9:
        return 864, 1080
    if r <= 1.2:
        return 1080, 1080
    return 1280, 720


def fit_filter(src, W=720, H=1280):
    """Remplit le format du montage : recadre si le clip est proche du format
    (pas de bandes noires), garde des bandes seulement si il est tres different."""
    w, h = video_size(src)
    tail = ",setsar=1,fps=30,format=yuv420p"
    target = W / float(H)
    if w and h and abs((w / float(h)) - target) / target <= 0.45:
        return ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (W, H, W, H)) + tail
    return ("scale=%d:%d:force_original_aspect_ratio=decrease,"
            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (W, H, W, H)) + tail


def loud_tiktok(path):
    """Remonte le son au niveau TikTok/Reels (~-14 LUFS), image inchangée."""
    tmp = path + ".loud.mp4"
    try:
        run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:v", "-map", "0:a?",
             "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac",
             "-b:a", "192k", "-ar", "44100", "-movflags", "+faststart", tmp])
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def concat(files, music=None, music_volume=0.35, title="histoire",
           mute=False, music_start=0.0, durations=None, subs=None):
    """Assemble des clips + musique optionnelle.

    mute=True       : on coupe le son des clips, seule la musique reste (clip musical)
    music_start     : debut de l'extrait de musique, en secondes
    durations       : duree exacte de chaque clip (coupe ou prolonge la derniere image)
    """
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg manquant : tape  pkg install ffmpeg  dans Termux")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    parts = []
    W, H = canvas_for(local_path(files[0])) if files else (720, 1280)
    for i, f in enumerate(files):
        src = local_path(f)
        if not os.path.isfile(src):
            raise RuntimeError("clip introuvable : " + f)
        out = os.path.join(WORK, "p%s_%02d.mp4" % (stamp, i))
        D = None
        if durations and i < len(durations) and durations[i]:
            D = float(durations[i])
        vf = fit_filter(src, W, H)
        if D:
            vf += ",tpad=stop_mode=clone:stop_duration=%.2f" % D
        tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k"]
        if D:
            tail += ["-t", "%.3f" % D]
        if has_audio(src) and not mute:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf] + \
                  (["-af", "apad"] if D else []) + tail + [out]
        else:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
                   "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                   "-vf", vf, "-map", "0:v", "-map", "1:a"] + \
                  ([] if D else ["-shortest"]) + tail + [out]
        run(cmd)
        parts.append(out)

    listfile = os.path.join(WORK, "liste_%s.txt" % stamp)
    with open(listfile, "w") as fh:
        for p in parts:
            fh.write("file '%s'\n" % p.replace("'", "'\\''"))
    joined = os.path.join(WORK, "joint_%s.mp4" % stamp)
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", joined])
    no_font = False
    if subs:
        subbed, no_font = burn_subs(joined, subs, stamp)
        if subbed != joined:
            os.remove(joined)
            joined = subbed

    final_name = "%s_%s.mp4" % (safe_name(title, "histoire"), stamp)
    final = os.path.join(VIDEOS, final_name)
    if music:
        msrc = local_path(music)
        total = duration(joined)
        fade_start = max(0.0, total - 2.0)
        if mute:
            fc = ("[1:a]volume=%.2f,afade=t=in:st=0:d=0.3,afade=t=out:st=%.2f:d=2[a]"
                  % (music_volume, fade_start))
        else:
            fc = ("[1:a]volume=%.2f,afade=t=out:st=%.2f:d=2[m];"
                  "[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]") % (music_volume, fade_start)
        run(["ffmpeg", "-y", "-v", "error", "-i", joined,
             "-stream_loop", "-1", "-ss", "%.2f" % max(0.0, float(music_start or 0)), "-i", msrc,
             "-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-t", "%.2f" % total, final])
    else:
        shutil.copy(joined, final)

    for p in parts + [listfile, joined]:
        try:
            os.remove(p)
        except OSError:
            pass
    loud_tiktok(final)
    return "/videos/" + final_name, duration(final), no_font


def mix_voices(video, voices, orig_volume=0.7, subs=True, title="voixoff"):
    """Pose des voix off (fichiers fal) sur une video, avec sous-titres optionnels."""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg manquant : tape  pkg install ffmpeg  dans Termux")
    src = local_path(video)
    if not os.path.isfile(src):
        raise RuntimeError("video introuvable")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    total = duration(src)
    files, t = [], 0.3
    for i, v in enumerate(voices):
        url = v.get("url", "")
        if not host_ok(url):
            raise RuntimeError("adresse de voix refusee")
        dest = os.path.join(WORK, "vo_%s_%02d.mp3" % (stamp, i))
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
            shutil.copyfileobj(r, fh)
        d = duration(dest)
        st = v.get("start")
        st = float(st) if st not in (None, "") else t
        files.append((dest, st, d, v.get("text", "")))
        t = st + d + 0.25
    work = src
    if subs:
        work, _ = burn_subs(src, [{"start": st, "end": st + d, "text": tx} for (_, st, d, tx) in files], stamp)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", work]
    if not has_audio(work):
        cmd += ["-f", "lavfi", "-t", "%.2f" % total, "-i", "anullsrc=r=44100:cl=stereo"]
        base = 1
    else:
        base = 0
    first = len(cmd)
    for (f, _, _, _) in files:
        cmd += ["-i", f]
    n0 = 2 if base == 1 else 1
    fc = ["[%d:a]volume=%.2f,aresample=44100[a0]" % (base, float(orig_volume))]
    labels = ["[a0]"]
    for i, (_, st, _, _) in enumerate(files):
        ms = int(st * 1000)
        fc.append("[%d:a]aresample=44100,volume=1.8,adelay=%d|%d[v%d]" % (n0 + i, ms, ms, i))
        labels.append("[v%d]" % i)
    fc.append("%samix=inputs=%d:duration=first:dropout_transition=0:normalize=0[a]" % ("".join(labels), len(labels)))
    name = "%s_%s.mp4" % (safe_name(title, "voixoff"), stamp)
    out = os.path.join(VIDEOS, name)
    cmd += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-t", "%.2f" % total, out]
    run(cmd)
    for (f, _, _, _) in files:
        try:
            os.remove(f)
        except OSError:
            pass
    if work != src:
        try:
            os.remove(work)
        except OSError:
            pass
    loud_tiktok(out)
    return "/videos/" + name, duration(out)


def audio_cut_datauri(rel, start, dur):
    """Decoupe un morceau du son (pour faire chanter l'artiste en play-back)."""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg manquant : tape  pkg install ffmpeg  dans Termux")
    src = local_path(rel)
    if not os.path.isfile(src):
        raise RuntimeError("son introuvable, remets ton morceau")
    out = os.path.join(WORK, "cut_%d.mp3" % int(time.time() * 1000))
    run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % float(start), "-t", "%.2f" % float(dur),
         "-i", src, "-ac", "1", "-ar", "44100", "-b:a", "128k", out])
    with open(out, "rb") as fh:
        data = fh.read()
    os.remove(out)
    if len(data) < 2000:
        raise RuntimeError("extrait vide : le debut depasse la fin du morceau ?")
    return "data:audio/mpeg;base64," + base64.b64encode(data).decode()


def video_ref_bytes(rel, start=0.0, maxdur=30.0):
    """Prepare une video de danse (reference de mouvement) : coupe et compresse en 720p."""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg manquant : tape  pkg install ffmpeg  dans Termux")
    src = local_path(rel)
    if not os.path.isfile(src):
        raise RuntimeError("video de danse introuvable, remets-la")
    out = os.path.join(WORK, "ref_%d.mp4" % int(time.time() * 1000))
    vf = ("scale='if(lt(iw,ih),min(720,iw),-2)':'if(lt(iw,ih),-2,min(720,ih))',"
          "fps=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % float(start or 0), "-t", "%.2f" % float(maxdur),
           "-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]
    cmd += (["-c:a", "aac", "-b:a", "96k"] if has_audio(src) else ["-an"])
    run(cmd + ["-movflags", "+faststart", out])
    dur = duration(out)
    with open(out, "rb") as fh:
        data = fh.read()
    os.remove(out)
    if dur < 3:
        raise RuntimeError("la video de danse doit durer au moins 3 secondes")
    return data, dur


def fal_upload(data, content_type, file_name, auth):
    """Envoie un fichier sur le stockage fal.ai et renvoie son adresse publique."""
    if not auth:
        raise RuntimeError("cle fal.ai manquante (reglages)")
    info, err = None, ""
    for url in (FAL_STORAGE + "?storage_type=fal-cdn-v3", FAL_STORAGE):
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps({"content_type": content_type, "file_name": file_name}).encode(),
            headers={"Authorization": auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                info = json.loads(r.read())
            if info.get("upload_url") and info.get("file_url"):
                break
        except urllib.error.HTTPError as e:
            err = "%s : %s" % (e.code, e.read()[:150].decode("utf8", "ignore"))
            if e.code in (401, 403):
                raise RuntimeError("Clé fal.ai refusée — vérifie-la dans ⚙️")
        except Exception as e:
            err = str(e)
        info = None
    if not info:
        raise RuntimeError("envoi de la vidéo sur fal.ai impossible (%s)" % err)
    put = urllib.request.Request(info["upload_url"], data=data, method="PUT",
                                 headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(put, timeout=300) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("envoi sur fal.ai impossible (%s)" % e.code)
    return info["file_url"]


def to_mp3_datauri(raw, ext):
    """Convertit un enregistrement micro (webm/ogg/m4a...) en MP3 base64 pour fal."""
    stamp = str(int(time.time() * 1000))
    src = os.path.join(UPLOADS, "micro_%s.%s" % (stamp, safe_name(ext, "webm")))
    with open(src, "wb") as fh:
        fh.write(raw)
    if not has_ffmpeg():
        return "data:audio/%s;base64,%s" % (ext, base64.b64encode(raw).decode()), "/uploads/" + os.path.basename(src)
    dst = src.rsplit(".", 1)[0] + ".mp3"
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-ac", "1", "-ar", "44100", "-b:a", "128k", dst])
    with open(dst, "rb") as fh:
        data = fh.read()
    return "data:audio/mpeg;base64," + base64.b64encode(data).decode(), "/uploads/" + os.path.basename(dst)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/fal" in (str(args[0]) if args else ""):
            return
        sys.stderr.write("  %s\n" % (fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # ---------- utilitaires ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def forward(self, method, url, data=None):
        if not host_ok(url):
            return self.send_json({"detail": "adresse refusee"}, 400)
        headers = {"Content-Type": "application/json"}
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                payload, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            payload, code = e.read(), e.code
        except Exception as e:
            return self.send_json({"detail": "reseau : %s" % e}, 502)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---------- routes ----------
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/health":
            return self.send_json({"ok": True, "ffmpeg": has_ffmpeg(), "dossier": ROOT})
        if p.path == "/api/get":
            url = urllib.parse.parse_qs(p.query).get("url", [""])[0]
            return self.forward("GET", url)
        if p.path == "/api/videos":
            items = []
            for f in sorted(os.listdir(VIDEOS), reverse=True):
                if f.endswith(".mp4"):
                    st = os.stat(os.path.join(VIDEOS, f))
                    items.append({"path": "/videos/" + f, "name": f, "size": st.st_size, "time": int(st.st_mtime)})
            return self.send_json({"videos": items})
        return super().do_GET()

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        try:
            if p.path.startswith("/api/fal/"):
                model = p.path[len("/api/fal/"):]
                return self.forward("POST", FAL_QUEUE + model, self.body())

            if p.path == "/api/save":
                d = json.loads(self.body() or b"{}")
                url = d.get("url", "")
                if not host_ok(url):
                    return self.send_json({"detail": "adresse refusee"}, 400)
                ext = ".mp4"
                low = url.split("?")[0].lower()
                for e in (".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav"):
                    if low.endswith(e):
                        ext = e
                name = "%s_%s%s" % (safe_name(d.get("name"), "video"), time.strftime("%Y%m%d_%H%M%S"), ext)
                dest = os.path.join(VIDEOS if ext == ".mp4" else UPLOADS, name)
                with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as fh:
                    shutil.copyfileobj(r, fh)
                rel = ("/videos/" if ext == ".mp4" else "/uploads/") + name
                return self.send_json({"path": rel})

            if p.path == "/api/concat":
                d = json.loads(self.body() or b"{}")
                path, dur, no_font = concat(d.get("files", []), d.get("music"),
                                            float(d.get("music_volume", 0.35)), d.get("title", "histoire"),
                                            bool(d.get("mute")), float(d.get("music_start") or 0),
                                            d.get("durations"), d.get("subs"))
                return self.send_json({"path": path, "duration": dur, "subs_skipped": no_font})

            if p.path == "/api/upload":
                q = urllib.parse.parse_qs(p.query)
                ext = safe_name(q.get("ext", ["bin"])[0], "bin")
                raw = self.body()
                if q.get("mp3", ["0"])[0] == "1":
                    uri, rel = to_mp3_datauri(raw, ext)
                    return self.send_json({"datauri": uri, "path": rel})
                name = "%s_%s.%s" % (safe_name(q.get("name", ["fichier"])[0]), int(time.time()), ext)
                dest = os.path.join(UPLOADS, name)
                with open(dest, "wb") as fh:
                    fh.write(raw)
                dur = 0.0
                if has_ffmpeg():
                    try:
                        dur = duration(dest)
                    except Exception:
                        dur = 0.0
                return self.send_json({"path": "/uploads/" + name, "duration": dur})

            if p.path == "/api/mixvoices":
                d = json.loads(self.body() or b"{}")
                path, dur = mix_voices(d.get("video", ""), d.get("voices", []),
                                       float(d.get("orig_volume", 0.7)), bool(d.get("subs", True)),
                                       d.get("title", "voixoff"))
                return self.send_json({"path": path, "duration": dur})

            if p.path == "/api/audiocut":
                d = json.loads(self.body() or b"{}")
                uri = audio_cut_datauri(d.get("path", ""), d.get("start", 0), d.get("dur", 5))
                return self.send_json({"datauri": uri})

            if p.path == "/api/refvideo":
                d = json.loads(self.body() or b"{}")
                data, dur = video_ref_bytes(d.get("path", ""), d.get("start", 0), d.get("max", 30))
                url = fal_upload(data, "video/mp4", "danse_%d.mp4" % int(time.time()),
                                 self.headers.get("Authorization"))
                return self.send_json({"url": url, "duration": dur})

            if p.path == "/api/delete":
                d = json.loads(self.body() or b"{}")
                f = local_path(d.get("path", ""))
                if os.path.dirname(f) == VIDEOS and os.path.isfile(f):
                    os.remove(f)
                return self.send_json({"ok": True})
        except Exception as e:
            return self.send_json({"detail": str(e)[:500]}, 500)
        self.send_json({"detail": "route inconnue"}, 404)


if __name__ == "__main__":
    print("")
    print("  Meta-Studio est lance !")
    print("  Ouvre dans ton navigateur :  http://localhost:%d" % PORT)
    print("  Montage video (ffmpeg) : %s" % ("OK" if has_ffmpeg() else "MANQUANT -> pkg install ffmpeg"))
    print("  Pour arreter : CTRL + C")
    print("")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
