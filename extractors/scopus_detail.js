// ============================================================
// Scopus Detail Extractor v3 — Pure DOM extraction
// ============================================================
// 不再调用 REST API。导航到详情页后直接从 DOM innerText 提取
// 标题、作者、DOI、摘要。零额外网络请求。
//
// Usage:
//   1. browser.navigate to detail page: /pages/publications/{id}
//   2. browser.act(kind="evaluate", fn=<this script>)
//   3. Returns structured paper metadata from DOM
// ============================================================

(() => {
    const text = document.body.innerText;
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);

    // --- 标题：第一个有意义的非导航行 ---
    let title = '';
    const skipWords = ['Back', 'Skip to', 'Elsevier', 'Scopus', 'Search', 'Sources',
        'SciVal', 'Alerts', 'Brought to', 'User', 'How can', 'Menu', 'Login', 'English'];
    for (const line of lines) {
        const skip = skipWords.some(w => line.startsWith(w)) || line.length < 5;
        if (!skip) {
            title = line;
            break;
        }
    }

    // --- DOI ---
    let doi = '';
    for (const line of lines) {
        if (line.startsWith('DOI:')) {
            doi = line.replace('DOI:', '').trim();
            break;
        }
    }
    // Fallback: regex match
    if (!doi) {
        const m = text.match(/10\.\d{4,}\/[^\s"']+/);
        if (m) doi = m[0].replace(/[;.,]$/, '');
    }

    // --- 作者（标题后面的几行，在 DOI 前） ---
    let authors = '';
    let authorStarted = false;
    const authorLines = [];
    for (let i = 0; i < lines.length; i++) {
        if (lines[i] === title) {
            authorStarted = true;
            continue;
        }
        if (authorStarted) {
            if (lines[i].startsWith('DOI:') || lines[i].match(/^\d+[,.\d]*\s*(Citations|Citation)/)) {
                break;
            }
            // Skip affiliation lines (emails, departments, "Show all", "Send mail")
            if (lines[i].includes('@') ||
                lines[i].startsWith('Show all') ||
                lines[i].startsWith('Send mail') ||
                lines[i].startsWith('Department') ||
                lines[i].startsWith('University') ||
                lines[i].match(/^[a-zA-Z],\s/) ||
                lines[i].match(/^\d+$/)) {
                continue;
            }
            if (lines[i].length > 2) authorLines.push(lines[i]);
        }
    }
    authors = authorLines.join('; ');

    // --- 摘要 ---
    let abstract = '';
    const absIdx = lines.findIndex(l => l === 'Abstract');
    if (absIdx >= 0) {
        const absLines = [];
        for (let i = absIdx + 1; i < lines.length; i++) {
            const endMarkers = ['Keywords', 'Author keywords', 'Indexed keywords',
                'References', 'Cited by', 'Copyright', 'Show all', 'ISSN', 'ISBN'];
            if (endMarkers.some(m => lines[i].startsWith(m))) break;
            absLines.push(lines[i]);
        }
        abstract = absLines.join(' ').replace(/\s+/g, ' ').trim();
    }

    // --- 期刊/会议名 ---
    let venue = '';
    if (title) {
        const titleIdx = lines.indexOf(title);
        if (titleIdx >= 0 && titleIdx + 1 < lines.length) {
            const nextLine = lines[titleIdx + 1];
            if (nextLine.length > 3 && !nextLine.includes('@') && !nextLine.startsWith('DOI')) {
                venue = nextLine;
            }
        }
    }

    // --- Scopus ID (从 URL) ---
    let scopusId = '';
    const m = window.location.href.match(/\/publications\/(\d+)/);
    if (m) scopusId = m[1];
    if (!scopusId) {
        const em = window.location.href.match(/scp=(\d+)/);
        if (em) scopusId = em[1];
    }

    return {
        scopusId,
        title,
        authors: authors.substring(0, 500),
        doi,
        venue,
        abstract,
        pageUrl: window.location.href,
    };
})()
