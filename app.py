import os, hashlib, json, subprocess, tempfile, base64, re, urllib.request, urllib.parse
from flask import Flask, render_template, request, jsonify, send_file, abort, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
CACHE_PATH = os.path.join(BASE_DIR, '.meta_cache.json')
DEEZER_CACHE_PATH = os.path.join(BASE_DIR, '.deezer_cache.json')
EXTS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.wma', '.aac'}

TRACKS = {}
META_CACHE = {}    # path -> {'mtime':, 'size':, 'meta': {...}}  — evita reler tags de arquivos inalterados
DEEZER_CACHE = {}  # track_id -> resultado do Deezer (ou {'no_match': True}) — evita repetir a mesma busca


def load_deezer_cache():
    global DEEZER_CACHE
    if os.path.exists(DEEZER_CACHE_PATH):
        try:
            DEEZER_CACHE = json.load(open(DEEZER_CACHE_PATH, encoding='utf-8'))
        except Exception:
            DEEZER_CACHE = {}


def save_deezer_cache():
    try:
        json.dump(DEEZER_CACHE, open(DEEZER_CACHE_PATH, 'w', encoding='utf-8'))
    except Exception:
        pass


load_deezer_cache()


def load_meta_cache():
    global META_CACHE
    if os.path.exists(CACHE_PATH):
        try:
            META_CACHE = json.load(open(CACHE_PATH, encoding='utf-8'))
        except Exception:
            META_CACHE = {}


def save_meta_cache():
    try:
        json.dump(META_CACHE, open(CACHE_PATH, 'w', encoding='utf-8'))
    except Exception:
        pass


load_meta_cache()


def norm(s):
    """Normaliza texto para comparação (ignora maiúsculas/minúsculas e espaços nas pontas)."""
    return (s or '').strip().casefold()


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            return json.load(open(CONFIG_PATH, encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH, 'w', encoding='utf-8'))


def track_id(path):
    return hashlib.md5(path.encode('utf-8')).hexdigest()[:16]


def easy_get(tags, key):
    if not tags:
        return ''
    v = tags.get(key)
    return str(v[0]) if v else ''


def read_lyrics_and_cover(path, ext):
    """Lê a letra e verifica se há capa embutida abrindo o arquivo UMA única vez
    (antes eram duas aberturas separadas, uma para cada coisa)."""
    lyrics, cover = '', False
    try:
        if ext == '.mp3':
            from mutagen.id3 import ID3
            id3 = ID3(path)
            fr = id3.getall('USLT')
            if fr:
                lyrics = fr[0].text
            else:
                txxx = id3.getall('TXXX:USLT')
                if txxx:
                    lyrics = txxx[0].text[0]
            cover = bool(id3.getall('APIC'))
        elif ext in ('.m4a', '.aac'):
            from mutagen.mp4 import MP4
            m = MP4(path)
            v = m.tags.get('\xa9lyr') if m.tags else None
            lyrics = v[0] if v else ''
            cover = bool(m.tags and m.tags.get('covr'))
        elif ext == '.flac':
            from mutagen.flac import FLAC
            fl = FLAC(path)
            if fl.tags:
                for k in ('lyrics', 'LYRICS', 'unsyncedlyrics', 'UNSYNCEDLYRICS'):
                    if k in fl.tags:
                        lyrics = fl.tags[k][0]
                        break
            cover = bool(fl.pictures)
        elif ext in ('.ogg', '.opus'):
            from mutagen import File as MFile
            m = MFile(path)
            if m and m.tags:
                for k in ('lyrics', 'LYRICS', 'unsyncedlyrics', 'UNSYNCEDLYRICS'):
                    if k in m.tags:
                        lyrics = m.tags[k][0]
                        break
                cover = 'metadata_block_picture' in m.tags or 'METADATA_BLOCK_PICTURE' in m.tags
    except Exception:
        pass
    return lyrics, cover


