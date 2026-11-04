"""Prepara o ZIP de insumos e aciona Claude Code para criar uma LP por card."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from . import trello

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "prospector" / "data" / "jobs"
OUTPUT = ROOT / "Landing Pages"
PROMPT_RE = re.compile(r"(?ims)^prompt\s*:\s*(.*?)\s*^cores\s*:")
COLORS_RE = re.compile(r"(?ims)^cores\s*:\s*(.*?)\s*^dados do prospecto\s*:")
MAX_FILES = 150
MAX_UNCOMPRESSED = 600 * 1024 * 1024
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov"}
MAX_ATTACHMENT_BYTES = 9_500_000


def artifact_slug(slug: str, card_id: str) -> str:
    """Impede que prospects homônimos compartilhem arquivos de produção."""
    return f"{slug}-{card_id[:8]}"


def extract_prompt(desc: str) -> str:
    match = PROMPT_RE.search(desc or "")
    return match.group(1).strip() if match else ""


def extract_colors(desc: str) -> str:
    match = COLORS_RE.search(desc or "")
    return match.group(1).strip() if match else ""


def source_zips(attachments: list[dict], slug: str) -> list[dict]:
    """Aceita um ou mais ZIPs de insumos por card — o Trello limita anexo a 10 MB,
    então um card com mais mídia precisa dividir em vários ZIPs; ignora o ZIP de
    saída do próprio site. Ordem estável (mais antigo primeiro) para que, em caso
    de arquivo repetido entre ZIPs, o mais recente prevaleça na mescla."""
    candidates = [a for a in attachments if str(a.get("name", "")).lower().endswith(".zip")
                  and str(a.get("name", "")).lower() != f"{slug}.zip".lower()]
    return sorted(candidates, key=lambda a: a.get("date") or "")


def fingerprint(desc: str, attachments: list[dict]) -> str:
    ids = ",".join(sorted(a["id"] for a in attachments))
    return hashlib.sha256((desc + "\n" + ids).encode("utf-8")).hexdigest()


def _safe_members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = [m for m in z.infolist() if not m.is_dir()]
    if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_UNCOMPRESSED:
        raise ValueError("ZIP excede o limite de arquivos ou tamanho descompactado")
    for m in members:
        path = PurePosixPath(m.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"caminho inseguro no ZIP: {m.filename}")
        if path.parts[0].lower() != "assets":
            raise ValueError("ZIP deve conter apenas a pasta assets/ na raiz")
        if m.file_size and m.compress_size and m.file_size / m.compress_size > 200:
            raise ValueError(f"arquivo suspeito no ZIP: {m.filename}")
    return members


def unpack_many(zip_paths: list[Path], dest: Path) -> Path:
    """Extrai só mídia de assets/ de um ou mais ZIPs, mesclando no mesmo destino
    (um card pode ter vários ZIPs porque o Trello limita anexo a 10 MB). Limites
    anti zip-bomb aplicados ao conjunto; em caso de nome repetido entre ZIPs,
    o último da lista prevalece (chame com a ordem que deve vencer por último)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    total_files = 0
    total_size = 0
    any_asset = False
    for zip_path in zip_paths:
        with zipfile.ZipFile(zip_path) as z:
            members = _safe_members(z)
            total_files += len(members)
            total_size += sum(m.file_size for m in members)
            if total_files > MAX_FILES or total_size > MAX_UNCOMPRESSED:
                raise ValueError("ZIPs excedem juntos o limite de arquivos ou tamanho descompactado")
            assets = [m for m in members if m.filename.replace("\\", "/").split("/")[0].lower() == "assets"
                      and Path(m.filename).suffix.lower() in MEDIA_EXT]
            any_asset = any_asset or bool(assets)
            for m in assets:
                rel = PurePosixPath(m.filename.replace("\\", "/"))
                target = dest.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    if not any_asset:
        raise ValueError("assets/ precisa conter ao menos uma imagem ou vídeo (em algum dos ZIPs)")
    return dest / "assets"


