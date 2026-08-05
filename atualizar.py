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
import pathlib
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

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
ARTICLES_FILE = DATA / "articles.json"
TEMPLATE = ROOT / "feed_template.html"
OUTPUT = ROOT / "acesso-jornal" / "index.html"

SYSTEM = (
    "Você é editor do Jornal da Pátria, um jornal digital brasileiro com linha "
    "editorial de direita (valores conservadores, liberalismo econômico, ceticismo "
    "com a esquerda). Recebe o título e o resumo de uma notícia de uma fonte externa.\n"
    "Tarefa:\n"
    "1) Reescreva um TÍTULO original, forte, estilo manchete de jornal, sem copiar o título da fonte.\n"
    "2) Escreva um RESUMO (dek) ORIGINAL de 1 a 2 frases, com suas próprias palavras, "
    "mantendo os FATOS exatos (quem, o quê, quando, números, declarações).\n"
    "Regras invioláveis: aplique o enquadramento editorial de direita, mas NUNCA invente "
    "fato, número, data ou declaração; não copie frases da fonte; não exagere nem distorça o fato. "
    "Português do Brasil, direto ao ponto.\n"
    'Responda SOMENTE com um objeto JSON, sem nenhum texto fora dele, no formato exato: '
    '{"titulo": "...", "dek": "..."}'
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
                "image": get_image(e),
            })
    # mais recentes primeiro
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def rewrite(client, item):
    user = (
        f"Fonte: {item['fonte']}\n"
        f"Título original: {item['title']}\n"
        f"Resumo original: {item['summary'] or '(sem resumo)'}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("resposta sem JSON")
    data = json.loads(match.group(0))
    return data["titulo"].strip(), data["dek"].strip()


def render_article(a, lead=False):
    cat = a["cat"]
    label = CAT_LABEL.get(cat, cat.title())
    tagclass = f"tag {cat}" if cat in ("urgente", "economia", "mundo") else "tag"
    titulo = html.escape(a["titulo"])
    dek = html.escape(a["dek"])
    fonte = html.escape(a["fonte"])
    url = html.escape(a["link"], quote=True)
    img = a.get("image", "")
    htag = "h2" if lead else "h3"
    extra = ' style="border-top:0;padding-top:0"' if lead else ""
    imghtml = ""
    if img:
        safe_img = html.escape(img, quote=True)
        imghtml = (f'\n  <a href="{url}" target="_blank" rel="noopener">'
                   f'<img class="thumb" src="{safe_img}" alt="" loading="lazy"></a>')
    return (
        f'<article class="story{" lead" if lead else ""}" data-cat="{cat}" '
        f'data-published="{a["published"]}"{extra}>\n'
        f'  <div class="tagrow"><span class="{tagclass}">{label}</span><span class="ago"></span></div>\n'
        f'  <a class="headline" href="{url}" target="_blank" rel="noopener"><{htag}>{titulo}</{htag}></a>{imghtml}\n'
        f'  <p class="dek">{dek}</p>\n'
        f'  <div class="src smallcaps">Fonte: <a href="{url}" target="_blank" rel="noopener">{fonte}</a> · '
        f'<a class="readmore" href="{url}" target="_blank" rel="noopener">Ler notícia completa →</a></div>\n'
        f'</article>'
    )


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
                titulo, dek = rewrite(client, it)
            except Exception as e:
                print(f"[pulado] {it['title'][:60]}... -> {e}")
                continue
            articles.insert(0, {
                "titulo": titulo, "dek": dek, "cat": it["cat"],
                "fonte": it["fonte"], "link": it["link"], "published": it["published"],
                "image": it.get("image", ""),
            })
            seen.add(it["link"])
            print(f"[ok] {titulo[:70]}")

    # mantem so os mais recentes
    articles.sort(key=lambda x: x["published"], reverse=True)
    articles = articles[:MAX_ARTICLES]
    seen = set(list(seen)[-500:])  # nao deixa o seen crescer sem limite

    save_json(ARTICLES_FILE, articles)
    save_json(SEEN_FILE, sorted(seen))

    # gera edicao.html
    if not articles:
        print("Sem artigos ainda; nada a gerar.")
        return
    blocks = [render_article(articles[0], lead=True)]
    blocks += [render_article(a) for a in articles[1:]]
    tpl = TEMPLATE.read_text(encoding="utf-8")
    tpl = tpl.replace("<!--ARTICLES-->", "\n".join(blocks))
    tpl = tpl.replace("<!--UPDATED-->", datetime.now(timezone.utc).isoformat())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(tpl, encoding="utf-8")
    print(f"edicao.html gerado com {len(articles)} notícias.")


if __name__ == "__main__":
    main()
