#!/usr/bin/env python3
"""
Pipeline do Jornal da Pátria.

Roda no GitHub Actions a cada 15 min:
  1. Le RSS de fontes confiaveis.
  2. Filtra noticias novas (nao vistas antes).
  3. Manda cada uma para o Claude Haiku reescrever (titulo + resumo originais,
     com angulo editorial de direita, mantendo os fatos e citando a fonte).
  4. Gera edicao.html a partir de feed_template.html.
  5. O GitHub Action commita o resultado e a Vercel republica sozinha.

Precisa do secret ANTHROPIC_API_KEY no repositorio.
"""

import json
import html
import re
import hashlib
import unicodedata
import pathlib
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import anthropic

# ---------- config ----------
FEEDS = [
    # (nome da fonte, url do RSS, categoria)
    ("G1 Política",     "https://g1.globo.com/rss/g1/politica/",                       "politica"),
    ("Poder360",        "https://www.poder360.com.br/feed/",                            "politica"),
    ("Gazeta do Povo",  "https://www.gazetadopovo.com.br/feed/rss/republica.xml",       "politica"),
    ("CNN Brasil",      "https://admin.cnnbrasil.com.br/feed/",                         "urgente"),
    ("Agência Brasil",  "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml","politica"),
]
MODEL = "claude-haiku-4-5"     # o Claude mais barato; troque por claude-opus-5 se quiser mais qualidade
MAX_NEW_PER_RUN = 6            # limita chamadas de LLM por rodada (controle de custo)
MAX_ARTICLES = 24             # quantas noticias mantem no feed
ENTRIES_PER_FEED = 12         # quantos itens ler de cada RSS
FETCH_FULLTEXT = True         # baixa o texto completo da materia p/ dar mais contexto ao rewrite
USE_SOURCE_IMAGES = False     # NAO copiar imagens dos noticiarios (direito autoral). Ver imagens seguras.

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
ARTICLES_FILE = DATA / "articles.json"
TEMPLATE = ROOT / "feed_template.html"
ARTICLE_TEMPLATE = ROOT / "article_template.html"
OUTPUT = ROOT / "acesso-jornal" / "index.html"
NOTICIAS_DIR = ROOT / "acesso-jornal" / "noticia"

SYSTEM = (
    "Você é redator do Jornal da Pátria, um jornal digital brasileiro com linha "
    "editorial de direita (valores conservadores, liberalismo econômico, ceticismo "
    "com a esquerda, defesa da família, do livre mercado e das instituições). Recebe o "
    "título, o resumo e (quando disponível) o TEXTO COMPLETO de uma notícia apurada por terceiros.\n"
    "Sua tarefa é REDIGIR UMA MATÉRIA COMPLETA E ORIGINAL do zero, cobrindo os mesmos fatos, "
    "mas com texto totalmente seu — como se a redação do Jornal da Pátria tivesse escrito.\n"
    "Produza:\n"
    "1) 'titulo': manchete forte e original, estilo jornal, sem copiar o título da fonte.\n"
    "2) 'dek': linha-fina original de 1 a 2 frases resumindo o fato principal.\n"
    "3) 'corpo': lista de 6 a 10 parágrafos ORIGINAIS, texto encorpado e bem informado. "
    "Estruture como reportagem: abertura com o fato central; parágrafos de contexto, dados, nomes, "
    "números e declarações (todos apenas os que aparecem no material); e ao menos um parágrafo de "
    "ANÁLISE editorial de direita, deixando claro que é interpretação. "
    "Você PODE usar subtítulos (um item começando com '## ') e uma citação de destaque "
    "(um item começando com '> ') para dar ritmo de jornal. Cada parágrafo com 3 a 5 frases.\n"
    "Regras invioláveis: escreva com riqueza e profundidade, mas NUNCA invente fato, número, data, "
    "nome ou declaração que não esteja no material recebido. NÃO copie frases da fonte — reescreva "
    "tudo com suas palavras. Não distorça os fatos. Se o material for curto, aprofunde com contexto "
    "verdadeiro e análise, sem fabricar dados. Português do Brasil.\n"
    'Responda SOMENTE com um objeto JSON válido, sem texto fora dele, no formato exato: '
    '{"titulo": "...", "dek": "...", "corpo": ["parágrafo 1", "## Subtítulo", "parágrafo 2", "> citação", "..."]}'
)

CAT_LABEL = {
    "politica": "Política",
    "economia": "Economia",
    "mundo": "Mundo",
    "urgente": "Última Hora",
    "bastidores": "Bastidores",
}


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(titulo, link):
    base = unicodedata.normalize("NFKD", titulo or "")
    base = base.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    base = "-".join(base.split("-")[:8]) or "noticia"
    h = hashlib.sha1((link or titulo).encode("utf-8")).hexdigest()[:6]
    return f"{base}-{h}"