def unpack(zip_path: Path, dest: Path) -> Path:
    """Extrai só mídia de assets/ de um único ZIP, com limites anti zip-bomb."""
    return unpack_many([zip_path], dest)


def _video_frames(assets: Path) -> None:
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    frames = assets / "frames"
    for video in list(assets.rglob("*.mp4")) + list(assets.rglob("*.mov")):
        frames.mkdir(exist_ok=True)
        for i, second in enumerate((0.2, 1.0, 3.0), 1):
            out = frames / f"{video.stem}-{i:02}.jpg"
            try:
                subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", str(second),
                                "-i", str(video), "-frames:v", "1", str(out)],
                               capture_output=True, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue


def prepare(tr: trello.Trello, card_id: str, slug: str, attachments: list[dict],
            description: str) -> tuple[Path, Path]:
    job = JOBS / slug
    job.mkdir(parents=True, exist_ok=True)
    zips = []
    for i, attachment in enumerate(attachments, 1):
        package = job / f"insumos-{i}.zip"
        tr.baixar_anexo(attachment["url"], package)
        zips.append(package)
    assets = unpack_many(zips, job / "input")
    _video_frames(assets)
    brief = job / "brief.md"
    brief.write_text(description + "\n", encoding="utf-8")
    return job, assets


def _instruction(job: Path, assets: Path, site: Path) -> str:
    return (
        "Crie uma landing page original, profissional, responsiva e pronta para revisão. "
        f"Leia o briefing (inclui Prompt, Cores e dados do prospecto) em {job / 'brief.md'}. "
        f"As mídias já estão copiadas em {site / 'assets'} — esse é o seu diretório de trabalho "
        "(cwd), então no HTML referencie cada mídia SEMPRE pelo caminho relativo "
        "'assets/<nome-do-arquivo>' (ex.: assets/foto.jpg, assets/frames/video-01.jpg para "
        "frames extraídos de vídeo). Nunca use caminho absoluto nem '../' para alcançar mídias "
        "fora de assets/. "
        f"Escreva os arquivos finais APENAS em {site}. O arquivo principal é index.html. "
        "Você é responsável pela copy e pelo HTML/CSS/JS. Use somente fatos comprovados "
        "no briefing ou insumos; sinalize dúvidas em Handoff.md, sem inventar avaliações, "
        "preços, horários ou nomes de clientes. Faça chamadas para WhatsApp apenas se "
        "o número estiver confirmado. "
        "Inclua title, viewport e h1; produza copy substancial e links internos válidos. "
        "Confira visualmente desktop e celular, evitando imagens quebradas, erros JavaScript "
        "e rolagem horizontal. "
        "Requisitos fixos não-negociáveis desta página, mesmo que o briefing não os repita: "
        "(1) no mínimo 15 animações diferentes ao longo da página, com variedade real — "
        "transições ao rolar, blur progressivo, movimento de fundo/parallax, efeito de "
        "'digitação' em texto, fade/slide, hover states, entre outros, sem repetir o mesmo "
        "efeito 15 vezes; (2) nenhum emoji usado como ícone em nenhum lugar da página — "
        "ícones sempre em SVG/PNG reais (ex.: um ícone de biblioteca aberta, ou o logo "
        "oficial do WhatsApp/e-mail quando fizer sentido); (3) o botão flutuante de "
        "WhatsApp deve reproduzir a animação de 'revelação circular' do botão oficial "
        "'Open app' da página de redirecionamento do WhatsApp (wa.me): position: "
        "relative + overflow: hidden no botão; um ::after circular atrás do texto "
        "(z-index: -1) com transform: scale(1) translateY(67px) em repouso; ao ativar "
        "(hover no desktop, e uma classe equivalente disparada por JS após um pequeno "
        "delay no mobile, já que toque não tem hover), o ::after anima para "
        "transform: scale(1.47) translateY(0) com transition: transform 1s "
        "cubic-bezier(.66,0,.34,1), revelando uma cor de preenchimento diferente "
        "(ex.: verde padrão -> tom escuro) enquanto o texto muda de cor com um delay "
        "de .5s. É uma transição de revelação ao interagir, não um pulso em loop. "
        "Não publique, não envie mensagens e não modifique o card do Trello. "
        "Ao terminar, descreva brevemente os arquivos criados."
    )


