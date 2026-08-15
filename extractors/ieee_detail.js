// ============================================================
// IEEE Xplore 详情页 SSR 数据提取
// 读取 xplGlobal.document.metadata（服务端渲染的 JS 变量）
// 不依赖 DOM 选择器，不等 SPA 渲染
// ============================================================

(() => {
    // xplGlobal 是服务端直接嵌入 HTML 的 <script> 变量，
    // Page.loadEventFired 后就可用，无需等 Angular 渲染
    const g = (typeof xplGlobal !== 'undefined' && xplGlobal.document && xplGlobal.document.metadata)
        ? xplGlobal.document.metadata
        : null;

    if (!g) {
        return { error: 'xplGlobal.document.metadata not found' };
    }

    // 作者：优先用预拼接的 authorNames，否则从 authors 数组取
    let authors = '';
    if (g.authorNames) {
        authors = g.authorNames;
    } else if (Array.isArray(g.authors)) {
        authors = g.authors.map(a => a.name).filter(Boolean).join('; ');
    }

    // docId: 优先用 articleNumber，否则从 URL 取
    let docId = g.articleNumber || '';
    if (!docId) {
        const m = window.location.href.match(/\/document\/(\d+)/);
        docId = m ? m[1] : '';
    }

    return {
        title: g.title || '',
        abstract: g.abstract || '',
        authors: authors || undefined,
        venue: g.displayPublicationTitle || g.publicationTitle || '',
        year: g.publicationYear || '',
        doi: g.doi || '',
        docId: docId,
        location: g.publicationTitle || undefined,
    };
})()