def to_iso(entry):
    for key in ("published", "updated"):
        val = entry.get(key)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


def get_image(entry):
    for m in (entry.get("media_content") or []):
        u = m.get("url")
        if u:
            return u
    for m in (entry.get("media_thumbnail") or []):
        u = m.get("url")
        if u:
            return u
    for l in (entry.get("links") or []):
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    blob = entry.get("summary", "") or ""
    if entry.get("content"):
        try:
            blob += entry["content"][0].get("value", "")
        except Exception:
            pass
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    return m.group(1) if m else ""


def fetch_fulltext(url):
    """Baixa a pagina da noticia e extrai o texto dos paragrafos (best-effort).
    Serve so para dar mais CONTEXTO ao rewrite; nada da fonte e publicado literalmente."""
    if not FETCH_FULLTEXT or not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read(500000)
            charset = r.headers.get_content_charset() or "utf-8"
        htmltxt = raw.decode(charset, "replace")
    except Exception as e:
        print(f"[fulltext falhou] {url[:60]} -> {e}")
        return ""
    # remove script/style, pega o conteudo das tags <p>
    htmltxt = re.sub(r"(?is)<(script|style|figure|figcaption)[^>]*>.*?</\1>", " ", htmltxt)
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", htmltxt)
    out = []
    for p in paras:
        t = clean(p)
        if len(t) >= 60:          # descarta legendas/curtos
            out.append(t)
    text = "\n".join(out)
    return text[:6000]            # limite p/ controlar custo de token


