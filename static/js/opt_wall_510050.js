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

  function formatPainYuan(value) {
    if (value === null || value === undefined) return '不可计算';
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '不可计算';
    const sign = numericValue < 0 ? '-' : '';
    const magnitude = Math.abs(numericValue);
    if (magnitude >= 100000000) return `${sign}${(magnitude / 100000000).toFixed(2)}亿元`;
    if (magnitude >= 10000) return `${sign}${(magnitude / 10000).toFixed(2)}万元`;
    return `${sign}${magnitude.toFixed(2)}元`;
  }

  function formatGammaExposure(value) {
    const formatted = formatPainYuan(value);
    return formatted === '不可计算' ? formatted : `${formatted}/1%`;
  }

  function formatIvPct(value) {
    if (value === null || value === undefined) return '不可计算';
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '不可计算';
    return `${(numericValue * 100).toFixed(2)}%`;
  }

  function formatFlowWan(value) {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '—';
    const sign = numericValue > 0 ? '+' : '';
    return `${sign}${(numericValue / 10000).toFixed(2)}万`;
  }

  function formatFlowVelocityWan(value) {
    if (value === null || value === undefined) return '不可计算';
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '不可计算';
    if (numericValue !== 0 && Math.abs(numericValue) < 100) {
      const sign = numericValue > 0 ? '+' : '-';
      return `${sign}<0.01万/日`;
    }
    return `${formatFlowWan(numericValue)}/日`;
  }

  function formatFlowAccelerationWan(value) {
    return formatFlowVelocityWan(value);
  }

  function formatFlowAccelerationRatio(value) {
    if (value === null || value === undefined) return '不可计算';
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '不可计算';
    const sign = numericValue > 0 ? '+' : numericValue < 0 ? '-' : '';
    const magnitude = Math.abs(numericValue);
    if (numericValue !== 0 && magnitude < 0.01) return `${sign}<0.01×`;
    return `${sign}${magnitude.toFixed(2)}×`;
  }

  function formatFlowShare(value) {
    if (value === null || value === undefined) return '不可计算';
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '不可计算';
    return `${(numericValue * 100).toFixed(2)}%`;
  }

  function formatDistancePct(value) {
    if (value === null || value === undefined) return '不可计算';
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return '不可计算';
    const magnitude = Math.abs(numericValue).toFixed(2);
    if (numericValue > 0) return `+${magnitude}%`;
    if (numericValue < 0) return `-${magnitude}%`;
    return `${magnitude}%`;
  }

  function formatFlowVelocityShare(value, denominator) {
    if (value === null || value === undefined || denominator === null || denominator === undefined) {
      return '不可计算';
    }
    const numericValue = Number(value);
    const numericDenominator = Number(denominator);
    if (!Number.isFinite(numericValue) || !Number.isFinite(numericDenominator) || numericDenominator <= 0) {
      return '不可计算';
    }
    const share = numericValue / numericDenominator;
    const sign = share > 0 ? '+' : share < 0 ? '-' : '';
    const percentage = Math.abs(share) * 100;
    if (share !== 0 && percentage < 0.01) return `${sign}<0.01%/日`;
    return `${sign}${percentage.toFixed(2)}%/日`;
  }

  function nullableNumber(value) {
    if (value === null || value === undefined) return null;
    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? numericValue : null;
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

  function summarizeSide(side) {
    return {
      wallStrikes: side.wallStrikes,
      wallOi: side.wallOi,
      top1Share: side.totalOi > 0 ? side.wallOi / side.totalOi : null,
      top3Strikes: side.strikes.filter((item) => item.isTop3).map((item) => item.strike),
      top3Share: side.top3Share,
    };
  }

  function summarizeBalance(expiry) {
    const callOi = Number(expiry.call.totalOi);
    const putOi = Number(expiry.put.totalOi);
    const totalOi = callOi + putOi;
    return {
      callOi,
      putOi,
      totalOi,
      callPutRatio: putOi > 0 ? callOi / putOi : null,
      callShare: totalOi > 0 ? callOi / totalOi : null,
      putShare: totalOi > 0 ? putOi / totalOi : null,
    };
  }

  function summarizeFlowSide(side, currentTotalOi = null) {
    const positiveFlowTotalOi = nullableNumber(side.positiveFlowTotalOi);
    const negativeFlowTotalOi = nullableNumber(side.negativeFlowTotalOi);
    const positiveWallOi = nullableNumber(side.positiveWallOi);
    const negativeWallOi = nullableNumber(side.negativeWallOi);
    const positiveWallConcentration = nullableNumber(side.positiveWallConcentration);
    const negativeWallConcentration = nullableNumber(side.negativeWallConcentration);
    const positiveWallVelocityOi = nullableNumber(side.positiveWallVelocityOi);
    const negativeWallVelocityOi = nullableNumber(side.negativeWallVelocityOi);
    const currentTotalOiValue = nullableNumber(currentTotalOi);
    const positiveWallVelocityShare = (
      positiveWallVelocityOi !== null && currentTotalOiValue !== null && currentTotalOiValue > 0
        ? positiveWallVelocityOi / currentTotalOiValue
        : null
    );
    const negativeWallVelocityShare = (
      negativeWallVelocityOi !== null && currentTotalOiValue !== null && currentTotalOiValue > 0
        ? negativeWallVelocityOi / currentTotalOiValue
        : null
    );
    return {
      positiveFlowTotalOi,
      positiveWallOi,
      positiveWallStrikes: side.positiveWallStrikes || [],
      positiveWallConcentration,
      positiveConcentrationLabel: formatFlowShare(positiveWallConcentration),
      positiveWallVelocityOi,
      positiveWallVelocityLabel: formatFlowVelocityWan(positiveWallVelocityOi),
      positiveWallVelocityShare,
      positiveWallVelocityShareLabel: formatFlowVelocityShare(positiveWallVelocityShare, 1),
      negativeFlowTotalOi,
      negativeWallOi,
      negativeWallStrikes: side.negativeWallStrikes || [],
      negativeWallConcentration,
      negativeConcentrationLabel: formatFlowShare(negativeWallConcentration),
      negativeWallVelocityOi,
      negativeWallVelocityLabel: formatFlowVelocityWan(negativeWallVelocityOi),
      negativeWallVelocityShare,
      negativeWallVelocityShareLabel: formatFlowVelocityShare(negativeWallVelocityShare, 1),
      positiveWallLabel: positiveWallOi === null ? '—' : formatFlowWan(positiveWallOi),
      negativeWallLabel: negativeWallOi === null ? '—' : formatFlowWan(negativeWallOi),
    };
  }

  return {
    formatOiWan,
    formatPainYuan,
    formatGammaExposure,
    formatIvPct,
    formatFlowWan,
    formatFlowVelocityWan,
    formatFlowAccelerationWan,
    formatFlowAccelerationRatio,
    formatFlowShare,
    formatDistancePct,
    formatFlowVelocityShare,
    collectStrikesAsc,
    collectWallSelections,
    summarizeSide,
    summarizeBalance,
    summarizeFlowSide,
  };
}));
