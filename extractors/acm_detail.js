// ============================================================
// ACM Digital Library 详情页 SSR 数据提取 (RDFa)
// ACM 用 RDFa 属性把数据嵌在 HTML 里，服务端渲染，无需等 React
// property="name" → 标题  property="abstract" → 摘要
// property="author" → 作者  property="isPartOf" → 会议/期刊
// ============================================================

(() => {
    // --- 标题: <h1 property="name"> ---
    const title = (document.querySelector('h1[property="name"]')?.textContent || '').trim();

    // --- 摘要: <section property="abstract"> ---
    let abstract = '';
    const absEl = document.querySelector('[property="abstract"]');
    if (absEl) {
        abstract = (absEl.textContent || '').trim();
        // 去掉 "Abstract" 前缀
        abstract = abstract.replace(/^Abstract\s*/i, '').trim();
    }
    // Fallback: find <h2>Abstract</h2> → next div
    if (!abstract || abstract.length < 50) {
        const h2 = Array.from(document.querySelectorAll('h2')).find(h => h.textContent.trim() === 'Abstract');
        if (h2) {
            const div = h2.nextElementSibling;
            if (div) abstract = (div.textContent || '').trim();
        }
    }

    // --- 作者: <span property="author"> → givenName + familyName ---
    const authorList = [];
    document.querySelectorAll('span[property="author"]').forEach(el => {
        const given = el.querySelector('[property="givenName"]')?.textContent?.trim() || '';
        const family = el.querySelector('[property="familyName"]')?.textContent?.trim() || '';
        const name = (given + ' ' + family).trim();
        if (name && name.length < 80) authorList.push(name);
    });
    const authors = authorList.join('; ');

    // --- 会议/期刊: <meta property="isPartOf"> ---
    let venue = '';
    const isPartOf = document.querySelector('meta[property="isPartOf"]');
    if (isPartOf) {
        venue = isPartOf.getAttribute('content') || '';
    }
    if (!venue) {
        const el = document.querySelector('[property="isPartOf"]');
        venue = (el?.textContent || '').trim();
    }

    // --- 年份 ---
    let year = '';
    const m = document.body.textContent.match(/\b(20\d{2})\b/);
    if (m) year = m[0];

    // --- DOI: 从 URL 提取 ---
    const doiMatch = window.location.href.match(/\b(10\.\d{4,}\/[^\s?#]+)/);
    const doi = doiMatch ? doiMatch[1] : '';

    // --- docId: 同样从 URL 提取 ---
    let docId = '';
    const idMatch = window.location.href.match(/\/doi\/[^/]+\/(\d+)/);
    if (idMatch) docId = idMatch[1];
    if (!docId && doi) docId = doi.replace(/\//g, '_');

    return {
        title,
        abstract,
        authors: authors || undefined,
        venue: venue || undefined,
        year: year || undefined,
        doi: doi || undefined,
        docId: docId || undefined,
    };
})()