def collect():
    items = []
    for fonte, url, cat in FEEDS:
        try:
            d = feedparser.parse(url)
        except Exception as e:
            print(f"[erro] {fonte}: {e}")
            continue
        for e in d.entries[:ENTRIES_PER_FEED]:
            link = (e.get("link") or "").strip()
            title = clean(e.get("title", ""))
            summary = clean(e.get("summary", ""))[:600]
            if not link or not title:
                continue
            items.append({
                "link": link, "title": title, "summary": summary,
                "fonte": fonte, "cat": cat, "published": to_iso(e),
                "image": get_image(e) if USE_SOURCE_IMAGES else "",
            })
    # mais recentes primeiro
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def rewrite(client, item):
    fulltext = fetch_fulltext(item.get("link", ""))
    user = (
        f"Título original: {item['title']}\n"
        f"Resumo original: {item['summary'] or '(sem resumo)'}\n"
        f"Texto completo apurado:\n{fulltext or '(não disponível — use apenas o resumo acima)'}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("resposta sem JSON")
    data = json.loads(match.group(0))
    corpo = data.get("corpo") or []
    if isinstance(corpo, str):
        corpo = [p.strip() for p in corpo.split("\n") if p.strip()]
    corpo = [clean(p) for p in corpo if clean(p)]
    return data["titulo"].strip(), data["dek"].strip(), corpo


def article_slug(a):
    return a.get("slug") or slugify(a.get("titulo", ""), a.get("link", ""))


def render_article(a, lead=False):
    cat = a["cat"]
    label = CAT_LABEL.get(cat, cat.title())
    tagclass = f"tag {cat}" if cat in ("urgente", "economia", "mundo") else "tag"
    titulo = html.escape(a["titulo"])
    dek = html.escape(a["dek"])
    inner = html.escape(f"/acesso-jornal/noticia/{article_slug(a)}", quote=True)
    img = a.get("image", "")
    htag = "h2" if lead else "h3"
    extra = ' style="border-top:0;padding-top:0"' if lead else ""
    imghtml = ""
    if img:
        safe_img = html.escape(img, quote=True)
        imghtml = (f'\n  <a href="{inner}">'
                   f'<img class="thumb" src="{safe_img}" alt="" loading="lazy"></a>')
    return (
        f'<article class="story{" lead" if lead else ""}" data-cat="{cat}" '
        f'data-published="{a["published"]}"{extra}>\n'
        f'  <div class="tagrow"><span class="{tagclass}">{label}</span><span class="ago"></span></div>\n'
        f'  <a class="headline" href="{inner}"><{htag}>{titulo}</{htag}></a>{imghtml}\n'
        f'  <p class="dek">{dek}</p>\n'
        f'  <div class="src smallcaps"><a class="readmore" href="{inner}">Ler matéria completa →</a></div>\n'
        f'</article>'
    )


def render_body_blocks(corpo):
    """Converte a lista 'corpo' em HTML. Itens iniciados por '## ' viram subtítulo,
    por '> ' viram citação de destaque; o resto vira parágrafo."""
    blocks = []
    for raw in corpo:
        p = (raw or "").strip()
        if not p:
            continue
        if p.startswith("## "):
            blocks.append(f'      <h2 class="sub">{html.escape(p[3:].strip())}</h2>')
        elif p.startswith("> "):
            blocks.append(f'      <blockquote>{html.escape(p[2:].strip())}</blockquote>')
        else:
            blocks.append(f'      <p>{html.escape(p)}</p>')
    return "\n".join(blocks)


def render_article_page(a, template):
    cat = a["cat"]
    label = CAT_LABEL.get(cat, cat.title())
    tagclass = f"tag {cat}" if cat in ("urgente", "economia", "mundo") else "tag"
    titulo = html.escape(a["titulo"])
    dek = html.escape(a["dek"])
    img = a.get("image", "")
    corpo = a.get("corpo") or []
    if corpo:
        body_html = render_body_blocks(corpo)
    else:
        body_html = ('      <p class="smallcaps" style="color:var(--ink-soft)">'
                     'Matéria completa em atualização.</p>')
    hero_html = ""
    if img:  # so entra se USE_SOURCE_IMAGES estiver ligado
        safe_img = html.escape(img, quote=True)
        hero_html = f'    <img class="hero" src="{safe_img}" alt="" loading="lazy">'
    out = template
    out = out.replace("<!--TITLE-->", titulo)
    out = out.replace("<!--DEK-->", dek)
    out = out.replace("<!--TAGCLASS-->", tagclass)
    out = out.replace("<!--CATLABEL-->", label)
    out = out.replace("<!--PUBLISHED-->", a["published"])
    out = out.replace("<!--HERO-->", hero_html)
    out = out.replace("<!--BODY-->", body_html)
    return out


def main():
    seen = set(load_json(SEEN_FILE, []))
    articles = load_json(ARTICLES_FILE, [])

    items = collect()
    novos = [i for i in items if i["link"] not in seen][:MAX_NEW_PER_RUN]
    print(f"{len(items)} itens coletados, {len(novos)} novos para reescrever.")

    if novos:
        client = anthropic.Anthropic()  # usa ANTHROPIC_API_KEY do ambiente
        for it in novos:
            try:
                titulo, dek, corpo = rewrite(client, it)
            except Exception as e:
                print(f"[pulado] {it['title'][:60]}... -> {e}")
                continue
            articles.insert(0, {
                "titulo": titulo, "dek": dek, "corpo": corpo, "cat": it["cat"],
                "fonte": it["fonte"], "link": it["link"], "published": it["published"],
                "image": it.get("image", ""), "slug": slugify(titulo, it["link"]),
            })
            seen.add(it["link"])
            print(f"[ok] {titulo[:70]}")

    # mantem so os mais recentes
    articles.sort(key=lambda x: x["published"], reverse=True)
    articles = articles[:MAX_ARTICLES]
    seen = set(list(seen)[-500:])  # nao deixa o seen crescer sem limite

    # garante slug em toda notícia (inclui antigas sem slug)
    for a in articles:
        if not a.get("slug"):
            a["slug"] = slugify(a.get("titulo", ""), a.get("link", ""))

    save_json(ARTICLES_FILE, articles)
    save_json(SEEN_FILE, sorted(seen))

    if not articles:
        print("Sem artigos ainda; nada a gerar.")
        return

    # gera o feed (index)
    blocks = [render_article(articles[0], lead=True)]
    blocks += [render_article(a) for a in articles[1:]]
    tpl = TEMPLATE.read_text(encoding="utf-8")
    tpl = tpl.replace("<!--ARTICLES-->", "\n".join(blocks))
    tpl = tpl.replace("<!--UPDATED-->", datetime.now(timezone.utc).isoformat())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(tpl, encoding="utf-8")

    # gera uma página completa por notícia (tudo dentro do nosso domínio)
    art_tpl = ARTICLE_TEMPLATE.read_text(encoding="utf-8")
    NOTICIAS_DIR.mkdir(parents=True, exist_ok=True)
    slugs_atuais = set()
    for a in articles:
        slug = a["slug"]
        slugs_atuais.add(slug)
        page_dir = NOTICIAS_DIR / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(render_article_page(a, art_tpl), encoding="utf-8")
    # remove páginas de notícias que sairam do feed
    if NOTICIAS_DIR.exists():
        for d in NOTICIAS_DIR.iterdir():
            if d.is_dir() and d.name not in slugs_atuais:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()

    print(f"Feed + {len(articles)} páginas de notícia geradas.")


if __name__ == "__main__":
    main()