def extract_meta(path):
    from mutagen import File as MFile
    ext = os.path.splitext(path)[1].lower()
    try:
        easy = MFile(path, easy=True)
    except Exception:
        easy = None
    tags = easy.tags if easy else None
    duration = 0
    if easy is not None and easy.info:
        duration = int(easy.info.length or 0)
    lyrics, cover = read_lyrics_and_cover(path, ext)
    return {
        'title': easy_get(tags, 'title') or os.path.splitext(os.path.basename(path))[0],
        'artist': easy_get(tags, 'artist'),
        'album': easy_get(tags, 'album'),
        'albumartist': easy_get(tags, 'albumartist'),
        'genre': easy_get(tags, 'genre'),
        'date': easy_get(tags, 'date'),
        'track': easy_get(tags, 'tracknumber'),
        'duration': duration,
        'lyrics': lyrics,
        'has_cover': cover,
    }


def get_meta_cached(path):
    """Retorna os metadados do arquivo, reaproveitando o cache em disco quando o
    arquivo não mudou (mesmo tamanho e mesma data de modificação). Isso evita
    reabrir e reler as tags de músicas já escaneadas antes, tornando um novo
    scan muito mais rápido quando a maior parte da biblioteca já é conhecida."""
    st = os.stat(path)
    cached = META_CACHE.get(path)
    if cached and cached.get('mtime') == st.st_mtime and cached.get('size') == st.st_size:
        return dict(cached['meta'])
    meta = extract_meta(path)
    META_CACHE[path] = {'mtime': st.st_mtime, 'size': st.st_size, 'meta': meta}
    return dict(meta)


def path_artist_collection(root_dir, root, mode='library', root_label=''):
    """Deriva Artista e Álbum/Playlist a partir da estrutura de pastas (sem tocar
    nas tags do arquivo, e sem copiar nada — sempre a pasta original). O modo é
    decidido automaticamente por pasta (ver detect_mode), pra não precisar
    perguntar pra pessoa se é "a biblioteca toda" ou "um álbum só":

    mode='library' (a pasta tem Artista/Álbum dentro, duas camadas):
      pasta/<Artista>/<Álbum ou Playlist>/parça.ext -> artista + coleção
      pasta/<Artista>/parça.ext                      -> só artista, parça avulsa
      pasta/parça.ext                                -> nenhum dos dois, parça avulsa

    mode='extra' (a pasta em si já é um álbum/playlist, sem duas camadas):
      a própria pasta escolhida (root_label) é a coleção pras parças que
      estão direto nela; uma subpasta dentro dela vira artista.
      <pasta>/parça.ext                      -> coleção = nome da pasta
      <pasta>/<Artista>/parça.ext            -> artista, coleção = nome da pasta
    """
    rel = os.path.relpath(root, root_dir)
    if rel == '.':
        return ('', root_label) if mode == 'extra' else ('', '')
    parts = rel.replace('\\', '/').split('/')
    if mode == 'extra':
        artist = parts[0]
        collection = root_label
    else:
        artist = parts[0]
        collection = parts[1] if len(parts) >= 2 else ''
    return artist, collection


def detect_mode(root_dir):
    """Decide sozinho se a pasta escolhida parece ser 'uma biblioteca inteira'
    (tem subpastas de Artista, cada uma com Álbuns dentro — duas camadas) ou
    'um álbum/playlist só' (as parças estão soltas nela, ou numa única
    camada de subpastas). Só olha um nível de profundidade, é bem rápido."""
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                if not entry.is_dir():
                    continue
                try:
                    with os.scandir(entry.path) as it2:
                        for sub in it2:
                            if sub.is_dir():
                                return 'library'
                except OSError:
                    continue
    except OSError:
        pass
    return 'extra'


