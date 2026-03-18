# Accessible.

**NDID ADA/WCAG 2.1 AA Compliance Tool** by CAS Consulting

A self-hosted web tool that converts `.docx` and `.pdf` documents to ADA-accessible Markdown, with a scored WCAG 2.1 AA compliance audit on every conversion. Built for the North Dakota Insurance Department (NDID) for web publishing workflows.

---

## Features

- **Document conversion** — Upload `.docx` or `.pdf`, get clean Markdown output
- **WCAG 2.1 AA compliance audit** — 13 automated checks across 6 criteria
- **Scored output** — 0–100 score with Conformant / Partially Conformant / Non-Conformant tier
- **Readability scoring** — Flesch Reading Ease with plain language guidance
- **Issues grouped by WCAG criterion** — collapsible, with line numbers and remediation guidance
- **Two-panel UI** — live rendered preview + raw Markdown side-by-side
- **Download** — one-click `.md` file export

---

## Compliance Checks

| Check | WCAG Criterion | Level |
|-------|---------------|-------|
| Missing or multiple H1 | 2.4.2 Page Titled | AA |
| Skipped heading levels (e.g. H1 → H3) | 2.4.6 Headings & Labels | AA |
| Duplicate heading text | 2.4.6 Headings & Labels | AA |
| Empty headings | 1.3.1 Info & Relationships | A |
| Tables missing header separator row | 1.3.1 Info & Relationships | A |
| Tables without preceding context | 1.3.1 Info & Relationships | A |
| Manual bullet characters (•, –, —) | 1.3.1 Info & Relationships | A |
| Excessive ALL CAPS text | 1.3.1 Info & Relationships | A |
| Entire paragraph bolded | 1.3.1 Info & Relationships | A |
| Images missing or generic alt text | 1.1.1 Non-text Content | A |
| Embedded images not extracted | 1.1.1 Non-text Content | A |
| Color used as sole indicator | 1.4.1 Use of Color | AA |
| Bare URLs / non-descriptive link text | 2.4.4 Link Purpose | AA |
| Readability (Flesch Reading Ease) | 3.1.5 Reading Level | AAA advisory |

**Scoring:** 100 base − 15 per error − 5 per warning − 1 per info. Advisory (AAA) checks are reported but do not reduce the score.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Uvicorn (Python 3.12) |
| Conversion | Microsoft MarkItDown (`markitdown[docx,pdf]`) |
| Compliance engine | Custom Python — no external dependencies |
| Frontend | Single-file HTML/CSS/JS — no framework, no build step |
| Container | Python 3.12-slim + pandoc |

---

## Self-Hosting

### Requirements

- Docker + Docker Compose (modern CLI — `docker compose`, not `docker-compose`)
- Port `8740` available on the host

### Deploy

```bash
git clone https://github.com/coltonschulz/accessible ~/docker/accessible
cd ~/docker/accessible
docker compose up -d --build
```

Verify:
```bash
curl -s http://localhost:8740/ | grep -o '<title>[^<]*</title>'
# Expected: <title>Accessible — NDID ADA Compliance Tool</title>
```

### Nginx Reverse Proxy

A template config is included at `nginx/example.conf`. Replace `YOUR_DOMAIN` and install:

```bash
sed 's/YOUR_DOMAIN/your.domain.com/g' nginx/example.conf \
  | sudo tee /etc/nginx/sites-available/accessible > /dev/null

sudo ln -s /etc/nginx/sites-available/accessible /etc/nginx/sites-enabled/
sudo certbot --nginx -d your.domain.com
sudo nginx -t && sudo systemctl reload nginx
```

> **Note:** Set `client_max_body_size 50m` in nginx to match the app's upload limit. The template includes this.

---

## File Structure

```
accessible/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── nginx/
│   └── example.conf        # Reverse proxy template (YOUR_DOMAIN placeholder)
└── app/
    ├── main.py              # FastAPI app + full compliance engine
    └── static/
        └── index.html       # Single-file UI
```

---

## Known Limitations

1. **Complex financial tables** (Schedule P multi-level) may flatten in MarkItDown output — verify manually after conversion.
2. **Embedded images** are flagged but not extracted — host images separately and update `![]()` src paths in the output Markdown.
3. **Tracked changes** in Word should be accepted before uploading — MarkItDown reads the final document state.
4. **PDF fidelity** — PDFs converted from Word extract cleanly, but heading levels and table structure are often lost. Re-convert from `.docx` for best compliance scores.
5. **Max file size:** 50 MB.

---

## License

MIT