def run_claude(job: Path, assets: Path, slug: str,
               executable: str = "claude", timeout: int = 3600) -> tuple[Path, str]:
    exe = shutil.which(executable)
    if not exe:
        raise RuntimeError("Claude Code CLI não encontrado no PATH; instalar/autenticar antes de ativar produção")
    site = job / "site"
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    shutil.copytree(assets, site / "assets")
    cmd = [exe, "-p", _instruction(job, assets, site), "--output-format", "json",
           "--tools", "Read,Write,Edit,Glob,Grep", "--allowedTools", "Read", "Write",
           "Edit", "Glob", "Grep", "--disallowedTools", "mcp__*",
           "--permission-mode", "dontAsk", "--max-turns", "40"]
    result = subprocess.run(cmd, cwd=site, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    (job / "claude-result.json").write_text(result.stdout, encoding="utf-8")
    (job / "claude-stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Claude Code saiu com código {result.returncode}: {result.stderr[-400:]}")
    try:
        payload = json.loads(result.stdout)
        if payload.get("is_error"):
            raise RuntimeError(str(payload.get("result", "erro do Claude Code")))
    except json.JSONDecodeError:
        raise RuntimeError("Claude Code não retornou JSON válido")
    return site, str(payload.get("result", ""))


def promote(site: Path, slug: str) -> Path:
    """Torna a versão aprovada no QA o artefato atual sem perder a anterior."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    dest = OUTPUT / slug
    if dest.exists():
        backup = JOBS / slug / f"previous-{datetime.now():%Y%m%d-%H%M%S}"
        if backup.exists():
            raise FileExistsError(backup)
        shutil.move(str(dest), str(backup))
    shutil.move(str(site), str(dest))
    return dest


def _static_checks(site: Path) -> list[str]:
    problems: list[str] = []
    html_file = site / "index.html"
    if not html_file.is_file():
        problems.append("index.html ausente")
        return problems
    html = html_file.read_text(encoding="utf-8")
    for pattern, message in ((r"<h1\b", "<h1> ausente"),
                             (r"<title\b[^>]*>\s*\S", "title ausente"),
                             (r"<meta\s+name=[\"']viewport", "viewport ausente")):
        if not re.search(pattern, html, re.I):
            problems.append(message)
    if "{{" in html or "https://DOMINIO/" in html:
        problems.append("placeholder no HTML")
    if len(re.sub(r"<[^>]+>", " ", html).strip()) < 80:
        problems.append("copy insuficiente")
    local_assets = 0
    for file in [html_file, *site.rglob("*.css")]:
        content = file.read_text(encoding="utf-8", errors="replace")
        refs = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", content, re.I)
        refs += re.findall(r"url\(\s*[\"']?([^\)\"']+)", content, re.I)
        for ref in refs:
            ref = ref.strip()
            if not ref or ref.startswith(("#", "//")):
                continue
            parsed = urlparse(ref)
            if parsed.scheme in ("http", "https", "mailto", "tel", "data"):
                if parsed.scheme in ("http", "https") and not parsed.netloc:
                    problems.append(f"link externo inválido: {ref[:80]}")
                if parsed.netloc.lower() == "wa.me" and not re.fullmatch(r"/\d{10,15}", parsed.path):
                    problems.append(f"WhatsApp inválido: {ref[:80]}")
                continue
            if parsed.scheme:
                problems.append(f"URL não permitida: {ref[:80]}")
                continue
            target = (file.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(site.resolve()) or not target.is_file():
                problems.append(f"arquivo local ausente: {ref[:80]}")
            elif "assets" in target.relative_to(site.resolve()).parts:
                local_assets += 1
    if not local_assets:
        problems.append("nenhum asset local usado na página")
    return problems


def _browser_checks(site: Path, qa: Path) -> list[str]:
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        try:
            for label, width, height in (("desktop", 1366, 768), ("mobile", 390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height},
                                        device_scale_factor=1)
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)[:120]))
                page.goto((site / "index.html").resolve().as_uri(), wait_until="load", timeout=30000)
                page.evaluate("""async () => {
                    document.querySelectorAll('img[loading="lazy"]').forEach(img => { img.loading = 'eager'; });
                    await Promise.all([...document.images].map(img => img.complete ? null :
                        new Promise(res => { img.onload = img.onerror = res; })));
                }""")
                page.screenshot(path=str(qa / f"{label}.png"), full_page=True)
                info = page.evaluate("""() => ({
                    overflow: document.documentElement.scrollWidth > innerWidth + 8,
                    textLength: document.body.innerText.trim().length,
                    anchors: [...document.querySelectorAll('a[href^="#"]')]
                        .filter(a => a.hash.length > 1 &&
                            !document.getElementById(decodeURIComponent(a.hash.slice(1))))
                        .map(a => a.getAttribute('href')),
                    broken: [...document.images].filter(i => {
                        const src = (i.getAttribute('src') || '').trim();
                        return src !== '' && (!i.complete || i.naturalWidth === 0);
                    }).map(i => i.getAttribute('src'))
                })""")
                if info["overflow"]:
                    problems.append(f"rolagem horizontal em {label}")
                if info["textLength"] < 80:
                    problems.append(f"copy renderizada insuficiente em {label}")
                for href in info["anchors"][:5]:
                    problems.append(f"âncora sem destino em {label}: {href}")
                for src in info["broken"][:5]:
                    problems.append(f"imagem quebrada em {label}: {src}")
                for error in page_errors[:5]:
                    problems.append(f"JavaScript em {label}: {error}")
                page.close()
        finally:
            browser.close()
    return problems


def package(site: Path, slug: str) -> tuple[Path, list[str]]:
    problems = _static_checks(site)
    qa = site / "_qa"
    qa.mkdir(exist_ok=True)
    if (site / "index.html").is_file():
        try:
            problems.extend(_browser_checks(site, qa))
        except Exception as exc:
            problems.append(f"QA visual indisponível: {type(exc).__name__}: {str(exc)[:120]}")
    (qa / "report.md").write_text("# QA técnico\n\n" +
        ("\n".join(f"- [ ] {p}" for p in problems) if problems else "- [x] verificações básicas passaram") +
        "\n\nCapturas: desktop.png e mobile.png. Revisão visual, dados do negócio e "
        "publicação continuam pendentes.\n",
        encoding="utf-8")
    zpath = site / f"{slug}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in site.rglob("*"):
            if f.is_file() and f != zpath and "_qa" not in f.parts:
                z.write(f, f.relative_to(site).as_posix())
    return zpath, problems


def split_zip_for_upload(zip_path: Path) -> list[Path]:
    """Se o ZIP final passar do limite de anexo do Trello (10 MB), divide o mesmo
    conjunto de arquivos em vários ZIPs válidos menores, salvos ao lado do
    original. Baixar todos e extrair no mesmo diretório reconstrói o site
    completo (cada parte é um ZIP normal, abre sozinho)."""
    if zip_path.stat().st_size <= MAX_ATTACHMENT_BYTES:
        return [zip_path]
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        groups: list[list[zipfile.ZipInfo]] = [[]]
        sizes = [0]
        for info in infos:
            size = info.compress_size or info.file_size
            if sizes[-1] + size > MAX_ATTACHMENT_BYTES and groups[-1]:
                groups.append([])
                sizes.append(0)
            groups[-1].append(info)
            sizes[-1] += size
        n = len(groups)
        parts: list[Path] = []
        with zipfile.ZipFile(zip_path) as zin:
            for i, group in enumerate(groups, 1):
                out = zip_path.with_name(f"{zip_path.stem}-parte{i}-de-{n}.zip")
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
                    for info in group:
                        zout.writestr(info, zin.read(info.filename))
                parts.append(out)
    return parts
