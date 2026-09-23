(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.OptWallUi = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function formatOiWan(value) {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '—';
    return `${(numericValue / 10000).toFixed(2)}万`;
  }

  function collectStrikesAsc(expiry) {
    return [...new Set([
      ...expiry.call.strikes.map((item) => Number(item.strike)),
      ...expiry.put.strikes.map((item) => Number(item.strike)),
    ])].sort((a, b) => a - b);
  }

  function collectWallSelections(expiry) {
    return ['call', 'put'].flatMap((sideName) => (
      expiry[sideName].strikes
        .filter((item) => item.isWall)
        .map((item) => ({ sideName, item }))
    ));
  }

  return { formatOiWan, collectStrikesAsc, collectWallSelections };
}));