def scan_root(root_dir, seen, mode='library', root_label=''):
    """Percorre root_dir (sempre a pasta real, no lugar de origem — nada é
    copiado) adicionando parças a TRACKS. Artista e Álbum/Playlist vêm
    principalmente do nome das pastas (path_artist_collection); a tag do
    arquivo entra como reforço quando a pasta não dá essa informação (ex.:
    artista sem pasta própria, ou álbum quando a estrutura de pastas não tem
    esse nível) — assim a biblioteca fica organizada mesmo quando as pastas
    não seguem à risca Artista/Álbum. É lida do cache sempre que o arquivo não
    mudou (get_meta_cached), e qualquer edição de tag grava direto nesse
    mesmo arquivo (write_tags usa o path original).
    `seen` evita duplicatas: mesma parça (artista+título) já vista na mesma
    coleção (álbum/playlist), ou já vista solta na pasta do artista."""
    for root, _, files in os.walk(root_dir):
        folder_artist, collection = path_artist_collection(root_dir, root, mode, root_label)
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in EXTS:
                continue
            path = os.path.join(root, f)
            try:
                meta = get_meta_cached(path)
            except Exception:
                continue
            tid = track_id(path)

            artist = folder_artist or meta.get('artist') or ''
            # 'folder' é estritamente o que veio da estrutura de pastas (usado
            # pras Playlists); 'album' pode cair pra tag do arquivo quando a
            # pasta não define um álbum, pra biblioteca não ficar sem álbuns
            # só porque as músicas não estão em Artista/Álbum/parça.
            album = collection or meta.get('album') or ''
            meta.update({
                'id': tid, 'path': path, 'ext': ext,
                'artist': artist,
                'album': album,
                'folder': collection,
            })

            dup_key = (norm(collection), norm(artist), norm(meta['title']))
            if dup_key in seen:
                continue

            TRACKS[tid] = meta
            seen.add(dup_key)


def scan_library():
    """Escaneia direto em cada pasta configurada (library_dirs) — sempre no
    lugar de origem, nada é copiado. Pra cada pasta, decide sozinho (via
    detect_mode) se ela é uma biblioteca com vários artistas ou um único
    álbum/playlist."""
    global TRACKS
    TRACKS = {}
    seen = set()
    cfg = load_config()
    for d in cfg.get('library_dirs', []):
        if d and os.path.isdir(d):
            mode = detect_mode(d)
            root_label = os.path.basename(os.path.normpath(d))
            scan_root(d, seen, mode=mode, root_label=root_label)
    save_meta_cache()


def prune_missing():
    """Remove da biblioteca em memória qualquer parça cujo arquivo não exista mais
    no disco, para que músicas indisponíveis não continuem aparecendo depois de
    atualizar a página (sem precisar de um rescan completo)."""
    for tid in list(TRACKS.keys()):
        if not os.path.isfile(TRACKS[tid]['path']):
            del TRACKS[tid]


def get_track_or_404(tid):
    t = TRACKS.get(tid)
    if not t or not os.path.isfile(t['path']):
        abort(404)
    return t


# ── Identificação via Deezer (busca por texto, sem fingerprint de áudio) ────
DEEZER_SEARCH_URL = 'https://api.deezer.com/search'
ITUNES_SEARCH_URL = 'https://itunes.apple.com/search'
DISCOVERY_CACHE = {}
DISCOVERY_TERMS = {
    'turkce': ['Türkçe müzik', 'Türk halk müziği', 'Türkçe pop'],
    'kurtce': ['Kürtçe müzik', 'Kurmancî music', 'Zazakî müzik'],
}


def _http_get_json(url, timeout=6):
    req = urllib.request.Request(url, headers={'User-Agent': 'İNADINA-TV-MÜZİK-ÇALAR/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', errors='ignore'))


