// ============================================================
// Engineering Village (Compendex) Detail Extractor v3 — Pure DOM
// ============================================================
// 同 ev_detail.js v3。不再调用 REST API，纯 DOM 提取。
// ============================================================

(() => {
    const text = document.body.innerText;
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);

    let title = '';
    const pageTitle = document.title.replace(/\s*\|\s*Compendex\s*$/i, '').trim();
    if (pageTitle.length > 5) title = pageTitle;
    if (!title || title.length < 5) {
        for (const line of lines) {
            if (line.length > 10 && !line.startsWith('Skip') && !line.startsWith('Search')) {
                title = line; break;
            }
        }
    }

    let doi = '';
    const doiMatch = text.match(/10\.\d{4,}\/[^\s"']+/);
    if (doiMatch) doi = doiMatch[0].replace(/[;.,]$/, '');

    let authors = '';
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('Authors') || lines[i] === 'Authors:') {
            const authorLines = [];
            for (let j = i + 1; j < lines.length; j++) {
                if (['Source', 'Database', 'DOI', 'Document', 'Abstract', 'Publisher',
                    'ISSN', 'ISBN', 'Keywords'].some(m => lines[j].startsWith(m))) break;
                if (lines[j].length > 2) authorLines.push(lines[j]);
            }
            authors = authorLines.join('; '); break;
        }
    }

    let abstract = '';
    const absIdx = lines.findIndex(l => l === 'Abstract');
    if (absIdx >= 0) {
        const absLines = [];
        for (let i = absIdx + 1; i < lines.length; i++) {
            const endMarkers = ['Keywords', 'Author keywords', 'Indexed keywords',
                'References', 'Cited by', 'Copyright', 'Publisher', 'Database',
                'Document type', 'Source', 'ISSN', 'ISBN', 'Language'];
            if (endMarkers.some(m => lines[i] === m || lines[i].startsWith(m + ':'))) break;
            absLines.push(lines[i]);
        }
        abstract = absLines.join(' ').replace(/\s+/g, ' ').trim();
    }

    let venue = '';
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('Source:') || lines[i].startsWith('Source')) {
            const src = lines[i].replace(/^Source:?\s*/, '').trim();
            if (src) venue = src; break;
        }
    }

    let year = '';
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].match(/^\d{4}$/)) {
            const y = parseInt(lines[i]);
            if (y >= 1900 && y <= 2100) { year = lines[i]; break; }
        }
    }

    let docId = new URLSearchParams(window.location.search).get('docid') || '';
    if (!docId) {
        const m = window.location.href.match(/docid=([^&]+)/);
        if (m) docId = decodeURIComponent(m[1]);
    }

    return {
        docId, title, authors: authors.substring(0, 500),
        doi, venue, year, abstract, pageUrl: window.location.href,
    };
})()