def _clean_query_text(s):
    """Remove ruído comum de nome de arquivo/tag ('official video', '(feat. X)',
    números de parça, extensões) antes de mandar pro Deezer, pra melhorar o match."""
    s = s or ''
    s = re.sub(r'\(.*?\)|\[.*?\]', ' ', s)
    s = re.sub(r'\b(official\s*(video|audio|lyric[s]?)?|lyric[s]?\s*video|hq|hd|clipe\s*oficial)\b', ' ', s, flags=re.I)
    s = re.sub(r'^\s*\d{1,3}[\s.\-]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def deezer_search_candidates(title, artist):
    title_q = _clean_query_text(title)
    artist_q = _clean_query_text(artist)
    if artist_q:
        adv = f'artist:"{artist_q}" track:"{title_q}"'
        url = f'{DEEZER_SEARCH_URL}?q={urllib.parse.quote(adv)}'
    else:
        url = f'{DEEZER_SEARCH_URL}?q={urllib.parse.quote(title_q)}'
    data = _http_get_json(url)
    results = data.get('data') or []
    if not results and artist_q:
        # busca avançada às vezes não acha nada; tenta busca simples como fallback
        url = f'{DEEZER_SEARCH_URL}?q={urllib.parse.quote(artist_q + " " + title_q)}'
        data = _http_get_json(url)
        results = data.get('data') or []
    return results


def _score_candidate(cand, title, artist, duration):
    score = 0.0
    if norm(cand.get('title_short') or cand.get('title')) == norm(title):
        score += 2.0
    elif norm(title) in norm(cand.get('title') or ''):
        score += 1.0
    cand_artist = (cand.get('artist') or {}).get('name', '')
    if norm(cand_artist) == norm(artist):
        score += 2.0
    elif artist and (norm(artist) in norm(cand_artist) or norm(cand_artist) in norm(artist)):
        score += 1.0
    cand_dur = cand.get('duration') or 0
    if duration and cand_dur:
        diff = abs(cand_dur - duration)
        if diff <= 2:
            score += 2.0
        elif diff <= 5:
            score += 1.0
        else:
            score -= 1.0
    return score


def deezer_best_match(title, artist, duration):
    try:
        candidates = deezer_search_candidates(title, artist)
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    if not candidates:
        return {'ok': True, 'match': None}
    best = max(candidates, key=lambda c: _score_candidate(c, title, artist, duration))
    if _score_candidate(best, title, artist, duration) < 2.0:
        return {'ok': True, 'match': None}
    album = best.get('album') or {}
    return {
        'ok': True,
        'match': {
            'deezer_id': best.get('id'),
            'title': best.get('title') or '',
            'artist': (best.get('artist') or {}).get('name', ''),
            'album': album.get('title', ''),
            'cover': album.get('cover_big') or album.get('cover_medium') or album.get('cover') or '',
            'duration': best.get('duration') or 0,
            'link': best.get('link', ''),
        }
    }


def deezer_album_extra(deezer_id):
    """Busca gênero e ano do álbum (o endpoint de busca de parças não traz isso)."""
    try:
        if deezer_id:
            data = _http_get_json(f'https://api.deezer.com/track/{deezer_id}')
            album = data.get('album') or {}
            alb_id = album.get('id')
            genre, date = '', ''
            if alb_id:
                adata = _http_get_json(f'https://api.deezer.com/album/{alb_id}')
                date = adata.get('release_date', '') or ''
                genres = ((adata.get('genres') or {}).get('data')) or []
                genre = genres[0]['name'] if genres else ''
            return {'genre': genre, 'date': date}
    except Exception:
        pass
    return {'genre': '', 'date': ''}


def itunes_search(query, limit=12):
    """Apple/iTunes Search API’den metadata alır; ses dosyasını proxy’lemez."""
    params = urllib.parse.urlencode({
        'term': query,
        'media': 'music',
        'entity': 'song',
        'country': 'TR',
        'limit': limit,
        'lang': 'tr_tr',
    })
    data = _http_get_json(f'{ITUNES_SEARCH_URL}?{params}', timeout=8)
    results = []
    for item in data.get('results') or []:
        results.append({
            'source': 'Apple Music',
            'title': item.get('trackName') or '',
            'artist': item.get('artistName') or '',
            'album': item.get('collectionName') or '',
            'artwork': (item.get('artworkUrl100') or '').replace('100x100', '300x300'),
            'url': item.get('trackViewUrl') or item.get('collectionViewUrl') or '',
            'preview': item.get('previewUrl') or '',
            'genre': item.get('primaryGenreName') or '',
            'release_date': (item.get('releaseDate') or '')[:10],
        })
    return results


def deezer_discovery_search(query, limit=12):
    """Deezer keşif sonuçlarını ortak bir dış-kaynak formatına dönüştürür."""
    data = _http_get_json(f'{DEEZER_SEARCH_URL}?{urllib.parse.urlencode({"q": query, "limit": limit})}', timeout=8)
    results = []
    for item in data.get('data') or []:
        album = item.get('album') or {}
        artist = item.get('artist') or {}
        results.append({
            'source': 'Deezer',
            'title': item.get('title') or '',
            'artist': artist.get('name') or '',
            'album': album.get('title') or '',
            'artwork': album.get('cover_medium') or album.get('cover') or '',
            'url': item.get('link') or '',
            'preview': item.get('preview') or '',
            'genre': '',
            'release_date': '',
        })
    return results


def discovery_results(language='all', query=''):
    """Türkçe/Kürtçe keşfi iki ücretsiz metadata kaynağından birleştirir."""
    query = _clean_query_text(query)
    terms = [query] if query else []
    if not terms:
        if language in ('turkce', 'kurtce'):
            terms = DISCOVERY_TERMS[language]
        else:
            terms = DISCOVERY_TERMS['turkce'][:2] + DISCOVERY_TERMS['kurtce'][:2]
    cache_key = f'{language}:{"|".join(terms)}'
    cached = DISCOVERY_CACHE.get(cache_key)
    if cached and cached['expires'] > __import__('time').time():
        return cached['items']
    items, seen = [], set()
    for term in terms[:4]:
        for fetcher in (deezer_discovery_search, itunes_search):
            try:
                for item in fetcher(term, limit=8):
                    key = (norm(item['title']), norm(item['artist']))
                    if key == ('', '') or key in seen:
                        continue
                    seen.add(key)
                    item['query'] = term
                    items.append(item)
            except Exception:
                continue
    items = items[:48]
    DISCOVERY_CACHE[cache_key] = {'expires': __import__('time').time() + 600, 'items': items}
    return items


@app.route('/api/discover')
def discover():
    language = request.args.get('language', 'all').lower()
    if language not in ('all', 'turkce', 'kurtce'):
        language = 'all'
    query = request.args.get('q', '')[:120]
    return jsonify({
        'language': language,
        'query': query,
        'sources': ['Deezer', 'Apple Music'],
        'items': discovery_results(language, query),
    })


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/browse_folder')
def browse_folder():
    """Tenta abrir o seletor nativo se houver interface gráfica (ex: PC).
    Se estiver em ambiente sem display (ex: Termux no Android), avisa o front para usar o seletor web."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title='Müzik klasörünü seçin')
        root.destroy()
        if not path:
            return jsonify({'ok': False, 'canceled': True})
        return jsonify({'ok': True, 'path': os.path.abspath(path)})
    except Exception as e:
        return jsonify({'ok': False, 'headless': True, 'error': str(e)})


def guess_storage_roots():
    """Locais onde o armazenamento do Android costuma estar montado (direto,
    ou via os symlinks que o `termux-setup-storage` cria). Usado só pra
    resolver o caminho real de uma pasta escolhida no seletor do navegador
    — a pessoa nunca digita nada, o app tenta achar sozinho."""
    roots = []

    def add(p):
        if p and os.path.isdir(p) and p not in roots:
            roots.append(p)

    add('/storage/emulated/0')
    add('/sdcard')
    add('/storage/self/primary')
    add('/mnt/sdcard')
    home = os.path.expanduser('~')
    add(home)
    add(os.path.join(home, 'Music'))
    add(os.path.join(home, 'Downloads'))
    for sub in ('storage/shared', 'storage/music', 'storage/downloads', 'storage/movies', 'storage/dcim'):
        add(os.path.join(home, sub))
    try:
        if os.path.isdir('/storage'):
            for name in os.listdir('/storage'):
                add(os.path.join('/storage', name))
    except OSError:
        pass
    return roots


@app.route('/api/resolve_folder', methods=['POST'])
def resolve_folder():
    """Recebe o caminho relativo (webkitRelativePath) de um arquivo dentro da
    pasta que a pessoa escolheu no seletor nativo do Android/navegador, e tenta achar
    o caminho real completo dessa pasta no aparelho."""
    data = request.get_json(force=True, silent=True) or {}
    raw_rel = data.get('relpath') or ''
    relpath = raw_rel.replace('\\', '/').lstrip('/')
    if not relpath:
        return jsonify({'ok': False, 'error': 'Klasör seçicisinden yol alınamadı.'}), 400

    top_folder = relpath.split('/')[0] if '/' in relpath else ''
    roots = guess_storage_roots()

    for root in roots:
        target_file = os.path.join(root, relpath)
        if os.path.isfile(target_file):
            folder_path = os.path.join(root, top_folder) if top_folder else os.path.dirname(target_file)
            return jsonify({'ok': True, 'path': folder_path})

    if top_folder:
        for root in roots:
            candidate = os.path.join(root, top_folder)
            if os.path.isdir(candidate):
                return jsonify({'ok': True, 'path': candidate})

    return jsonify({'ok': False, 'error': 'Bu klasör cihaz depolamasında bulunamadı.'})


@app.route('/api/folders', methods=['GET', 'POST', 'DELETE'])
def folders():
    """Adiciona/remove uma pasta pelo caminho real no disco — nada é
    enviado/copiado pro servidor, a pasta é escaneada direto de onde ela
    está, e qualquer edição de tag grava nos arquivos ali mesmo."""
    cfg = load_config()
    if request.method == 'GET':
        return jsonify({'library_dirs': cfg.get('library_dirs', [])})

    data = request.get_json(force=True, silent=True) or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({'ok': False, 'error': 'Klasör belirtilmedi.'}), 400
    if not os.path.isdir(path):
        return jsonify({'ok': False, 'error': 'Klasör sunucuda bulunamadı.'}), 400
    norm_path = os.path.normpath(os.path.abspath(path))
    dirs = cfg.get('library_dirs', [])

    if request.method == 'DELETE':
        dirs = [d for d in dirs if os.path.normpath(os.path.abspath(d)) != norm_path]
    else:
        if not any(os.path.normpath(os.path.abspath(d)) == norm_path for d in dirs):
            dirs.append(path)

    cfg['library_dirs'] = dirs
    save_config(cfg)
    scan_library()
    return jsonify({'ok': True, 'count': len(TRACKS), 'library_dirs': dirs})


@app.route('/api/library')
def library():
    if request.args.get('rescan') or not TRACKS:
        scan_library()
    else:
        prune_missing()
    cfg = load_config()
    tracks = [{k: v for k, v in t.items() if k not in ('path', 'lyrics')} for t in TRACKS.values()]
    tracks.sort(key=lambda t: (t['artist'].lower(), t['album'].lower(), t['title'].lower()))
    resp = jsonify({'tracks': tracks, 'library_dirs': cfg.get('library_dirs', [])})
    # Evita que o navegador sirva uma resposta antiga logo depois de adicionar
    # uma pasta, quando o front força um reload imediato.
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/stream/<tid>')
def stream(tid):
    t = get_track_or_404(tid)
    return send_file(t['path'], conditional=True)


@app.route('/api/download/<tid>')
def download(tid):
    t = get_track_or_404(tid)
    name = f"{t['artist']} - {t['title']}".strip(' -') or 'parça'
    name = ''.join(c for c in name if c not in '\\/:*?"<>|').strip() + t['ext']
    return send_file(t['path'], as_attachment=True, download_name=name)


@app.route('/api/cover/<tid>')
def cover(tid):
    t = get_track_or_404(tid)
    ext = t['ext']
    data, mime = None, 'image/jpeg'
    try:
        if ext == '.mp3':
            from mutagen.id3 import ID3
            fr = ID3(t['path']).getall('APIC')
            if fr:
                data, mime = fr[0].data, fr[0].mime
        elif ext == '.flac':
            from mutagen.flac import FLAC
            pics = FLAC(t['path']).pictures
            if pics:
                data, mime = pics[0].data, pics[0].mime
        elif ext in ('.m4a', '.aac'):
            from mutagen.mp4 import MP4, MP4Cover
            m = MP4(t['path'])
            covr = m.tags.get('covr') if m.tags else None
            if covr:
                c = covr[0]
                mime = 'image/png' if c.imageformat == MP4Cover.FORMAT_PNG else 'image/jpeg'
                data = bytes(c)
        elif ext in ('.ogg', '.opus'):
            from mutagen import File as MFile
            from mutagen.flac import Picture
            m = MFile(t['path'])
            raw = None
            if m and m.tags:
                raw = m.tags.get('metadata_block_picture') or m.tags.get('METADATA_BLOCK_PICTURE')
            if raw:
                pic = Picture(base64.b64decode(raw[0]))
                data, mime = pic.data, pic.mime
    except Exception:
        data = None
    if not data:
        abort(404)
    return Response(data, mimetype=mime)


def write_tags(path, fields, cover_path=None):
    ext = os.path.splitext(path)[1]
    fd, tmp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(path))
    os.close(fd)
    cmd = ['ffmpeg', '-y', '-i', path]
    if cover_path:
        cmd += ['-i', cover_path, '-map', '0:a', '-map', '1:0',
                '-c:a', 'copy', '-c:v', 'copy', '-disposition:v', 'attached_pic']
    else:
        cmd += ['-map', '0', '-c', 'copy']
    for k, v in fields.items():
        cmd += ['-metadata', f'{k}={v}']
    if ext.lower() == '.mp3':
        cmd += ['-id3v2_version', '3']
    cmd.append(tmp)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, path)


@app.route('/api/deezer/<tid>')
def deezer_lookup(tid):
    t = get_track_or_404(tid)
    if not request.args.get('force') and tid in DEEZER_CACHE:
        cached = DEEZER_CACHE[tid]
        return jsonify({'ok': True, 'match': None if cached.get('no_match') else cached})

    result = deezer_best_match(t['title'], t['artist'], t.get('duration') or 0)
    if not result.get('ok'):
        return jsonify(result), 502

    match = result['match']
    if not match:
        DEEZER_CACHE[tid] = {'no_match': True}
        save_deezer_cache()
        return jsonify({'ok': True, 'match': None})

    extra = deezer_album_extra(match['deezer_id'])
    match.update(extra)
    DEEZER_CACHE[tid] = match
    save_deezer_cache()
    return jsonify({'ok': True, 'match': match})


@app.route('/api/deezer/<tid>/apply', methods=['POST'])
def deezer_apply(tid):
    t = get_track_or_404(tid)
    match = DEEZER_CACHE.get(tid)
    if not match or match.get('no_match'):
        return jsonify({'ok': False, 'error': 'Bu parça için Deezer sonucu yok. Önce arama yapın.'}), 400

    fields = {}
    for k in ('title', 'genre', 'date'):
        if match.get(k):
            fields[k] = match[k]
    # artista/álbum da TAG do arquivo podem refletir o Deezer; a organização do
    # app continua vindo da pasta (não muda com isso, veja o endpoint de tags)
    if match.get('artist'):
        fields['artist'] = match['artist']
    if match.get('album'):
        fields['album'] = match['album']

    cover_path = None
    if match.get('cover'):
        try:
            fd, cover_path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            req = urllib.request.Request(match['cover'], headers={'User-Agent': 'İNADINA-TV-MÜZİK-ÇALAR/1.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                open(cover_path, 'wb').write(r.read())
        except Exception:
            if cover_path and os.path.exists(cover_path):
                os.remove(cover_path)
            cover_path = None

    try:
        write_tags(t['path'], fields, cover_path)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors='ignore')[-800:] if e.stderr else str(e)
        return jsonify({'ok': False, 'error': err}), 500
    finally:
        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)

    fresh = extract_meta(t['path'])
    try:
        st = os.stat(t['path'])
        META_CACHE[t['path']] = {'mtime': st.st_mtime, 'size': st.st_size, 'meta': fresh}
        save_meta_cache()
    except OSError:
        pass
    TRACKS[tid] = {**t, **fresh, 'artist': t['artist'], 'album': t['album'], 'folder': t['folder']}
    return jsonify({'ok': True})


@app.route('/api/tags/<tid>', methods=['GET', 'POST'])
def tags(tid):
    t = get_track_or_404(tid)
    if request.method == 'GET':
        return jsonify(extract_meta(t['path']))

    fields = {}
    for k in ('title', 'artist', 'album', 'album_artist', 'genre', 'date', 'track', 'lyrics'):
        v = request.form.get(k)
        if v is not None:
            fields[k] = v

    cover_file = request.files.get('cover')
    cover_path = None
    if cover_file and cover_file.filename:
        fd, cover_path = tempfile.mkstemp(suffix=os.path.splitext(cover_file.filename)[1] or '.jpg')
        os.close(fd)
        cover_file.save(cover_path)

    try:
        write_tags(t['path'], fields, cover_path)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors='ignore')[-800:] if e.stderr else str(e)
        return jsonify({'ok': False, 'error': err}), 500
    finally:
        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)

    fresh = extract_meta(t['path'])
    try:
        st = os.stat(t['path'])
        META_CACHE[t['path']] = {'mtime': st.st_mtime, 'size': st.st_size, 'meta': fresh}
        save_meta_cache()
    except OSError:
        pass

    # artista/álbum/pasta continuam vindo da estrutura de pastas, não da tag recém-salva
    TRACKS[tid] = {**t, **fresh, 'artist': t['artist'], 'album': t['album'], 'folder': t['folder']}
    return jsonify({'ok': True})


if __name__ == '__main__':
    scan_library()
    app.run(host='0.0.0.0', port=5000, debug=False)
